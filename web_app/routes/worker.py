"""
Worker API — authenticated endpoints used only by the local worker.py process.
The local machine (running AdsPower) calls these to sync profiles,
pick up queued jobs, and post results back.
"""
import base64
from datetime import datetime
from functools import wraps
from pathlib import Path

import threading
import uuid as _uuid

from flask import Blueprint, request, jsonify, current_app, send_file
from .. import db
from ..models import (ProfileSnapshot, VerifyJob, WabaRecord, WorkerCommand,
                      delete_waba_cascade, log_event)

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

    # Remove profiles no longer in AdsPower — delete WabaRecords first, then snapshots
    stale_ids = [old.profile_id for old in ProfileSnapshot.query.all()
                 if old.profile_id not in incoming_ids]
    if stale_ids:
        for waba in WabaRecord.query.filter(WabaRecord.profile_id.in_(stale_ids)).all():
            delete_waba_cascade(waba)
        for pid in stale_ids:
            snap = db.session.get(ProfileSnapshot, pid)
            if snap:
                db.session.delete(snap)

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


# ── Gerador proxy (agent has no DB access — calls VPS on its behalf) ──────────

@bp.route("/gerador/runs/<int:run_id>", methods=["GET"])
@_require_key
def gerador_get_run(run_id: int):
    from services.cnpj_pipeline import get_run_data
    try:
        return jsonify(get_run_data(run_id))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/gerador/runs/<int:run_id>/pdf", methods=["GET"])
@_require_key
def gerador_get_pdf(run_id: int):
    from services.cnpj_pipeline import download_pdf
    try:
        pdf_path = download_pdf(run_id)
        return send_file(pdf_path, mimetype="application/pdf", as_attachment=False)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/gerador/runs/<int:run_id>/change-phone", methods=["POST"])
@_require_key
def gerador_change_phone(run_id: int):
    from services.cnpj_pipeline import change_phone
    data = request.get_json(force=True) or {}
    phone = data.get("phone", "")
    if not phone:
        return jsonify({"error": "phone required"}), 400
    try:
        phone_formatted, _pdf_path = change_phone(run_id, phone)
        return jsonify({"success": True, "phone_formatted": phone_formatted})
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/gerador/runs/<int:run_id>/change-website-phone", methods=["POST"])
@_require_key
def gerador_change_website_phone(run_id: int):
    from services.cnpj_pipeline import change_website_phone
    data = request.get_json(force=True) or {}
    phone = data.get("phone", "")
    if not phone:
        return jsonify({"error": "phone required"}), 400
    try:
        ok = change_website_phone(run_id, phone)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/gerador/runs/<int:run_id>/inject-meta-tag", methods=["POST"])
@_require_key
def gerador_inject_meta_tag(run_id: int):
    from services.cnpj_pipeline import inject_meta_tag
    data = request.get_json(force=True) or {}
    meta_tag = data.get("meta_tag", "")
    if not meta_tag:
        return jsonify({"error": "meta_tag required"}), 400
    try:
        ok = inject_meta_tag(run_id, meta_tag)
        return jsonify({"success": ok})
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 500


# poll_id → {"status": "pending"|"done"|"error", "run_id": int, "error": str}
_gen_jobs: dict = {}


def _run_generation(poll_id: str, app):
    with app.app_context():
        try:
            from services.cnpj_pipeline import generate_cnpj_run
            run = generate_cnpj_run()
            _gen_jobs[poll_id] = {"status": "done", "run_id": run.id}
        except Exception as e:
            _gen_jobs[poll_id] = {"status": "error", "error": str(e)}


@bp.route("/gerador/acquire-run", methods=["POST"])
@_require_key
def gerador_acquire_run():
    from web_app.models import CNPJRun
    from datetime import datetime

    # Fast path: claim from pre-generated bank
    pre = (
        CNPJRun.query
        .filter_by(is_pre_generated=True, claimed_at=None)
        .order_by(CNPJRun.created_at.asc())
        .first()
    )
    if pre:
        pre.claimed_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"run_id": pre.id, "source": "bank"})

    # Bank empty — start async generation and return a poll handle
    poll_id = str(_uuid.uuid4())
    _gen_jobs[poll_id] = {"status": "pending"}
    app = current_app._get_current_object()
    t = threading.Thread(target=_run_generation, args=(poll_id, app), daemon=True)
    t.start()
    return jsonify({"status": "pending", "poll_id": poll_id}), 202


@bp.route("/gerador/acquire-run/<poll_id>", methods=["GET"])
@_require_key
def gerador_acquire_run_poll(poll_id: str):
    job = _gen_jobs.get(poll_id)
    if job is None:
        return jsonify({"error": "poll_id not found"}), 404
    if job["status"] == "pending":
        return jsonify({"status": "pending"}), 202
    # Remove entry after it's been consumed
    _gen_jobs.pop(poll_id, None)
    if job["status"] == "done":
        return jsonify({"status": "done", "run_id": job["run_id"], "source": "generated"})
    return jsonify({"status": "error", "error": job.get("error", "unknown")}), 500
