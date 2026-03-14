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
  {"type": "run_job",       "job": {"id", "profile_id", "business_id"}}
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
    """Return the User whose agent_token matches the ?token= query param."""
    token = request.args.get("token", "").strip()
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
    # "ping" → silently ignored


def _handle_profiles_push(app, user_id: int, profiles: list):
    with app.app_context():
        incoming_ids = {p["profile_id"] for p in profiles}

        for p in profiles:
            snap = db.session.get(ProfileSnapshot, p["profile_id"])
            if snap is None:
                snap = ProfileSnapshot(profile_id=p["profile_id"])
                db.session.add(snap)
            snap.name       = p.get("name", "")
            snap.group_name = p.get("group_name", "")
            snap.remark     = p.get("remark", "")
            snap.synced_at  = datetime.utcnow()
            snap.user_id    = user_id      # tag profile with its owner

        # Remove this user's profiles that are no longer in AdsPower
        for old in ProfileSnapshot.query.filter_by(user_id=user_id).all():
            if old.profile_id not in incoming_ids:
                db.session.delete(old)

        db.session.commit()
        log_event("info", "agent", f"{len(profiles)} perfis sincronizados", user_id=user_id)
        print(f"[AGENT WS] user_id={user_id}: {len(profiles)} perfis sincronizados")


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
        job = db.session.get(VerifyJob, job_id)
        if not job:
            return

        job.status       = "success" if msg.get("success") else "error"
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

        db.session.commit()
        success = msg.get("success")
        status_word = "✓ sucesso" if success else "✗ falha"
        log_event(
            "info" if success else "error", "job",
            f"Job concluído: {status_word}",
            detail=msg.get("message", ""),
            user_id=job.user_id, profile_id=job.profile_id, job_id=job_id,
        )
        print(f"[AGENT WS] Job {job_id}: {status_word}")


def _handle_command_done(app, cmd_id):
    if cmd_id is None:
        return
    with app.app_context():
        cmd = db.session.get(WorkerCommand, cmd_id)
        if cmd:
            cmd.status = "done"
            db.session.commit()


# ── WebSocket handler (registered via sock.route in __init__.py) ──────────────

def handle_ws(ws):
    """
    Called by:
        @sock.route("/agent/ws")
        def agent_ws_route(ws): handle_ws(ws)
    """
    user = _auth_user()
    if not user:
        log_event("warning", "agent", "Conexão WS rejeitada: token inválido")
        ws.close()
        return

    user_id  = user.id
    username = user.username
    app      = current_app._get_current_object()

    session = AgentSession(user_id=user_id, username=username, ws=ws)

    with _registry_lock:
        # Disconnect any previous session for this user
        old = _agents.get(user_id)
        if old:
            try:
                old.ws.close()
            except Exception:
                pass
            old.send_queue.put(None)   # stop old sender thread

        # Clear stale messages
        while not session.send_queue.empty():
            try:
                session.send_queue.get_nowait()
            except queue.Empty:
                break

        _agents[user_id] = session

    log_event("info", "agent", f"Agent conectado: '{username}'", user_id=user_id)
    print(f"[AGENT WS] '{username}' (id={user_id}) conectado")

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
            except Exception:
                break

    session.sender_thread = threading.Thread(target=_sender, daemon=True)
    session.sender_thread.start()

    # ── Flush pending queued jobs for this user's profiles ────────────────────
    with app.app_context():
        # Find profile_ids owned by this user
        owned_ids = {
            s.profile_id
            for s in ProfileSnapshot.query.filter_by(user_id=user_id).all()
        }
        if owned_ids:
            pending = (
                VerifyJob.query
                .filter(VerifyJob.status == "queued",
                        VerifyJob.profile_id.in_(owned_ids))
                .order_by(VerifyJob.created_at.asc())
                .all()
            )
            for job in pending:
                session.send_queue.put(json.dumps({
                    "type": "run_job",
                    "job": {
                        "id":          job.id,
                        "profile_id":  job.profile_id,
                        "business_id": job.business_id or "",
                    },
                }))

    # ── Receive loop ──────────────────────────────────────────────────────────
    try:
        while True:
            try:
                data = ws.receive(timeout=120)
            except TimeoutError:
                # No application-level message in 120s — send a server ping
                # to check the connection is alive, then keep waiting.
                try:
                    ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue
            if data is None:
                break
            _handle_agent_message(app, user_id, data)
    except Exception as e:
        with app.app_context():
            log_event("error", "agent", f"Erro na conexão: '{username}'", detail=str(e), user_id=user_id)
        print(f"[AGENT WS] '{username}': erro na conexão: {e}")
    finally:
        with _registry_lock:
            if _agents.get(user_id) is session:
                del _agents[user_id]
        session.send_queue.put(None)   # stop sender thread
        with app.app_context():
            log_event("info", "agent", f"Agent desconectado: '{username}'", user_id=user_id)
        print(f"[AGENT WS] '{username}' desconectado")


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
