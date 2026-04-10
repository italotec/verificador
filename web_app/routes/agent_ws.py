"""
Agent WebSocket endpoint — per-user agent registry.

Each user has a unique agent_token (shown on their /account page).
Their local agent_gui.exe connects here using that token, so the VPS
knows exactly which agent belongs to which user.

Agent → VPS frames:
  {"type": "profiles_push",  "profiles": [...]}
  {"type": "job_start",      "job_id": N}
  {"type": "job_progress",   "job_id": N, "message": str}
  {"type": "job_done",       "job_id": N, "success": bool, "message": str, "screenshot_b64": str}
  {"type": "command_done",   "cmd_id": N}
  {"type": "ping"}

VPS → Agent frames:
  {"type": "run_job",       "job": {"id", "profile_id", "business_id", "sms": {provider, api_key, country, service}}}
  {"type": "open_browser",  "profile_id": str, "cmd_id": N|null}
  {"type": "sync_request"}
"""
import base64
import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user
from .. import db
from ..models import ProfileSnapshot, VerifyJob, WorkerCommand, User, log_event

bp = Blueprint("agent_ws", __name__, url_prefix="/agent")


# ── Per-user agent registry ───────────────────────────────────────────────────

@dataclass
class AgentSession:
    user_id:   int
    username:  str
    ws:        object                          # flask_sock WS
    send_queue: queue.Queue = field(default_factory=queue.Queue)
    sender_thread: object = None


_registry_lock = threading.Lock()
_agents: dict[int, AgentSession] = {}         # keyed by user_id


def is_agent_connected(user_id: int) -> bool:
    return user_id in _agents


def push_to_agent(user_id: int, msg: dict) -> bool:
    """
    Thread-safe: put a JSON message in the named agent's send queue.
    Returns True if that user's agent is currently connected.
    """
    session = _agents.get(user_id)
    if not session:
        return False
    session.send_queue.put(json.dumps(msg))
    return True


def agent_user_id_for_profile(profile_id: str) -> int | None:
    """Look up which user's agent owns a given profile_id."""
    snap = ProfileSnapshot.query.filter_by(profile_id=profile_id).first()
    return snap.user_id if snap else None


# ── Auth: resolve user from token ─────────────────────────────────────────────

def _auth_user() -> User | None:
    """Return the User whose agent_token matches the ?token= or ?key= query param."""
    token = (
        request.args.get("token", "").strip()
        or request.args.get("key", "").strip()
    )
    if not token:
        return None
    return User.query.filter_by(agent_token=token).first()


# ── Incoming message handlers ─────────────────────────────────────────────────

def _handle_agent_message(app, user_id: int, data: str):
    try:
        msg = json.loads(data)
    except Exception:
        return

    msg_type = msg.get("type", "")

    if msg_type == "profiles_push":
        _handle_profiles_push(app, user_id, msg.get("profiles", []))
    elif msg_type == "job_start":
        _handle_job_start(app, msg.get("job_id"))
    elif msg_type == "job_done":
        _handle_job_done(app, msg)
    elif msg_type == "job_progress":
        _handle_job_progress(app, msg.get("job_id"), msg.get("message", ""))
    elif msg_type == "command_done":
        _handle_command_done(app, msg.get("cmd_id"))
    elif msg_type == "job_cancelled":
        _handle_job_cancelled(app, msg.get("job_id"))
    # "ping" → silently ignored


def _handle_profiles_push(app, user_id: int, profiles: list):
    from ..models import WabaRecord, WABA_STATUS_AGUARDANDO, WABA_STATUS_EM_REVISAO
    with app.app_context():
        incoming_ids = {p["profile_id"] for p in profiles}

        new_wabas = 0
        for p in profiles:
            snap = db.session.get(ProfileSnapshot, p["profile_id"])
            if snap is None:
                snap = ProfileSnapshot(profile_id=p["profile_id"])
                db.session.add(snap)
            snap.name       = p.get("name", "")
            snap.group_name = p.get("group_name", "")
            snap.remark     = p.get("remark", "")
            snap.synced_at  = datetime.utcnow()
            snap.user_id    = user_id

            # Auto-create a WabaRecord for this profile if one doesn't exist yet
            existing = WabaRecord.query.filter_by(profile_id=p["profile_id"]).first()
            if existing is None:
                group = p.get("group_name", "")
                status = WABA_STATUS_EM_REVISAO if group == "Verificadas" else WABA_STATUS_AGUARDANDO
                waba = WabaRecord(
                    profile_id=p["profile_id"],
                    user_id=user_id,
                    waba_name=p.get("name", ""),
                    status=status,
                )
                db.session.add(waba)
                new_wabas += 1

        # Remove this user's profiles that are no longer in AdsPower
        for old in ProfileSnapshot.query.filter_by(user_id=user_id).all():
            if old.profile_id not in incoming_ids:
                db.session.delete(old)

        db.session.commit()
        log_event("info", "agent", f"{len(profiles)} perfis sincronizados, {new_wabas} WABAs criadas", user_id=user_id)
        print(f"[AGENT WS] user_id={user_id}: {len(profiles)} perfis sincronizados, {new_wabas} novas WABAs")


def _handle_job_progress(app, job_id, message: str):
    if job_id is None:
        return
    with app.app_context():
        job = db.session.get(VerifyJob, job_id)
        if job:
            job.last_message = message
            db.session.commit()


def _handle_job_start(app, job_id):
    if job_id is None:
        return
    with app.app_context():
        job = db.session.get(VerifyJob, job_id)
        if job:
            job.status     = "running"
            job.started_at = datetime.utcnow()
            db.session.commit()
            log_event("info", "job", f"Job iniciado pelo agent", user_id=job.user_id, profile_id=job.profile_id, job_id=job_id)


def _handle_job_done(app, msg: dict):
    job_id = msg.get("job_id")
    if job_id is None:
        return
    with app.app_context():
        from ..models import WabaRecord, WABA_STATUS_EM_REVISAO, WABA_STATUS_ERRO
        job = db.session.get(VerifyJob, job_id)
        if not job:
            return

        success = msg.get("success")
        job.status       = "success" if success else "error"
        job.last_message = msg.get("message", "")
        job.finished_at  = datetime.utcnow()

        screenshot_b64 = msg.get("screenshot_b64", "")
        if screenshot_b64:
            try:
                screenshots_dir = Path(app.static_folder) / "screenshots"
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                dest = screenshots_dir / f"{job.profile_id}.png"
                dest.write_bytes(base64.b64decode(screenshot_b64))
                job.screenshot_path = f"{job.profile_id}.png"
            except Exception:
                pass

        # Update linked WabaRecord status
        waba = (
            db.session.get(WabaRecord, job.waba_record_id)
            if job.waba_record_id
            else WabaRecord.query.filter_by(profile_id=job.profile_id).first()
        )
        if waba:
            if success:
                waba.status = WABA_STATUS_EM_REVISAO
            else:
                error_msg = msg.get("message", "")
                waba.last_error = error_msg
                _bm_restricted = (
                    "portfólio bloqueado para anúncios" in error_msg
                    or "business portfolio to advertise" in error_msg
                )
                if _bm_restricted:
                    from ..models import WABA_STATUS_RESTRITA
                    from services.status_manager import StatusManager
                    StatusManager.transition(waba, WABA_STATUS_RESTRITA, reason=error_msg, force=True)
                else:
                    waba.status = WABA_STATUS_ERRO
                    waba.error_count = (waba.error_count or 0) + 1

        db.session.commit()

        # Create ErrorReport for failed jobs so the /errors admin page shows them
        if not success:
            try:
                from services.error_analyzer import analyze_error
                analyze_error(
                    waba_record_id=waba.id if waba else None,
                    job_id=job_id,
                    error_type="AgentJobFailed",
                    error_message=msg.get("message", ""),
                    screenshot_path=job.screenshot_path,
                    step_name=msg.get("step_name") or "agent_verify",
                    page_url=msg.get("page_url") or None,
                    traceback_str=msg.get("traceback") or None,
                    page_html=msg.get("page_html") or None,
                )
            except Exception as _e:
                print(f"[AGENT WS] Erro ao criar ErrorReport: {_e}")

        status_word = "✓ sucesso" if success else "✗ falha"
        log_event(
            "info" if success else "error", "job",
            f"Job concluído: {status_word}",
            detail=msg.get("message", ""),
            user_id=job.user_id, profile_id=job.profile_id, job_id=job_id,
        )
        print(f"[AGENT WS] Job {job_id}: {status_word}")


def _handle_job_cancelled(app, job_id):
    if job_id is None:
        return
    with app.app_context():
        from ..models import WabaRecord, WABA_STATUS_AGUARDANDO
        job = db.session.get(VerifyJob, job_id)
        if job:
            job.status      = "error"
            job.last_message = "Cancelado manualmente"
            job.finished_at = datetime.utcnow()
            waba = (
                db.session.get(WabaRecord, job.waba_record_id)
                if job.waba_record_id
                else WabaRecord.query.filter_by(profile_id=job.profile_id).first()
            )
            if waba and waba.status == "executando":
                waba.status = WABA_STATUS_AGUARDANDO
            db.session.commit()
            print(f"[AGENT WS] Job {job_id} cancelado manualmente")


def _handle_command_done(app, cmd_id):
    if cmd_id is None:
        return
    with app.app_context():
        cmd = db.session.get(WorkerCommand, cmd_id)
        if cmd:
            cmd.status = "done"
            db.session.commit()


def _sms_payload() -> dict:
    """
    Read SMS provider + credentials from SystemSetting (DB) so the VPS can
    inject them into every run_job message.  The agent has no access to the
    VPS database, so the only reliable way to pass settings is in the payload.
    """
    import config as verif_config
    from ..models import SystemSetting

    provider = SystemSetting.get("SMS_PROVIDER", verif_config.SMS_PROVIDER) or "sms24h"

    if provider == "herosms":
        return {
            "provider": "herosms",
            "api_key":  SystemSetting.get("HEROSMS_API_KEY", verif_config.HEROSMS_API_KEY),
            "country":  SystemSetting.get("HEROSMS_COUNTRY", verif_config.HEROSMS_COUNTRY),
            "service":  SystemSetting.get("HEROSMS_SERVICE", verif_config.HEROSMS_SERVICE),
        }
    else:
        return {
            "provider": "sms24h",
            "api_key":  SystemSetting.get("SMS24H_API_KEY", verif_config.SMS24H_API_KEY),
            "country":  SystemSetting.get("SMS24H_COUNTRY", verif_config.SMS24H_COUNTRY),
            "service":  SystemSetting.get("SMS24H_SERVICE", verif_config.SMS24H_SERVICE),
        }


def _reset_stale_jobs(app, user_id: int):
    """
    Called when an agent disconnects.  Any WabaRecord still in 'executando' and
    any VerifyJob still 'running' or 'queued' for this user are reset to
    'aguardando' so they can be re-queued on the next run.
    """
    from ..models import WabaRecord, WABA_STATUS_EXECUTANDO, WABA_STATUS_AGUARDANDO
    with app.app_context():
        try:
            owned_ids = {
                s.profile_id
                for s in ProfileSnapshot.query.filter_by(user_id=user_id).all()
            }
            if not owned_ids:
                return

            stale_jobs = (
                VerifyJob.query
                .filter(VerifyJob.status.in_(["running", "queued"]),
                        VerifyJob.profile_id.in_(owned_ids))
                .all()
            )
            stale_count = len(stale_jobs)
            for job in stale_jobs:
                job.status      = "error"
                job.last_message = "Agent desconectado — execução interrompida"
                job.finished_at = datetime.utcnow()

            stale_wabas = (
                WabaRecord.query
                .filter(WabaRecord.status == WABA_STATUS_EXECUTANDO,
                        WabaRecord.profile_id.in_(owned_ids))
                .all()
            )
            for waba in stale_wabas:
                waba.status = WABA_STATUS_AGUARDANDO

            if stale_count or stale_wabas:
                db.session.commit()
                print(f"[DBG-WS] reset {len(stale_wabas)} WABAs executando → aguardando, "
                      f"{stale_count} jobs → error (agent disconnect)")
        except Exception as e:
            print(f"[DBG-WS] _reset_stale_jobs erro: {e}")
            db.session.rollback()


# ── WebSocket handler (registered via sock.route in __init__.py) ──────────────

def handle_ws(ws):
    """
    Called by:
        @sock.route("/agent/ws")
        def agent_ws_route(ws): handle_ws(ws)
    """
    print(f"[DBG-WS] handle_ws chamado")
    user = _auth_user()
    if not user:
        print(f"[DBG-WS] FALHA AUTH — token inválido ou ausente")
        log_event("warning", "agent", "Conexão WS rejeitada: token inválido")
        ws.close()
        return

    user_id  = user.id
    username = user.username
    app      = current_app._get_current_object()
    print(f"[DBG-WS] Auth OK — user='{username}' id={user_id}")

    session = AgentSession(user_id=user_id, username=username, ws=ws)

    with _registry_lock:
        old = _agents.get(user_id)
        if old:
            print(f"[DBG-WS] Sessão anterior encontrada — encerrando")
            try:
                old.ws.close()
            except Exception:
                pass
            old.send_queue.put(None)

        while not session.send_queue.empty():
            try:
                session.send_queue.get_nowait()
            except queue.Empty:
                break

        _agents[user_id] = session

    log_event("info", "agent", f"Agent conectado: '{username}'", user_id=user_id)
    print(f"[DBG-WS] '{username}' registrado no registry")

    # ── Sender thread ─────────────────────────────────────────────────────────
    def _sender():
        while True:
            try:
                data = session.send_queue.get(timeout=1)
                if data is None:
                    break
                ws.send(data)
            except queue.Empty:
                if user_id not in _agents:
                    break
            except Exception as e:
                print(f"[DBG-WS] sender thread erro: {type(e).__name__}: {e}")
                break

    session.sender_thread = threading.Thread(target=_sender, daemon=True)
    session.sender_thread.start()
    print(f"[DBG-WS] sender thread iniciada")

    # ── Flush pending queued jobs for this user's profiles ────────────────────
    print(f"[DBG-WS] iniciando flush de jobs pendentes…")
    try:
        with app.app_context():
            owned_ids = {
                s.profile_id
                for s in ProfileSnapshot.query.filter_by(user_id=user_id).all()
            }
            print(f"[DBG-WS] owned_ids={owned_ids}")
            if owned_ids:
                pending = (
                    VerifyJob.query
                    .filter(VerifyJob.status == "queued",
                            VerifyJob.profile_id.in_(owned_ids))
                    .order_by(VerifyJob.created_at.asc())
                    .all()
                )
                sms = _sms_payload()
                for job in pending:
                    session.send_queue.put(json.dumps({
                        "type": "run_job",
                        "job": {
                            "id":          job.id,
                            "profile_id":  job.profile_id,
                            "business_id": job.business_id or "",
                            "sms":         sms,
                        },
                    }))
                print(f"[DBG-WS] {len(pending)} jobs enfileirados")
    except Exception as e:
        print(f"[DBG-WS] ERRO no flush: {type(e).__name__}: {e}")

    # ── Receive loop ──────────────────────────────────────────────────────────
    print(f"[DBG-WS] entrando no receive loop")
    try:
        while True:
            try:
                data = ws.receive(timeout=120)
            except Exception as e:
                print(f"[DBG-WS] ws.receive() lançou {type(e).__name__}: {e}")
                break
            if data is None:
                print(f"[DBG-WS] ws.receive() retornou None (timeout 120s) — enviando ping")
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception as e:
                    print(f"[DBG-WS] falha ao enviar ping: {type(e).__name__}: {e}")
                    break
                continue
            print(f"[DBG-WS] mensagem recebida: {data[:120]}")
            try:
                _handle_agent_message(app, user_id, data)
            except Exception as e:
                print(f"[DBG-WS] erro ao processar mensagem: {type(e).__name__}: {e}")
    except Exception as e:
        with app.app_context():
            log_event("error", "agent", f"Erro na conexão: '{username}'", detail=str(e), user_id=user_id)
        print(f"[DBG-WS] ERRO externo no receive loop: {type(e).__name__}: {e}")
    finally:
        with _registry_lock:
            if _agents.get(user_id) is session:
                del _agents[user_id]
        session.send_queue.put(None)
        with app.app_context():
            _reset_stale_jobs(app, user_id)
            log_event("info", "agent", f"Agent desconectado: '{username}'", user_id=user_id)
        print(f"[DBG-WS] '{username}' desconectado — handle_ws retornando")


# ── Status endpoints ──────────────────────────────────────────────────────────

@bp.route("/status")
@login_required
def agent_status():
    """Returns whether the current user's agent is connected."""
    return jsonify({"online": is_agent_connected(current_user.id)})


@bp.route("/all")
@login_required
def agent_all():
    """Admin: list all connected agents."""
    if not current_user.is_admin:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({
        "agents": [
            {"user_id": uid, "username": s.username}
            for uid, s in _agents.items()
        ]
    })
