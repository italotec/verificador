"""
Local worker — runs on the Windows machine where AdsPower is installed.

Responsibilities:
  1. Periodically sync AdsPower profiles to the VPS Flask app.
  2. Poll the VPS for queued verification jobs and execute them locally.
  3. Execute open-browser commands queued by the VPS.

Usage:
    python worker.py --vps http://YOUR_VPS_IP:5001 --key YOUR_WORKER_API_KEY

Environment variables (alternative to CLI flags):
    VPS_URL          VPS base URL
    WORKER_API_KEY   Shared secret key
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

import requests as _requests

# ── Project imports ──────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import config as verif_config
from services.adspower import AdsPowerClient

_client = AdsPowerClient(verif_config.ADSPOWER_BASE)

# ── Global state (set in main()) ─────────────────────────────────────────────
VPS_URL = ""
HEADERS = {}


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs):
    url = f"{VPS_URL}{path}"
    r = _requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Profile sync ─────────────────────────────────────────────────────────────

def sync_profiles():
    """Push the current AdsPower profile list to the VPS."""
    try:
        group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
        name_to_id = {g["group_name"]: str(g["group_id"]) for g in group_data.get("list", [])}

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

        result = _api("POST", "/worker/profiles/push", json={"profiles": profiles})
        print(f"[SYNC] {result.get('count', 0)} perfis sincronizados com o VPS")
    except Exception as e:
        print(f"[SYNC] Falha: {e}")


# ── Screenshot capture ────────────────────────────────────────────────────────

def _capture_screenshot_b64(since_epoch: float) -> str:
    """Find the most recent debug screenshot taken since *since_epoch* and
    return it as a base64-encoded PNG string."""
    debug_dir = Path(verif_config.DEBUG_DIR)
    if not debug_dir.exists():
        return ""
    candidates = [p for p in debug_dir.rglob("*.png") if p.stat().st_mtime >= since_epoch]
    if not candidates:
        candidates = list(debug_dir.rglob("*.png"))
    if not candidates:
        return ""
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return base64.b64encode(latest.read_bytes()).decode()


# ── Job execution ─────────────────────────────────────────────────────────────

def _parse_gerador_block(remark: str) -> dict | None:
    marker = verif_config.GERADOR_REMARK_MARKER
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


def execute_job(job: dict):
    job_id     = job["id"]
    profile_id = job["profile_id"]
    business_id = job.get("business_id", "")

    print(f"[JOB {job_id}] Iniciando para perfil {profile_id}…")

    # Mark as running on VPS
    try:
        _api("POST", f"/worker/jobs/{job_id}/start")
    except Exception as e:
        print(f"[JOB {job_id}] Erro ao marcar como running: {e}")
        return

    success = False
    message = ""

    try:
        from main import _run_for_profile, _mark_verified, _acquire_run_id

        # Fetch profile from local AdsPower
        profile = _client.get_profile(profile_id)

        # Resolve run_id
        remark      = profile.get("remark", "")
        gerador_data = _parse_gerador_block(remark)
        run_id      = gerador_data.get("run_id") if gerador_data else None
        email_mode  = gerador_data.get("email_mode", "own") if gerador_data else "own"

        if run_id is None:
            print(f"[JOB {job_id}] Sem run_id no remark — adquirindo do Gerador…")
            run_id = _acquire_run_id()
            if run_id is None:
                raise RuntimeError("Gerador não retornou um run_id válido.")
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
            business_id=business_id,
            gerador_data=gerador_data or {},
        )

        if success:
            _mark_verified(profile_id)
            message = "Verificação concluída com sucesso!"
        else:
            message = "Verificação falhou."

        screenshot_b64 = _capture_screenshot_b64(since_epoch=start_time)

    except Exception as e:
        message        = str(e)[:500]
        screenshot_b64 = ""
        print(f"[JOB {job_id}] Exceção: {e}")

    # Report result to VPS
    try:
        _api("POST", f"/worker/jobs/{job_id}/done", json={
            "success":        success,
            "message":        message,
            "screenshot_b64": screenshot_b64,
        })
        print(f"[JOB {job_id}] {'✓ Sucesso' if success else '✗ Falha'} — resultado enviado ao VPS")
    except Exception as e:
        print(f"[JOB {job_id}] Erro ao reportar resultado: {e}")


def poll_jobs():
    try:
        data = _api("GET", "/worker/jobs/next")
        job  = data.get("job")
        if job:
            execute_job(job)
    except Exception as e:
        print(f"[JOBS] Erro ao consultar fila: {e}")


# ── Open-browser commands ─────────────────────────────────────────────────────

def poll_commands():
    try:
        data = _api("GET", "/worker/commands/next")
        cmd  = data.get("command")
        if not cmd:
            return

        cmd_id      = cmd["id"]
        cmd_type    = cmd["command_type"]
        profile_id  = cmd["profile_id"]

        if cmd_type == "open_browser":
            try:
                _client.open_browser(profile_id)
                print(f"[CMD {cmd_id}] Browser aberto para {profile_id}")
            except Exception as e:
                print(f"[CMD {cmd_id}] Erro ao abrir browser: {e}")

        _api("POST", f"/worker/commands/{cmd_id}/done")
    except Exception as e:
        print(f"[CMDS] Erro ao consultar comandos: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    global VPS_URL, HEADERS

    import os
    parser = argparse.ArgumentParser(description="Verificador local worker")
    parser.add_argument("--vps",  default=os.getenv("VPS_URL",        ""), help="VPS base URL (ex: http://1.2.3.4:5001)")
    parser.add_argument("--key",  default=os.getenv("WORKER_API_KEY", ""), help="Worker API key")
    parser.add_argument("--sync-interval", type=int, default=30,  help="Segundos entre cada sync de perfis (padrão: 30)")
    parser.add_argument("--poll-interval", type=int, default=5,   help="Segundos entre cada poll de jobs (padrão: 5)")
    args = parser.parse_args()

    if not args.vps:
        print("ERRO: Informe --vps http://SEU_VPS_IP:5001")
        sys.exit(1)
    if not args.key:
        print("ERRO: Informe --key SUA_CHAVE_SECRETA")
        sys.exit(1)

    VPS_URL = args.vps.rstrip("/")
    HEADERS = {"X-Worker-Key": args.key, "Content-Type": "application/json"}

    print(f"[WORKER] VPS: {VPS_URL}")
    print(f"[WORKER] Sync a cada {args.sync_interval}s | Poll a cada {args.poll_interval}s")
    print(f"[WORKER] AdsPower: {verif_config.ADSPOWER_BASE}")
    print()

    last_sync = 0.0
    while True:
        now = time.time()

        # Sync profiles periodically
        if now - last_sync >= args.sync_interval:
            sync_profiles()
            last_sync = time.time()

        # Check for open-browser commands
        poll_commands()

        # Check for queued verification jobs
        poll_jobs()

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
