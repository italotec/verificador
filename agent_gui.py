"""
Verificador Agent — local GUI agent.

The user pastes their personal token (from the web app's "Minha Conta" page)
and clicks Connect.  The VPS address is hardcoded below — change it before
building the .exe.

Build:
    pyinstaller --onefile --windowed --icon=prosperidadelogo.ico --name "Client Verificador" --add-data "services;services" --add-data "main.py;." --add-data "config.py;." --add-data ".env;." --add-data "accounts.json;." --add-data "proxies.json;." agent_gui.py
"""
import asyncio
import base64
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import websockets
import websockets.exceptions

# ── VPS address — CHANGE THIS before building the .exe ───────────────────────
# Use your domain (recommended) or IP.
# ws://  → plain HTTP server
# wss:// → HTTPS / TLS server (recommended for production)
_VPS_WS_BASE = "ws://38.247.136.208:5050"   # e.g. "wss://verificador.seusite.com"

# ── Local config constants (inlined so config.py is not needed in the bundle) ─
def _detect_adspower() -> str:
    """Try common AdsPower API base URLs and return the first that responds."""
    import requests as _req
    for base in (
        "http://local.adspower.net:50365",
        "http://127.0.0.1:50365",
        "http://local.adspower.net:50325",
        "http://127.0.0.1:50325",
    ):
        try:
            _req.get(f"{base}/api/v1/status", timeout=2)
            return base
        except Exception:
            continue
    return "http://local.adspower.net:50325"  # last-resort default

_ADSPOWER_BASE     = _detect_adspower()
_VERIFICAR_GROUP   = "Verificar"
_VERIFICADAS_GROUP = "Verificadas"
_GERADOR_MARKER    = "---GERADOR---"

# Debug screenshots directory — relative to the .exe location when frozen,
# or the project root when running from source.
if getattr(sys, "frozen", False):
    _DEBUG_DIR = Path(sys.executable).parent / "debug"
else:
    _DEBUG_DIR = Path(__file__).parent / "debug"

# ── AdsPower client ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from services.adspower import AdsPowerClient
_client = AdsPowerClient(_ADSPOWER_BASE)

# ── Colour palette (mirrors the web app's Tailwind theme) ────────────────────
BG       = "#09090b"   # zinc-950
CARD     = "#18181b"   # zinc-900
BORDER   = "#27272a"   # zinc-800
TEXT     = "#f4f4f5"   # zinc-100
MUTED    = "#71717a"   # zinc-500
INDIGO   = "#6366f1"   # indigo-500
INDIGO_D = "#4f46e5"   # indigo-600
GREEN    = "#34d399"   # emerald-400
RED      = "#f87171"   # red-400
YELLOW   = "#fbbf24"   # amber-400
ENTRY_BG = "#0f0f11"
FONT_UI  = ("Segoe UI", 10)
FONT_MONO= ("Consolas", 9)
FONT_BIG = ("Segoe UI", 13, "bold")
FONT_SM  = ("Segoe UI", 9)


# ── Core agent logic (async) ──────────────────────────────────────────────────

def _parse_gerador_block(remark: str) -> dict | None:
    if _GERADOR_MARKER not in remark:
        return None
    _, _, tail = remark.partition(_GERADOR_MARKER)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


def _capture_screenshot_b64(since_epoch: float) -> str:
    if not _DEBUG_DIR.exists():
        return ""
    candidates = [p for p in _DEBUG_DIR.rglob("*.png") if p.stat().st_mtime >= since_epoch]
    if not candidates:
        candidates = list(_DEBUG_DIR.rglob("*.png"))
    if not candidates:
        return ""
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return base64.b64encode(latest.read_bytes()).decode()


def _execute_job_sync(job: dict, log, progress=None) -> dict:
    """Blocking job execution — called via asyncio.to_thread."""
    job_id      = job["id"]
    profile_id  = job["profile_id"]
    business_id = job.get("business_id", "")

    def _progress(msg: str):
        log(f"[JOB {job_id}] {msg}")
        if progress:
            progress(msg)

    _progress(f"Iniciando para perfil {profile_id}…")

    success        = False
    message        = ""
    screenshot_b64 = ""

    try:
        from main import _run_for_profile, _mark_verified, _acquire_run_id
        import main as _main_mod
        _main_mod.adspower = _client  # use the auto-detected AdsPower URL

        profile      = _client.get_profile(profile_id)
        remark       = profile.get("remark", "")
        gerador_data = _parse_gerador_block(remark)
        run_id       = gerador_data.get("run_id")   if gerador_data else None
        email_mode   = gerador_data.get("email_mode", "own") if gerador_data else "own"

        if run_id is None:
            _progress("Adquirindo dados do Gerador…")
            run_id = _acquire_run_id()
            gerador_data = gerador_data or {}
            gerador_data["run_id"] = run_id

        if business_id:
            gerador_data = gerador_data or {}
            gerador_data["business_id"] = business_id

        _progress("Abrindo browser AdsPower…")
        start_time = time.time()

        _progress("Executando verificação no Facebook…")
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
        message = str(e)[:500]
        log(f"[JOB {job_id}] Exceção: {e}")

    log(f"[JOB {job_id}] {'✓ Sucesso' if success else '✗ Falha'}")
    return {
        "type":           "job_done",
        "job_id":         job_id,
        "success":        success,
        "message":        message,
        "screenshot_b64": screenshot_b64,
    }


async def _sync_profiles(outbox: asyncio.Queue, log):
    try:
        def _collect():
            group_data = _client._get("/api/v1/group/list", page=1, page_size=200)
            name_to_id = {
                g["group_name"]: str(g["group_id"])
                for g in group_data.get("list", [])
            }
            target = {_VERIFICAR_GROUP, _VERIFICADAS_GROUP}
            profiles = []
            for gname, gid in name_to_id.items():
                if gname not in target:
                    continue
                for p in _client.list_profiles(group_id=gid):
                    profiles.append({
                        "profile_id": p["user_id"],
                        "name":       p.get("name", ""),
                        "group_name": gname,
                        "remark":     p.get("remark", ""),
                    })
            return profiles

        profiles = await asyncio.to_thread(_collect)
        await outbox.put(json.dumps({"type": "profiles_push", "profiles": profiles}))
        log(f"[SYNC] {len(profiles)} perfis enviados ao VPS")
    except Exception as e:
        log(f"[SYNC] Falha: {e}")


async def _handle_run_job(msg: dict, outbox: asyncio.Queue, log):
    job    = msg["job"]
    job_id = job["id"]
    await outbox.put(json.dumps({"type": "job_start", "job_id": job_id}))

    loop = asyncio.get_event_loop()
    def progress(message: str):
        frame = json.dumps({"type": "job_progress", "job_id": job_id, "message": message})
        loop.call_soon_threadsafe(outbox.put_nowait, frame)

    result = await asyncio.to_thread(_execute_job_sync, job, log, progress)
    await outbox.put(json.dumps(result))


async def _handle_open_browser(msg: dict, outbox: asyncio.Queue, log):
    profile_id = msg.get("profile_id", "")
    cmd_id     = msg.get("cmd_id")
    try:
        await asyncio.to_thread(_client.open_browser, profile_id)
        log(f"[CMD] Browser aberto para {profile_id}")
    except Exception as e:
        log(f"[CMD] Erro ao abrir browser: {e}")
    if cmd_id is not None:
        await outbox.put(json.dumps({"type": "command_done", "cmd_id": cmd_id}))


async def _receiver(ws, outbox: asyncio.Queue, log, stop: asyncio.Event):
    async for raw in ws:
        if stop.is_set():
            break
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        t = msg.get("type", "")
        if t == "run_job":
            asyncio.create_task(_handle_run_job(msg, outbox, log))
        elif t == "open_browser":
            asyncio.create_task(_handle_open_browser(msg, outbox, log))
        elif t == "sync_request":
            asyncio.create_task(_sync_profiles(outbox, log))


async def _sender(ws, outbox: asyncio.Queue):
    while True:
        msg = await outbox.get()
        if msg is None:
            break
        await ws.send(msg)


async def _periodic_sync(outbox: asyncio.Queue, log, stop: asyncio.Event):
    while not stop.is_set():
        await asyncio.sleep(60)
        if stop.is_set():
            break
        log("[SYNC] Sync periódico…")
        await _sync_profiles(outbox, log)


async def connect_loop(ws_url: str, log, on_status, stop: asyncio.Event):
    while not stop.is_set():
        try:
            log("[AGENT] Conectando…")
            on_status("connecting")
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                log("[AGENT] Conectado!")
                on_status("online")

                outbox  = asyncio.Queue()
                stop_ev = asyncio.Event()

                await _sync_profiles(outbox, log)

                sync_task   = asyncio.create_task(_periodic_sync(outbox, log, stop_ev))
                sender_task = asyncio.create_task(_sender(ws, outbox))
                recv_task   = asyncio.create_task(_receiver(ws, outbox, log, stop_ev))

                while not stop.is_set():
                    if recv_task.done():
                        break
                    await asyncio.sleep(0.5)

                stop_ev.set()
                recv_task.cancel()
                sync_task.cancel()
                await outbox.put(None)
                await sender_task

        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidHandshake,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            log(f"[AGENT] Desconectado: {e}")
        except Exception as e:
            log(f"[AGENT] Erro inesperado: {e}")
        finally:
            on_status("offline")

        if not stop.is_set():
            log("[AGENT] Reconectando em 5s…")
            for _ in range(50):
                if stop.is_set():
                    break
                await asyncio.sleep(0.1)

    log("[AGENT] Encerrado.")
    on_status("offline")


# ── Tkinter GUI ───────────────────────────────────────────────────────────────

class AgentApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Verificador Agent")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.geometry("480x480")

        try:
            self.root.iconbitmap(ROOT / "prosperidadelogo.ico")
        except Exception:
            pass

        self._log_queue:  queue.Queue = queue.Queue()
        self._stop_event: threading.Event = threading.Event()
        self._loop:       asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._thread:     threading.Thread | None = None

        self._build_ui()
        self.root.after(100, self._poll_logs)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self.root, bg=CARD, pady=14)
        hdr.pack(fill="x")

        logo_frame = tk.Frame(hdr, bg=CARD)
        logo_frame.pack(padx=20)

        logo_box = tk.Frame(logo_frame, bg=INDIGO, width=38, height=38)
        logo_box.pack(side="left")
        logo_box.pack_propagate(False)
        tk.Label(logo_box, text="V", bg=INDIGO, fg=TEXT,
                 font=("Segoe UI", 14, "bold")).place(relx=.5, rely=.5, anchor="center")

        title_frame = tk.Frame(logo_frame, bg=CARD)
        title_frame.pack(side="left", padx=(10, 0))
        tk.Label(title_frame, text="Verificador Agent", bg=CARD, fg=TEXT,
                 font=FONT_BIG, anchor="w").pack(anchor="w")
        tk.Label(title_frame, text="dark • clean • by day", bg=CARD, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(anchor="w")

        # Form — token only
        form = tk.Frame(self.root, bg=BG, padx=20, pady=20)
        form.pack(fill="x")

        tk.Label(form, text="Token", bg=BG, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 4))
        tk.Label(form,
                 text="Copie em: Verificador Web → Minha Conta → Token do Agent",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(0, 6))

        token_frame = tk.Frame(form, bg=BORDER)
        token_frame.pack(fill="x", pady=(0, 18))

        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(
            token_frame, textvariable=self.token_var,
            bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=FONT_MONO, bd=8, show="●",
        )
        self.token_entry.pack(fill="x", side="left", expand=True, ipady=2)

        self._show_token = False
        tk.Button(
            token_frame, text="👁", bg=ENTRY_BG, fg=MUTED,
            relief="flat", font=FONT_SM, bd=0, cursor="hand2",
            command=self._toggle_token,
        ).pack(side="right", padx=4)

        # Status + Connect
        ctrl = tk.Frame(self.root, bg=BG, padx=20)
        ctrl.pack(fill="x")

        status_frame = tk.Frame(ctrl, bg=CARD, padx=12, pady=8)
        status_frame.pack(side="left", fill="y")

        self.dot_canvas = tk.Canvas(status_frame, width=10, height=10,
                                    bg=CARD, highlightthickness=0)
        self.dot_canvas.pack(side="left", padx=(0, 6))
        self._dot = self.dot_canvas.create_oval(1, 1, 9, 9, fill=MUTED, outline="")

        self.status_label = tk.Label(status_frame, text="Desconectado",
                                     bg=CARD, fg=MUTED, font=FONT_UI)
        self.status_label.pack(side="left")

        self.connect_btn = tk.Button(
            ctrl, text="Conectar",
            bg=INDIGO, fg=TEXT, activebackground=INDIGO_D, activeforeground=TEXT,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=20, pady=8, cursor="hand2",
            command=self._on_connect_click,
        )
        self.connect_btn.pack(side="right")

        # Log
        log_outer = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        log_outer.pack(fill="both", expand=True)

        tk.Label(log_outer, text="Log", bg=BG, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(fill="x", pady=(0, 6))

        log_frame = tk.Frame(log_outer, bg=CARD)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame, bg=CARD, fg=TEXT, insertbackground=TEXT,
            font=FONT_MONO, relief="flat", state="disabled",
            wrap="word", bd=8,
        )
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview,
                          bg=BORDER, troughcolor=CARD, relief="flat")
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        self.log_text.tag_configure("ok",    foreground=GREEN)
        self.log_text.tag_configure("err",   foreground=RED)
        self.log_text.tag_configure("warn",  foreground=YELLOW)
        self.log_text.tag_configure("info",  foreground=TEXT)
        self.log_text.tag_configure("muted", foreground=MUTED)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _toggle_token(self):
        self._show_token = not self._show_token
        self.token_entry.config(show="" if self._show_token else "●")

    def _set_status(self, state: str):
        cfg = {
            "online":     (GREEN,  "Online"),
            "offline":    (MUTED,  "Desconectado"),
            "connecting": (YELLOW, "Conectando…"),
        }
        colour, label = cfg.get(state, (MUTED, state))
        self.root.after(0, lambda: self._apply_status(colour, label, state))

    def _apply_status(self, colour: str, label: str, state: str):
        self.dot_canvas.itemconfig(self._dot, fill=colour)
        self.status_label.config(text=label, fg=colour)
        if state == "online":
            self.connect_btn.config(text="Desconectar",
                                    bg="#7f1d1d", activebackground="#991b1b")
        else:
            self.connect_btn.config(text="Conectar",
                                    bg=INDIGO, activebackground=INDIGO_D)

    def log(self, msg: str):
        self._log_queue.put(msg)

    def _poll_logs(self):
        while not self._log_queue.empty():
            self._append_log(self._log_queue.get_nowait())
        self.root.after(100, self._poll_logs)

    def _append_log(self, msg: str):
        ml  = msg.lower()
        tag = "info"
        if "✓" in msg or "sucesso" in ml or "conectado" in ml:
            tag = "ok"
        elif "✗" in msg or "erro" in ml or "falha" in ml or "exceção" in ml:
            tag = "err"
        elif "reconectando" in ml or "desconectado" in ml:
            tag = "warn"
        elif msg.startswith("[SYNC]") or "periódico" in ml:
            tag = "muted"

        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ── Connection ────────────────────────────────────────────────────────────

    def _on_connect_click(self):
        if self._thread and self._thread.is_alive():
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        token = self.token_var.get().strip()
        if not token:
            self._append_log("[ERRO] Cole seu token antes de conectar.")
            return

        ws_url = f"{_VPS_WS_BASE.rstrip('/')}/agent/ws?token={token}"

        self._stop_event.clear()
        self.token_entry.config(state="disabled")
        self._thread = threading.Thread(
            target=self._run_loop, args=(ws_url,), daemon=True
        )
        self._thread.start()

    def _disconnect(self):
        self._stop_event.set()
        if self._async_stop and self._loop:
            self._loop.call_soon_threadsafe(self._async_stop.set)
        self.token_entry.config(state="normal")

    def _run_loop(self, ws_url: str):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._async_stop = asyncio.Event()
        try:
            self._loop.run_until_complete(
                connect_loop(
                    ws_url=ws_url,
                    log=self.log,
                    on_status=self._set_status,
                    stop=self._async_stop,
                )
            )
        finally:
            self._loop.close()
            self._loop = None
            self._async_stop = None
            self.root.after(0, lambda: self.token_entry.config(state="normal"))

    def _on_close(self):
        self._disconnect()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    AgentApp().run()
