"""
Dashboard — shows AdsPower profiles from "Verificar" and "Verificadas" groups.
"""
import sys
import json
from pathlib import Path
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app, send_from_directory,
)
from flask_login import login_required, current_user
from .. import db
from ..models import VerifyJob, ProfileSnapshot, WorkerCommand

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


def _build_steps(gerador: dict | None, job_status: str) -> dict:
    """Build the 5-step progress dict from gerador flags and job status."""
    return {
        "bm_created":       bool(gerador.get("business_id")) if gerador else False,
        "business_info":    bool(gerador.get("business_info_done")) if gerador else False,
        "domain_verified":  bool(gerador.get("domain_done")) if gerador else False,
        "waba_created":     bool(gerador.get("waba_done")) if gerador else False,
        "verification_done": job_status == "success",
    }


def _enrich_profiles(profiles: list[dict]) -> list[dict]:
    """Add job status info and step progress to each profile dict."""
    cfg = _verif_config()
    for p in profiles:
        job = _latest_job(p["user_id"])
        job_status = job.status if job else "idle"
        p["job"] = {
            "id": job.id if job else None,
            "status": job_status,
            "last_message": job.last_message if job else "",
            "screenshot_path": job.screenshot_path if job else "",
        }
        gerador = _parse_gerador_block(p.get("remark", ""), cfg)
        p["steps"] = _build_steps(gerador, job_status)
    return profiles


@bp.route("/")
@login_required
def index():
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/dashboard")
@login_required
def dashboard():
    from ..config import Config

    verificar_profiles = []
    verificadas_profiles = []
    error_msg = None

    if Config.USE_WORKER:
        # ── VPS mode: read THIS user's profile snapshots ───────────────────
        snaps = (
            ProfileSnapshot.query
            .filter_by(user_id=current_user.id)
            .order_by(ProfileSnapshot.name.asc())
            .all()
        )
        cfg = _verif_config()
        for s in snaps:
            p = {"user_id": s.profile_id, "name": s.name, "remark": s.remark}
            job = _latest_job(s.profile_id)
            job_status = job.status if job else "idle"
            p["job"] = {
                "id": job.id if job else None,
                "status": job_status,
                "last_message": job.last_message if job else "",
                "screenshot_path": job.screenshot_path if job else "",
            }
            gerador = _parse_gerador_block(s.remark, cfg)
            p["steps"] = _build_steps(gerador, job_status)
            if s.group_name == cfg.VERIFICAR_GROUP_NAME:
                verificar_profiles.append(p)
            else:
                verificadas_profiles.append(p)

        if not snaps:
            error_msg = "Aguardando sincronização do agent. Abra o Verificador Agent na sua máquina local."
    else:
        # ── Local mode: query AdsPower directly ────────────────────────────
        cfg = _verif_config()
        client = _adspower()
        try:
            group_data = client._get("/api/v1/group/list", page=1, page_size=200)
            groups = {g["group_name"]: str(g["group_id"]) for g in group_data.get("list", [])}

            verificar_gid = groups.get(cfg.VERIFICAR_GROUP_NAME)
            verificadas_gid = groups.get(cfg.VERIFICADAS_GROUP_NAME)

            if verificar_gid:
                verificar_profiles = _enrich_profiles(
                    client.list_profiles(group_id=verificar_gid)
                )
            if verificadas_gid:
                verificadas_profiles = _enrich_profiles(
                    client.list_profiles(group_id=verificadas_gid)
                )
        except Exception as e:
            error_msg = f"Erro ao carregar perfis do AdsPower: {e}"

    active_tab = request.args.get("tab", "verificar")

    return render_template(
        "dashboard.html",
        title="Dashboard",
        verificar_profiles=verificar_profiles,
        verificadas_profiles=verificadas_profiles,
        active_tab=active_tab,
        error_msg=error_msg,
    )


# ── API: open browser ─────────────────────────────────────────────────────────

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
        # Agent offline: queue a WorkerCommand for when it reconnects
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


# ── API: run verification ─────────────────────────────────────────────────────

@bp.route("/api/profile/<profile_id>/run", methods=["POST"])
@login_required
def run_profile(profile_id: str):
    data = request.get_json(silent=True) or {}
    business_id = (data.get("business_id") or "").strip()
    return _trigger_run(profile_id, business_id)


def _trigger_run(profile_id: str, business_id: str):
    """Shared logic for /run. Handles both local-thread and VPS-worker modes."""
    from ..config import Config

    # Block if already running
    existing = _latest_job(profile_id)
    if existing and existing.status in ("running", "queued"):
        return jsonify({"ok": False, "error": "Verificação já em andamento para este perfil."}), 409

    if Config.USE_WORKER:
        # ── VPS mode: create a queued job; push to the profile-owner's agent ─
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
        push_to_agent(owner_id, {
            "type": "run_job",
            "job": {
                "id":          job.id,
                "profile_id":  job.profile_id,
                "business_id": job.business_id or "",
            },
        })
        return jsonify({"ok": True, "job_id": job.id})

    # ── Local mode: resolve run_id and start a thread ──────────────────────
    cfg = _verif_config()
    client = _adspower()

    try:
        profile = client.get_profile(profile_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Perfil não encontrado: {e}"}), 404

    remark = profile.get("remark", "")
    gerador_data = _parse_gerador_block(remark, cfg)
    run_id    = gerador_data.get("run_id") if gerador_data else None
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


# ── API: profile job status ───────────────────────────────────────────────────

@bp.route("/api/profile/<profile_id>/status")
@login_required
def profile_status(profile_id: str):
    job = _latest_job(profile_id)
    job_status = job.status if job else "idle"

    # Parse step flags from profile remark
    snap = db.session.get(ProfileSnapshot, profile_id)
    cfg = _verif_config()
    gerador = _parse_gerador_block(snap.remark, cfg) if snap and snap.remark else None
    steps = _build_steps(gerador, job_status)

    if not job:
        return jsonify({"status": "idle", "last_message": "", "screenshot_path": "", "steps": steps})
    return jsonify({
        "job_id": job.id,
        "status": job.status,
        "last_message": job.last_message,
        "screenshot_path": job.screenshot_path,
        "steps": steps,
    })


# ── Screenshot serving ────────────────────────────────────────────────────────

@bp.route("/screenshots/<path:filename>")
@login_required
def screenshot(filename: str):
    screenshots_dir = Path(current_app.static_folder) / "screenshots"
    return send_from_directory(screenshots_dir, filename)
