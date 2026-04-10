"""
Dashboard — WABA automation platform with count cards, filters, and status tracking.
"""
import sys
import json
from datetime import datetime
from pathlib import Path
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app, send_from_directory,
)
from flask_login import login_required, current_user
from .. import db
from ..models import (
    VerifyJob, ProfileSnapshot, WorkerCommand, WabaRecord,
    StatusTransition, ErrorReport, log_event,
    WABA_STATUS_AGUARDANDO, WABA_STATUS_EXECUTANDO, WABA_STATUS_EM_REVISAO,
    WABA_STATUS_NAO_VERIFICOU, WABA_STATUS_MONITORANDO_LIMITE,
    WABA_STATUS_250, WABA_STATUS_2K, WABA_STATUS_RESTRITA,
    WABA_STATUS_DESATIVADA, WABA_STATUS_ERRO,
    ALL_WABA_STATUSES, WABA_STATUS_LABELS, WABA_STATUS_COLORS,
)

bp = Blueprint("dashboard", __name__)


def _adspower():
    project_root = str(Path(__file__).parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from services.adspower import AdsPowerClient
    import config as verif_config
    return AdsPowerClient(verif_config.ADSPOWER_BASE)


def _verif_config():
    project_root = str(Path(__file__).parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    import config as verif_config
    return verif_config


def _latest_job(profile_id: str) -> VerifyJob | None:
    return (
        VerifyJob.query
        .filter_by(profile_id=profile_id)
        .order_by(VerifyJob.created_at.desc())
        .first()
    )


# ── Dashboard card stats ─────────────────────────────────────────────────────

def _get_card_stats() -> dict:
    """Get counts for each status card."""
    from sqlalchemy import func

    total = db.session.query(func.count(WabaRecord.id)).scalar() or 0

    counts = (
        db.session.query(WabaRecord.status, func.count(WabaRecord.id))
        .group_by(WabaRecord.status)
        .all()
    )
    status_counts = dict(counts)

    # "Verificadas" = those that passed review (monitorando + 250 + 2k)
    verified_count = sum(
        status_counts.get(s, 0)
        for s in [WABA_STATUS_MONITORANDO_LIMITE, WABA_STATUS_250, WABA_STATUS_2K]
    )

    return {
        "total":       total,
        "aguardando":  status_counts.get(WABA_STATUS_AGUARDANDO, 0),
        "executando":  status_counts.get(WABA_STATUS_EXECUTANDO, 0),
        "em_revisao":  status_counts.get(WABA_STATUS_EM_REVISAO, 0),
        "verificadas": verified_count,
        "monitorando": status_counts.get(WABA_STATUS_MONITORANDO_LIMITE, 0),
        "250":         status_counts.get(WABA_STATUS_250, 0),
        "2k":          status_counts.get(WABA_STATUS_2K, 0),
        "restrita":    status_counts.get(WABA_STATUS_RESTRITA, 0),
        "desativada":  status_counts.get(WABA_STATUS_DESATIVADA, 0),
        "nao_verificou": status_counts.get(WABA_STATUS_NAO_VERIFICOU, 0),
        "erro":        status_counts.get(WABA_STATUS_ERRO, 0),
    }


# ── Main routes ──────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def index():
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/dashboard")
@login_required
def dashboard():
    stats = _get_card_stats()
    active_filter = request.args.get("status", "todos")
    page = request.args.get("page", 1, type=int)
    per_page = 50

    # Build query
    query = WabaRecord.query

    if active_filter != "todos":
        if active_filter == "verificadas":
            query = query.filter(WabaRecord.status.in_([
                WABA_STATUS_MONITORANDO_LIMITE, WABA_STATUS_250, WABA_STATUS_2K
            ]))
        else:
            query = query.filter_by(status=active_filter)

    pagination = query.order_by(WabaRecord.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Unresolved errors count for nav badge
    unresolved_errors = ErrorReport.query.filter_by(resolved=False).count()

    return render_template(
        "dashboard.html",
        title="Dashboard",
        stats=stats,
        wabas=pagination.items,
        pagination=pagination,
        active_filter=active_filter,
        status_labels=WABA_STATUS_LABELS,
        status_colors=WABA_STATUS_COLORS,
        all_statuses=ALL_WABA_STATUSES,
        unresolved_errors=unresolved_errors,
    )


# ── API: dashboard stats (AJAX) ─────────────────────────────────────────────

@bp.route("/api/dashboard/stats")
@login_required
def api_stats():
    return jsonify(_get_card_stats())


# ── API: WABA list (AJAX) ───────────────────────────────────────────────────

@bp.route("/api/wabas")
@login_required
def api_wabas():
    status_filter = request.args.get("status", "todos")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)

    query = WabaRecord.query
    if status_filter != "todos":
        if status_filter == "verificadas":
            query = query.filter(WabaRecord.status.in_([
                WABA_STATUS_MONITORANDO_LIMITE, WABA_STATUS_250, WABA_STATUS_2K
            ]))
        else:
            query = query.filter_by(status=status_filter)

    pagination = query.order_by(WabaRecord.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    items = []
    for w in pagination.items:
        items.append({
            "id": w.id,
            "profile_id": w.profile_id,
            "waba_name": w.waba_name or "",
            "status": w.status,
            "status_label": WABA_STATUS_LABELS.get(w.status, w.status),
            "status_color": WABA_STATUS_COLORS.get(w.status, "bg-zinc-700"),
            "messaging_limit": w.messaging_limit or "",
            "last_limit_check": w.last_limit_check.isoformat() if w.last_limit_check else "",
            "last_error": w.last_error or "",
            "business_id": w.business_id or "",
            "domain": w.domain or "",
            "created_at": w.created_at.isoformat() if w.created_at else "",
            # Step flags
            "bm_created": w.bm_created,
            "business_info_done": w.business_info_done,
            "domain_done": w.domain_done,
            "waba_created": w.waba_created,
            "verification_sent": w.verification_sent,
        })

    return jsonify({
        "items": items,
        "page": pagination.page,
        "pages": pagination.pages,
        "total": pagination.total,
    })


# ── API: manual check trigger ────────────────────────────────────────────────

@bp.route("/api/wabas/<int:waba_id>/check", methods=["POST"])
@login_required
def trigger_check(waba_id: int):
    """Manually trigger a status check for a WABA."""
    waba = db.session.get(WabaRecord, waba_id)
    if not waba:
        return jsonify({"ok": False, "error": "WABA não encontrada"}), 404

    from ..config import Config
    if Config.USE_CELERY:
        from tasks.check_waba import check_waba_status
        check_waba_status.apply_async(args=[waba_id], queue="check", retry=False)
        return jsonify({"ok": True, "message": "Verificação enfileirada"})
    else:
        # Direct execution in a thread (fallback)
        import threading
        from services.waba_checker import WabaChecker

        def _run():
            with current_app._get_current_object().app_context():
                checker = WabaChecker()
                checker.check(waba)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "message": "Verificação iniciada"})


# ── API: run verification for a WABA ────────────────────────────────────────

@bp.route("/api/wabas/<int:waba_id>/run", methods=["POST"])
@login_required
def run_waba(waba_id: int):
    """Enqueue a verification job for a WABA."""
    waba = db.session.get(WabaRecord, waba_id)
    if not waba:
        return jsonify({"ok": False, "error": "WABA não encontrada"}), 404

    if waba.status not in (WABA_STATUS_AGUARDANDO, WABA_STATUS_ERRO):
        return jsonify({"ok": False, "error": f"WABA não pode ser executada no status '{WABA_STATUS_LABELS.get(waba.status, waba.status)}'"}), 409

    from ..config import Config
    if Config.USE_CELERY:
        try:
            from tasks.verify_waba import create_and_verify
            create_and_verify.apply_async(args=[waba_id], queue="verify", retry=False)
            return jsonify({"ok": True, "message": "Verificação enfileirada"})
        except Exception:
            pass  # Redis unavailable — fall through to WebSocket dispatch
    return _trigger_run_legacy(waba)


# ── API: cancel running job ──────────────────────────────────────────────────

@bp.route("/api/wabas/<int:waba_id>/cancel", methods=["POST"])
@login_required
def cancel_waba(waba_id: int):
    """Cancel a running/queued verification job and reset WABA to aguardando."""
    from .agent_ws import push_to_agent, agent_user_id_for_profile

    waba = db.session.get(WabaRecord, waba_id)
    if not waba:
        return jsonify({"ok": False, "error": "WABA não encontrada"}), 404

    if waba.status != WABA_STATUS_EXECUTANDO:
        return jsonify({"ok": False, "error": "WABA não está em execução"}), 409

    # Find the active job so we can tell the agent to abort it
    active_job = (
        VerifyJob.query
        .filter(VerifyJob.profile_id == waba.profile_id,
                VerifyJob.status.in_(["running", "queued"]))
        .order_by(VerifyJob.created_at.desc())
        .first()
    )

    # Send cancel signal to the agent before touching the DB
    if active_job:
        owner_id = agent_user_id_for_profile(waba.profile_id or "") or current_user.id
        push_to_agent(owner_id, {"type": "cancel_job", "job_id": active_job.id})

    # Reset status immediately so the UI reflects it even if the agent is gone
    waba.status = WABA_STATUS_AGUARDANDO
    if active_job:
        active_job.status      = "error"
        active_job.last_message = "Cancelado manualmente"
        active_job.finished_at = datetime.utcnow()
    db.session.commit()

    log_event("info", "job", f"Job cancelado manualmente, waba_id={waba_id}",
              user_id=current_user.id, profile_id=waba.profile_id,
              job_id=active_job.id if active_job else None)
    return jsonify({"ok": True})


# ── API: bulk actions ────────────────────────────────────────────────────────

@bp.route("/api/wabas/bulk/run", methods=["POST"])
@login_required
def bulk_run():
    """Enqueue verification for multiple WABAs."""
    data = request.get_json(silent=True) or {}
    waba_ids = data.get("waba_ids", [])

    if not waba_ids:
        return jsonify({"ok": False, "error": "Nenhuma WABA selecionada"}), 400

    from ..config import Config
    enqueued = 0
    for waba_id in waba_ids:
        waba = db.session.get(WabaRecord, waba_id)
        if waba and waba.status in (WABA_STATUS_AGUARDANDO, WABA_STATUS_ERRO):
            dispatched = False
            if Config.USE_CELERY:
                try:
                    from tasks.verify_waba import create_and_verify
                    create_and_verify.apply_async(args=[waba_id], queue="verify", retry=False)
                    dispatched = True
                except Exception:
                    pass  # Redis unavailable — fall through to WebSocket dispatch
            if not dispatched:
                _trigger_run_legacy(waba)
            enqueued += 1

    return jsonify({"ok": True, "enqueued": enqueued})


@bp.route("/api/wabas/bulk/check", methods=["POST"])
@login_required
def bulk_check():
    """Enqueue status checks for multiple WABAs."""
    data = request.get_json(silent=True) or {}
    waba_ids = data.get("waba_ids", [])

    if not waba_ids:
        return jsonify({"ok": False, "error": "Nenhuma WABA selecionada"}), 400

    from ..config import Config
    if not Config.USE_CELERY:
        return jsonify({"ok": False, "error": "Checar status requer Celery ativo (USE_CELERY=1 + Redis rodando)."}), 503

    enqueued = 0
    for waba_id in waba_ids:
        waba = db.session.get(WabaRecord, waba_id)
        if waba:
            try:
                from tasks.check_waba import check_waba_status
                check_waba_status.apply_async(args=[waba_id], queue="check", retry=False)
                enqueued += 1
            except Exception:
                return jsonify({"ok": False, "error": "Redis indisponível — inicie o Redis e tente novamente."}), 503

    return jsonify({"ok": True, "enqueued": enqueued})


# ── Legacy: open profile ─────────────────────────────────────────────────────

@bp.route("/api/profile/<profile_id>/open", methods=["POST"])
@login_required
def open_profile(profile_id: str):
    from ..config import Config
    if Config.USE_WORKER:
        from .agent_ws import push_to_agent, is_agent_connected, agent_user_id_for_profile
        owner_id = agent_user_id_for_profile(profile_id) or current_user.id
        if is_agent_connected(owner_id):
            push_to_agent(owner_id, {
                "type":       "open_browser",
                "profile_id": profile_id,
                "cmd_id":     None,
            })
            return jsonify({"ok": True, "queued": False})
        cmd = WorkerCommand(command_type="open_browser", profile_id=profile_id)
        db.session.add(cmd)
        db.session.commit()
        return jsonify({"ok": True, "queued": True})
    try:
        client = _adspower()
        client.open_browser(profile_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Legacy: run verification by profile_id ───────────────────────────────────

@bp.route("/api/profile/<profile_id>/run", methods=["POST"])
@login_required
def run_profile(profile_id: str):
    data = request.get_json(silent=True) or {}
    business_id = (data.get("business_id") or "").strip()

    # Check if there's a WabaRecord for this profile
    waba = WabaRecord.query.filter_by(profile_id=profile_id).first()
    if waba:
        if business_id:
            waba.business_id = business_id
            db.session.commit()
        return run_waba(waba.id)

    # Fallback to legacy flow
    return _trigger_run_legacy_by_profile(profile_id, business_id)


def _trigger_run_legacy(waba: WabaRecord):
    """Dispatch a WabaRecord job via WebSocket agent (Celery fallback)."""
    from .agent_ws import push_to_agent, is_agent_connected, agent_user_id_for_profile
    profile_id = waba.profile_id or ""
    owner_id   = agent_user_id_for_profile(profile_id) or current_user.id
    connected  = is_agent_connected(owner_id)
    job = VerifyJob(
        profile_id=profile_id,
        waba_record_id=waba.id,
        user_id=current_user.id,
        status="queued",
        business_id=waba.business_id or "",
        last_message="Aguardando agent..." if connected else "Agent offline — aguardando conexão...",
    )
    db.session.add(job)
    # Mark WABA as executando so duplicate clicks get a 409
    waba.status = WABA_STATUS_EXECUTANDO
    db.session.commit()
    log_event("info", "job", f"Job criado (fallback WebSocket), waba_id={waba.id}",
              user_id=current_user.id, profile_id=profile_id, job_id=job.id)
    from .agent_ws import _sms_payload
    push_to_agent(owner_id, {
        "type": "run_job",
        "job":  {"id": job.id, "profile_id": profile_id, "business_id": waba.business_id or "",
                 "sms": _sms_payload()},
    })
    return jsonify({"ok": True, "job_id": job.id})


def _trigger_run_legacy_by_profile(profile_id: str, business_id: str):
    """Legacy local-thread execution by profile_id."""
    from ..config import Config

    existing = _latest_job(profile_id)
    if existing and existing.status in ("running", "queued"):
        return jsonify({"ok": False, "error": "Verificação já em andamento para este perfil."}), 409

    if Config.USE_WORKER:
        from .agent_ws import push_to_agent, is_agent_connected, agent_user_id_for_profile
        owner_id = agent_user_id_for_profile(profile_id) or current_user.id
        connected = is_agent_connected(owner_id)
        job = VerifyJob(
            profile_id=profile_id,
            user_id=current_user.id,
            status="queued",
            business_id=business_id or "",
            last_message="Aguardando agent..." if connected else "Agent offline — aguardando conexão...",
        )
        db.session.add(job)
        db.session.commit()
        log_event("info", "job", f"Job criado (VPS mode), business_id='{business_id}'",
                  user_id=current_user.id, profile_id=profile_id, job_id=job.id)
        from .agent_ws import _sms_payload
        push_to_agent(owner_id, {
            "type": "run_job",
            "job": {
                "id":          job.id,
                "profile_id":  job.profile_id,
                "business_id": job.business_id or "",
                "sms":         _sms_payload(),
            },
        })
        return jsonify({"ok": True, "job_id": job.id})

    # Local mode
    cfg = _verif_config()
    client = _adspower()

    try:
        profile = client.get_profile(profile_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Perfil não encontrado: {e}"}), 404

    remark = profile.get("remark", "")
    gerador_data = _parse_gerador_block(remark, cfg)
    run_id = gerador_data.get("run_id") if gerador_data else None
    email_mode = gerador_data.get("email_mode", "own") if gerador_data else "own"

    if business_id:
        gerador_data = gerador_data or {}
        gerador_data["business_id"] = business_id

    if run_id is None:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            from main import _acquire_run_id
            run_id = _acquire_run_id()
        except Exception as e:
            return jsonify({"ok": False, "error": f"Não foi possível obter run_id do Gerador: {e}"}), 500

        if run_id is None:
            return jsonify({"ok": False, "error": "Gerador não retornou um run_id válido."}), 500

        gerador_data = gerador_data or {}
        gerador_data["run_id"] = run_id

        # Persist run_id to the profile remark immediately so re-runs reuse the same run
        new_remark = remark.rstrip() + f"\n\n{cfg.GERADOR_REMARK_MARKER}\n{json.dumps(gerador_data)}"
        try:
            client.update_profile(profile_id, remark=new_remark)
            profile = dict(profile)
            profile["remark"] = new_remark
            remark = new_remark
        except Exception as e:
            current_app.logger.warning(f"Could not persist run_id {run_id} to profile {profile_id} remark: {e}")

    from .jobs import start_job
    job_id = start_job(
        app=current_app._get_current_object(),
        profile=profile,
        run_id=run_id,
        email_mode=email_mode,
        business_id=business_id,
        triggered_by_user_id=current_user.id,
        gerador_data=gerador_data,
    )
    return jsonify({"ok": True, "job_id": job_id})


def _parse_gerador_block(remark: str, cfg) -> dict | None:
    marker = cfg.GERADOR_REMARK_MARKER
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


# ── API: profile job status (legacy) ─────────────────────────────────────────

@bp.route("/api/profile/<profile_id>/status")
@login_required
def profile_status(profile_id: str):
    # Check WabaRecord first
    waba = WabaRecord.query.filter_by(profile_id=profile_id).first()
    if waba:
        return jsonify({
            "waba_id": waba.id,
            "status": waba.status,
            "status_label": WABA_STATUS_LABELS.get(waba.status, waba.status),
            "last_message": waba.last_error or "",
            "screenshot_path": waba.last_screenshot or "",
            "steps": {
                "bm_created": waba.bm_created,
                "business_info": waba.business_info_done,
                "domain_verified": waba.domain_done,
                "waba_created": waba.waba_created,
                "verification_done": waba.verification_sent,
            },
        })

    # Fallback to legacy VerifyJob
    job = _latest_job(profile_id)
    if not job:
        return jsonify({"status": "idle", "last_message": "", "screenshot_path": "", "steps": {}})
    return jsonify({
        "job_id": job.id,
        "status": job.status,
        "last_message": job.last_message,
        "screenshot_path": job.screenshot_path,
        "steps": {},
    })


# ── WABA status (AJAX polling) ──────────────────────────────────────────────

@bp.route("/api/wabas/<int:waba_id>/status")
@login_required
def waba_status(waba_id: int):
    waba = db.session.get(WabaRecord, waba_id)
    if not waba:
        return jsonify({"error": "Not found"}), 404

    return jsonify({
        "id": waba.id,
        "status": waba.status,
        "status_label": WABA_STATUS_LABELS.get(waba.status, waba.status),
        "status_color": WABA_STATUS_COLORS.get(waba.status, "bg-zinc-700"),
        "last_error": waba.last_error or "",
        "last_screenshot": waba.last_screenshot or "",
        "messaging_limit": waba.messaging_limit or "",
        "steps": {
            "bm_created": waba.bm_created,
            "business_info": waba.business_info_done,
            "domain_verified": waba.domain_done,
            "waba_created": waba.waba_created,
            "verification_done": waba.verification_sent,
        },
    })


# ── Screenshot serving ───────────────────────────────────────────────────────

@bp.route("/screenshots/<path:filename>")
@login_required
def screenshot(filename: str):
    screenshots_dir = Path(current_app.static_folder) / "screenshots"
    return send_from_directory(screenshots_dir, filename)
