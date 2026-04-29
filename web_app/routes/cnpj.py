"""
User-facing CNPJ generation endpoints.
Bank-first: claims a pre-generated run instantly, falls back to async generation.
"""
import threading
import uuid
from datetime import datetime
from pathlib import Path

import config as cfg
from flask import (
    Blueprint, jsonify, render_template, request, send_file, abort,
    current_app,
)
from flask_login import login_required

from .. import db
from ..models import CNPJRun

bp = Blueprint("cnpj", __name__)

# In-process job store for async generation (poll_id → state dict)
_gen_jobs: dict[str, dict] = {}


def _run_generation(poll_id: str, app) -> None:
    with app.app_context():
        try:
            from services.cnpj_pipeline import generate_cnpj_run
            run = generate_cnpj_run()
            _gen_jobs[poll_id] = {"status": "done", "run_id": run.id}
        except Exception as e:
            _gen_jobs[poll_id] = {"status": "error", "error": str(e)}


# ── API: acquire a run ────────────────────────────────────────────────────────

@bp.post("/api/cnpj/acquire")
@login_required
def api_acquire():
    pre = (
        CNPJRun.query
        .filter_by(is_pre_generated=True, claimed_at=None)
        .order_by(CNPJRun.created_at.asc())
        .first()
    )
    if pre:
        pre.claimed_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "run_id": pre.id, "source": "bank"})

    poll_id = str(uuid.uuid4())
    _gen_jobs[poll_id] = {"status": "pending"}
    threading.Thread(
        target=_run_generation,
        args=(poll_id, current_app._get_current_object()),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "poll_id": poll_id}), 202


@bp.get("/api/cnpj/acquire/<poll_id>")
@login_required
def api_acquire_poll(poll_id: str):
    job = _gen_jobs.get(poll_id)
    if job is None:
        return jsonify({"ok": False, "error": "poll_id não encontrado"}), 404
    if job["status"] == "pending":
        return jsonify({"ok": True, "status": "pending"}), 202
    _gen_jobs.pop(poll_id, None)
    if job["status"] == "done":
        return jsonify({"ok": True, "status": "done", "run_id": job["run_id"]})
    return jsonify({"ok": False, "status": "error", "error": job.get("error", "erro desconhecido")}), 500


# ── Pages ─────────────────────────────────────────────────────────────────────

@bp.get("/cnpj/loading")
@login_required
def loading():
    poll_id = request.args.get("poll_id", "")
    return render_template("cnpj_view.html", loading=True, poll_id=poll_id)


@bp.get("/cnpj/<int:run_id>")
@login_required
def view(run_id: int):
    run = CNPJRun.query.get_or_404(run_id)
    try:
        from services.cnpj_pipeline import get_run_data
        data = get_run_data(run_id)
    except Exception as e:
        current_app.logger.warning(f"[cnpj.view] get_run_data failed for {run_id}: {e}")
        data = None
    return render_template("cnpj_view.html", run=run, data=data, loading=False, poll_id="")


@bp.get("/cnpj/<int:run_id>/preview")
@login_required
def preview(run_id: int):
    run = CNPJRun.query.get_or_404(run_id)
    if not run.index_rel:
        abort(404)
    index_path = (cfg.GERADOR_STORAGE_DIR / run.index_rel).resolve()
    if not index_path.exists():
        abort(404)
    response = send_file(index_path, mimetype="text/html")
    # Allow iframe embedding — remove the X-Frame-Options header Flask may add
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    return response


@bp.get("/cnpj/<int:run_id>/download/<kind>")
@login_required
def download(run_id: int, kind: str):
    run = CNPJRun.query.get_or_404(run_id)
    rel_map = {"pdf": run.pdf_rel, "index": run.index_rel, "link": run.link_rel}
    rel = rel_map.get(kind)
    if not rel:
        abort(404)
    file_path = (cfg.GERADOR_STORAGE_DIR / rel).resolve()
    if not file_path.exists():
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=Path(rel).name)
