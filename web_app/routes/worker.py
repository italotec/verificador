"""
Worker API — authenticated endpoints used only by the local worker.py process.
The local machine (running AdsPower) calls these to sync profiles,
pick up queued jobs, and post results back.
"""
import base64
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app
from .. import db
from ..models import ProfileSnapshot, VerifyJob, WorkerCommand, log_event

bp = Blueprint("worker", __name__, url_prefix="/worker")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        from ..config import Config
        key = request.headers.get("X-Worker-Key", "")
        if not key or key != Config.WORKER_API_KEY:
            log_event("warning", "worker", f"Auth falhou: X-Worker-Key inválido de {request.remote_addr}")
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Profile sync ──────────────────────────────────────────────────────────────

@bp.route("/profiles/push", methods=["POST"])
@_require_key
def push_profiles():
    """
    Worker pushes its current snapshot of AdsPower profiles.
    Body: {"profiles": [{"profile_id", "name", "group_name", "remark"}, ...]}
    Replaces the entire snapshot table.
    """
    data = request.get_json(force=True) or {}
    profiles = data.get("profiles", [])

    incoming_ids = {p["profile_id"] for p in profiles}

    # Upsert incoming profiles
    for p in profiles:
        snap = db.session.get(ProfileSnapshot, p["profile_id"])
        if snap is None:
            snap = ProfileSnapshot(profile_id=p["profile_id"])
            db.session.add(snap)
        snap.name       = p.get("name", "")
        snap.group_name = p.get("group_name", "")
        snap.remark     = p.get("remark", "")
        snap.synced_at  = datetime.utcnow()

    # Remove profiles no longer present in AdsPower
    for old in ProfileSnapshot.query.all():
        if old.profile_id not in incoming_ids:
            db.session.delete(old)

    db.session.commit()
    log_event("info", "worker", f"Worker sincronizou {len(profiles)} perfis")
    return jsonify({"ok": True, "count": len(profiles)})


# ── Job queue ─────────────────────────────────────────────────────────────────

@bp.route("/jobs/next", methods=["GET"])
@_require_key
def next_job():
    """Returns the oldest queued job, or null if none."""
    job = (
        VerifyJob.query
        .filter_by(status="queued")
        .order_by(VerifyJob.created_at.asc())
        .first()
    )
    if not job:
        return jsonify({"job": None})
    return jsonify({"job": {
        "id":          job.id,
        "profile_id":  job.profile_id,
        "business_id": job.business_id,
    }})


@bp.route("/jobs/<int:job_id>/start", methods=["POST"])
@_require_key
def job_start(job_id: int):
    job = db.session.get(VerifyJob, job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    job.status     = "running"
    job.started_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/jobs/<int:job_id>/done", methods=["POST"])
@_require_key
def job_done(job_id: int):
    """
    Worker reports job result.
    Body: {"success": bool, "message": str, "screenshot_b64": str (optional)}
    """
    job = db.session.get(VerifyJob, job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(force=True) or {}
    job.status      = "success" if data.get("success") else "error"
    job.last_message = data.get("message", "")
    job.finished_at = datetime.utcnow()
    log_event(
        "info" if data.get("success") else "error", "worker",
        f"Worker job {job_id}: {'sucesso' if data.get('success') else 'falha'}",
        detail=job.last_message, profile_id=job.profile_id, job_id=job_id,
    )

    # Save screenshot if provided (base64-encoded PNG)
    screenshot_b64 = data.get("screenshot_b64", "")
    if screenshot_b64:
        try:
            screenshots_dir = Path(current_app.static_folder) / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            dest = screenshots_dir / f"{job.profile_id}.png"
            dest.write_bytes(base64.b64decode(screenshot_b64))
            job.screenshot_path = f"{job.profile_id}.png"
        except Exception:
            pass

    db.session.commit()
    return jsonify({"ok": True})


# ── Open-browser commands ─────────────────────────────────────────────────────

@bp.route("/commands/next", methods=["GET"])
@_require_key
def next_command():
    """Returns the oldest pending command for the worker."""
    cmd = (
        WorkerCommand.query
        .filter_by(status="pending")
        .order_by(WorkerCommand.created_at.asc())
        .first()
    )
    if not cmd:
        return jsonify({"command": None})
    return jsonify({"command": {
        "id":           cmd.id,
        "command_type": cmd.command_type,
        "profile_id":   cmd.profile_id,
    }})


@bp.route("/commands/<int:cmd_id>/done", methods=["POST"])
@_require_key
def command_done(cmd_id: int):
    cmd = db.session.get(WorkerCommand, cmd_id)
    if not cmd:
        return jsonify({"error": "Not found"}), 404
    cmd.status = "done"
    db.session.commit()
    return jsonify({"ok": True})
