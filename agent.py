"""
Local Agent — runs on the Windows machine where AdsPower is installed.

Connects to the VPS via a persistent WebSocket.  The VPS pushes jobs and
commands in real-time; the agent executes them locally and sends results back.

Usage:
    python agent.py --vps http://YOUR_VPS_IP:5001 --key YOUR_WORKER_API_KEY

Environment variables (alternative to CLI flags):
    VPS_URL          VPS base URL
    WORKER_API_KEY   Shared secret key

Build as .exe:
    pip install pyinstaller
    pyinstaller --onefile --name verificador-agent agent.py
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time
import threading
from pathlib import Path

# Job-level cancellation flags: job_id → threading.Event
_cancel_flags: dict[int, threading.Event] = {}
_cancel_lock = threading.Lock()

import websockets
import websockets.exceptions

# ── Project imports ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import config as verif_config
from services.adspower import AdsPowerClient

_client = AdsPowerClient(verif_config.ADSPOWER_BASE)


# ── Profile sync ──────────────────────────────────────────────────────────────

async def _sync_profiles(outbox: asyncio.Queue):
    """Collect AdsPower profiles and push them to the VPS via outbox."""
    try:
        # Run blocking AdsPower calls in a thread so we don't block the event loop
        def _collect():
            group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
            name_to_id = {
                g["group_name"]: str(g["group_id"])
                for g in group_data.get("list", [])
            }
            target_groups = {
                verif_config.VERIFICAR_GROUP_NAME,
                verif_config.VERIFICADAS_GROUP_NAME,
            }
            profiles = []
            for group_name, group_id in name_to_id.items():
                if group_name not in target_groups:
                    continue
                for p in _client.list_profiles(group_id=group_id):
                    profiles.append({
                        "profile_id": p["user_id"],
                        "name":       p.get("name", ""),
                        "group_name": group_name,
                        "remark":     p.get("remark", ""),
                    })
            return profiles

        profiles = await asyncio.to_thread(_collect)
        await outbox.put(json.dumps({"type": "profiles_push", "profiles": profiles}))
        print(f"[SYNC] {len(profiles)} perfis enviados ao VPS")
    except Exception as e:
        print(f"[SYNC] Falha: {e}")


# ── Screenshot capture ────────────────────────────────────────────────────────

def _capture_screenshot_b64(since_epoch: float) -> str:
    debug_dir = Path(verif_config.DEBUG_DIR)
    if not debug_dir.exists():
        return ""
    candidates = [
        p for p in debug_dir.rglob("*.png")
        if p.stat().st_mtime >= since_epoch
    ]
    if not candidates:
        candidates = list(debug_dir.rglob("*.png"))
    if not candidates:
        return ""
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return base64.b64encode(latest.read_bytes()).decode()


# ── Gerador block parser ──────────────────────────────────────────────────────

def _parse_gerador_block(remark: str) -> dict | None:
    marker = verif_config.GERADOR_REMARK_MARKER
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


# ── Job execution (blocking — runs in thread executor) ───────────────────────

def _execute_job_sync(job: dict) -> dict:
    """
    Execute a verification job synchronously (called from asyncio.to_thread).
    Returns a dict suitable for the job_done WebSocket message.
    """
    job_id      = job["id"]
    profile_id  = job["profile_id"]
    business_id = job.get("business_id", "")
    sms_payload = job.get("sms")   # injected by VPS; contains provider + credentials

    # Register a cancel flag for this job
    cancel_event = threading.Event()
    with _cancel_lock:
        _cancel_flags[job_id] = cancel_event

    print(f"[JOB {job_id}] Iniciando para perfil {profile_id}…")

    success          = False
    message          = ""
    screenshot_b64   = ""
    error_traceback  = ""
    e                = None

    # Check if already cancelled before we even start
    if cancel_event.is_set():
        with _cancel_lock:
            _cancel_flags.pop(job_id, None)
        return {"type": "job_cancelled", "job_id": job_id, "success": False,
                "message": "Cancelado antes de iniciar", "step_name": "", "page_url": "",
                "page_html": "", "traceback": "", "screenshot_b64": ""}

    try:
        from main import _run_for_profile, _mark_verified, _mark_restricted, _acquire_run_id
        from services.facebook_bot import BmRestrictedException

        profile      = _client.get_profile(profile_id)
        remark       = profile.get("remark", "")
        gerador_data = _parse_gerador_block(remark)
        run_id       = gerador_data.get("run_id")  if gerador_data else None
        email_mode   = gerador_data.get("email_mode", "own") if gerador_data else "own"

        if run_id is None:
            print(f"[JOB {job_id}] Sem run_id — adquirindo do Gerador…")
            run_id = _acquire_run_id()
            gerador_data = gerador_data or {}
            gerador_data["run_id"] = run_id

        if business_id:
            gerador_data = gerador_data or {}
            gerador_data["business_id"] = business_id

        start_time = time.time()

        success = _run_for_profile(
            profile=profile,
            run_id=run_id,
            email_mode=email_mode,
            sms_payload=sms_payload,
            business_id=business_id,
            gerador_data=gerador_data or {},
        )

        if success:
            _mark_verified(profile_id)
            message = "Verificação concluída com sucesso!"
        else:
            message = "Verificação falhou."

        screenshot_b64 = _capture_screenshot_b64(since_epoch=start_time)

    except BmRestrictedException as e:
        import traceback as _tb
        message = str(e)
        error_traceback = _tb.format_exc()
        print(f"[JOB {job_id}] BM Restrita: {e}")
        try:
            from main import _mark_restricted
            _mark_restricted(profile_id)
        except Exception as _me:
            print(f"[JOB {job_id}] Could not mark as restricted: {_me}")

    except Exception as e:
        import traceback as _tb
        message = str(e)[:500]
        error_traceback = _tb.format_exc()
        print(f"[JOB {job_id}] Exceção: {e}")

    step_name = ""
    page_url  = ""
    page_html = ""
    try:
        from services.facebook_bot import VerificationStepError as _VSE
        cause = e if isinstance(e, _VSE) else getattr(e, "__cause__", None)
        if isinstance(cause, _VSE):
            step_name = cause.step
            page_url  = cause.page_url
            page_html = (cause.page_html or "")[:50000]
    except Exception:
        pass

    # Clean up cancel flag
    with _cancel_lock:
        _cancel_flags.pop(job_id, None)

    # If cancelled mid-run, report as cancelled so VPS resets status properly
    if cancel_event.is_set():
        print(f"[JOB {job_id}] ✗ Cancelado")
        return {"type": "job_cancelled", "job_id": job_id, "success": False,
                "message": "Cancelado manualmente", "step_name": "", "page_url": "",
                "page_html": "", "traceback": "", "screenshot_b64": screenshot_b64}

    print(f"[JOB {job_id}] {'✓ Sucesso' if success else '✗ Falha'}")
    return {
        "type":           "job_done",
        "job_id":         job_id,
        "success":        success,
        "message":        message,
        "step_name":      step_name,
        "page_url":       page_url,
        "page_html":      page_html,
        "traceback":      error_traceback if not success else "",
        "screenshot_b64": screenshot_b64,
    }


# ── Message handlers ──────────────────────────────────────────────────────────

async def _handle_run_job(msg: dict, outbox: asyncio.Queue):
    job = msg["job"]
    job_id = job["id"]

    # Notify VPS that the job has started
    await outbox.put(json.dumps({"type": "job_start", "job_id": job_id}))

    # Run blocking job in a thread; result goes back through outbox
    result = await asyncio.to_thread(_execute_job_sync, job)
    await outbox.put(json.dumps(result))


async def _handle_cancel_job(msg: dict, outbox: asyncio.Queue):
    job_id = msg.get("job_id")
    if job_id is None:
        return
    with _cancel_lock:
        flag = _cancel_flags.get(job_id)
    if flag:
        flag.set()
        print(f"[JOB {job_id}] Cancel flag set — job will stop at next checkpoint")
    else:
        # Job not running locally — just acknowledge to VPS
        await outbox.put(json.dumps({"type": "job_cancelled", "job_id": job_id,
                                     "success": False, "message": "Cancelado (job não ativo)"}))


async def _handle_open_browser(msg: dict, outbox: asyncio.Queue):
    profile_id = msg.get("profile_id", "")
    cmd_id     = msg.get("cmd_id")
    try:
        await asyncio.to_thread(_client.open_browser, profile_id)
        print(f"[CMD] Browser aberto para {profile_id}")
    except Exception as e:
        print(f"[CMD] Erro ao abrir browser: {e}")
    if cmd_id is not None:
        await outbox.put(json.dumps({"type": "command_done", "cmd_id": cmd_id}))


async def _receiver(ws, outbox: asyncio.Queue):
    """Read messages from VPS and dispatch handlers as tasks."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue

        msg_type = msg.get("type", "")

        if msg_type == "run_job":
            asyncio.create_task(_handle_run_job(msg, outbox))
        elif msg_type == "cancel_job":
            asyncio.create_task(_handle_cancel_job(msg, outbox))
        elif msg_type == "open_browser":
            asyncio.create_task(_handle_open_browser(msg, outbox))
        elif msg_type == "sync_request":
            asyncio.create_task(_sync_profiles(outbox))
        # Other types (e.g. server-side pings) are silently ignored


async def _sender(ws, outbox: asyncio.Queue):
    """Drain outbox and send frames to VPS."""
    while True:
        msg = await outbox.get()
        if msg is None:   # sentinel: shut down
            break
        await ws.send(msg)


async def _periodic_sync(outbox: asyncio.Queue, interval: int):
    """Re-sync profiles every *interval* seconds."""
    while True:
        await asyncio.sleep(interval)
        print("[SYNC] Sync periódico de perfis…")
        await _sync_profiles(outbox)


# ── Main connection loop ──────────────────────────────────────────────────────

async def connect_and_run(vps_url: str, api_key: str, sync_interval: int):
    ws_url = (
        vps_url.rstrip("/")
        .replace("http://", "ws://")
        .replace("https://", "wss://")
    ) + f"/agent/ws?key={api_key}"

    base_url = ws_url[:ws_url.index("?")]
    print(f"[AGENT] Conectando ao VPS: {base_url}")

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                print("[AGENT] Conectado! Sincronizando perfis…")

                outbox: asyncio.Queue = asyncio.Queue()

                # Sync profiles immediately on connect
                await _sync_profiles(outbox)

                # Start background tasks
                sync_task   = asyncio.create_task(_periodic_sync(outbox, sync_interval))
                sender_task = asyncio.create_task(_sender(ws, outbox))
                recv_task   = asyncio.create_task(_receiver(ws, outbox))

                try:
                    # Wait until the receive loop ends (connection closed)
                    await recv_task
                finally:
                    sync_task.cancel()
                    # Drain sender then shut it down
                    await outbox.put(None)
                    await sender_task

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            print(f"[AGENT] Desconectado: {e}. Reconectando em 5s…")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[AGENT] Erro inesperado: {e}. Reconectando em 10s…")
            await asyncio.sleep(10)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Verificador local agent")
    parser.add_argument(
        "--vps", default=os.getenv("VPS_URL", ""),
        help="VPS base URL (ex: http://1.2.3.4:5001)",
    )
    parser.add_argument(
        "--key", default=os.getenv("WORKER_API_KEY", ""),
        help="Worker API key",
    )
    parser.add_argument(
        "--sync-interval", type=int, default=60,
        help="Segundos entre syncs periódicos de perfis (padrão: 60)",
    )
    args = parser.parse_args()

    if not args.vps:
        print("ERRO: Informe --vps http://SEU_VPS_IP:5001")
        sys.exit(1)
    if not args.key:
        print("ERRO: Informe --key SUA_CHAVE_SECRETA")
        sys.exit(1)

    # Expose to child imports (main.py uses these to pick GeradorRemoteClient)
    os.environ.setdefault("VPS_URL", args.vps)
    os.environ.setdefault("WORKER_API_KEY", args.key)

    print(f"[AGENT] VPS: {args.vps}")
    print(f"[AGENT] Sync a cada {args.sync_interval}s")
    print(f"[AGENT] AdsPower: {verif_config.ADSPOWER_BASE}")
    print()

    asyncio.run(connect_and_run(args.vps, args.key, args.sync_interval))


if __name__ == "__main__":
    main()
