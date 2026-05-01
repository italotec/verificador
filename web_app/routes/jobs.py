"""
Background job runner for profile verification.
Runs each verification in a separate thread so Flask stays responsive.
"""
import os
import sys
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify, current_app
from flask_login import login_required
from .. import db
from ..models import VerifyJob, log_event

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

# Track active threads per profile_id so we don't double-run
_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()


def _get_lock(profile_id: str) -> threading.Lock:
    with _locks_mutex:
        if profile_id not in _locks:
            _locks[profile_id] = threading.Lock()
        return _locks[profile_id]


def _latest_screenshot(debug_dir: Path, since_epoch: float) -> Path | None:
    """Find the most recently modified .png in debug_dir created after since_epoch."""
    if not debug_dir.exists():
        return None
    candidates = [
        p for p in debug_dir.rglob("*.png")
        if p.stat().st_mtime >= since_epoch
    ]
    if not candidates:
        # Fallback: take any most-recent screenshot
        all_pngs = list(debug_dir.rglob("*.png"))
        if not all_pngs:
            return None
        candidates = all_pngs
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _job_thread(app, job_id: int, profile: dict, run_id, email_mode: str,
                business_id: str, gerador_data: dict):
    """Runs in a background thread. Updates VerifyJob in DB when done."""
    profile_id = profile["user_id"]
    lock = _get_lock(profile_id)

    if not lock.acquire(blocking=False):
        # Another thread is already processing this profile
        with app.app_context():
            job = db.session.get(VerifyJob, job_id)
            if job:
                job.status = "error"
                job.last_message = "Outro processo já está rodando para este perfil."
                job.finished_at = datetime.utcnow()
                db.session.commit()
                log_event("warning", "job", "Bloqueado: perfil já em execução", profile_id=profile_id, job_id=job_id)
        return

    try:
        with app.app_context():
            job = db.session.get(VerifyJob, job_id)
            if job:
                job.status = "running"
                job.started_at = datetime.utcnow()
                db.session.commit()
            gerador_data = gerador_data or {}
            # domain_verification_method is resolved by get_run_data() on the VPS;
            # no local DB read needed here.

        # Add project root to sys.path so we can import main.py helpers
        project_root = str(Path(__file__).parent.parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        import config as verif_config
        from main import _run_for_profile, _mark_verified

        start_time = time.time()

        try:
            success = _run_for_profile(
                profile=profile,
                run_id=run_id,
                email_mode=email_mode,
                business_id=business_id,
                gerador_data=gerador_data,
            )
        except Exception as e:
            success = False
            err_msg = str(e)[:500]
            import traceback
            with app.app_context():
                log_event("error", "job", f"Exceção durante execução: {err_msg}",
                          detail=traceback.format_exc(), profile_id=profile_id, job_id=job_id)
        else:
            err_msg = ""

        # Capture latest screenshot
        screenshot_rel = ""
        try:
            latest_png = _latest_screenshot(
                Path(verif_config.DEBUG_DIR), since_epoch=start_time
            )
            if latest_png:
                with app.app_context():
                    screenshots_dir = Path(app.static_folder) / "screenshots"
                    screenshots_dir.mkdir(parents=True, exist_ok=True)
                    dest = screenshots_dir / f"{profile_id}.png"
                    shutil.copy2(latest_png, dest)
                    screenshot_rel = f"{profile_id}.png"
        except Exception:
            pass

        with app.app_context():
            job = db.session.get(VerifyJob, job_id)
            if job:
                job.status = "success" if success else "error"
                job.finished_at = datetime.utcnow()
                job.screenshot_path = screenshot_rel
                job.last_message = (
                    "Verificação concluída com sucesso!" if success
                    else (err_msg or "Verificação falhou.")
                )
                db.session.commit()
                log_event(
                    "info" if success else "error", "job",
                    f"Job concluído: {'sucesso' if success else 'falha'}",
                    detail=job.last_message, profile_id=profile_id, job_id=job_id,
                    user_id=job.user_id,
                )

            if success:
                _mark_verified(profile_id)

    finally:
        lock.release()


def start_job(app, profile: dict, run_id, email_mode: str,
              business_id: str, triggered_by_user_id: int,
              gerador_data: dict | None = None) -> int:
    """
    Create a VerifyJob record and launch the background thread.
    Returns the new job_id.
    """
    profile_id = profile["user_id"]
    gerador_data = gerador_data or {}

    with app.app_context():
        job = VerifyJob(
            profile_id=profile_id,
            user_id=triggered_by_user_id,
            status="idle",
            business_id=business_id or "",
        )
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    t = threading.Thread(
        target=_job_thread,
        args=(app, job_id, profile, run_id, email_mode, business_id, gerador_data),
        daemon=True,
    )
    t.start()
    return job_id


@bp.route("/<int:job_id>/status")
@login_required
def job_status(job_id: int):
    job = db.session.get(VerifyJob, job_id)
    if not job:
        return jsonify({"error": "Job não encontrado."}), 404
    return jsonify({
        "id": job.id,
        "profile_id": job.profile_id,
        "status": job.status,
        "last_message": job.last_message,
        "screenshot_path": job.screenshot_path,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    })
