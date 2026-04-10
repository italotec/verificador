from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from .. import db
from ..models import User, VerifyJob, AppLog, SystemSetting, log_event

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _is_admin():
    return current_user.is_authenticated and bool(getattr(current_user, "is_admin", False))


@bp.before_request
def guard():
    if not _is_admin():
        return redirect(url_for("dashboard.dashboard"))


@bp.route("/users", methods=["GET"])
@login_required
def admin_users():
    users = User.query.order_by(User.is_admin.desc(), User.id.asc()).all()

    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)
    start_7d = now - timedelta(days=7)
    start_30d = now - timedelta(days=30)

    stats = {}
    for u in users:
        q = VerifyJob.query.filter_by(user_id=u.id, status="success")
        today = q.filter(VerifyJob.finished_at >= start_today).count()
        week = q.filter(VerifyJob.finished_at >= start_7d).count()
        month = q.filter(VerifyJob.finished_at >= start_30d).count()
        total = q.count()
        stats[u.id] = {"today": today, "week": week, "month": month, "total": total}

    return render_template(
        "admin_users.html",
        title="Admin • Usuários",
        users=users,
        stats=stats,
    )


@bp.route("/users/create", methods=["POST"])
@login_required
def admin_create_user():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        flash("Informe username e password.", "error")
        return redirect(url_for("admin.admin_users"))

    if User.query.filter_by(username=username).first():
        flash("Usuário já existe.", "error")
        return redirect(url_for("admin.admin_users"))

    u = User(username=username, is_admin=False, is_banned=False)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()

    log_event("info", "admin", f"Usuário criado: '{username}'", user_id=current_user.id)
    flash("Usuário criado com sucesso.", "success")
    return redirect(url_for("admin.admin_users"))


@bp.route("/users/<int:user_id>/toggle-ban", methods=["POST"])
@login_required
def admin_toggle_ban(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    if u.is_admin:
        flash("Não é permitido banir um admin.", "error")
        return redirect(url_for("admin.admin_users"))

    u.is_banned = not u.is_banned
    db.session.commit()

    action = "banido" if u.is_banned else "desbanido"
    log_event("warning", "admin", f"Usuário '{u.username}' {action}", user_id=current_user.id)
    flash("Status atualizado.", "success")
    return redirect(url_for("admin.admin_users"))


@bp.route("/users/<int:user_id>", methods=["GET"])
@login_required
def admin_user_detail(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    jobs = (
        VerifyJob.query
        .filter_by(user_id=u.id)
        .order_by(VerifyJob.created_at.desc())
        .limit(50)
        .all()
    )

    return render_template(
        "admin_user_detail.html",
        title=f"Admin • {u.username}",
        u=u,
        jobs=jobs,
    )


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def admin_reset_password(user_id: int):
    u = db.session.get(User, user_id)
    if not u:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin.admin_users"))

    new_password = (request.form.get("new_password") or "").strip()
    if not new_password:
        flash("Informe a nova senha.", "error")
        return redirect(url_for("admin.admin_user_detail", user_id=user_id))

    u.set_password(new_password)
    db.session.commit()
    log_event("warning", "admin", f"Senha resetada para '{u.username}'", user_id=current_user.id)
    flash("Senha atualizada.", "success")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))


# ── Logs ─────────────────────────────────────────────────────────────────────

@bp.route("/logs", methods=["GET"])
@login_required
def admin_logs():
    q = AppLog.query

    # Filters
    filter_user = request.args.get("user", "").strip()
    filter_category = request.args.get("category", "").strip()
    filter_level = request.args.get("level", "").strip()
    filter_profile = request.args.get("profile", "").strip()

    if filter_user:
        u = User.query.filter_by(username=filter_user).first()
        if u:
            q = q.filter_by(user_id=u.id)
        else:
            q = q.filter_by(user_id=-1)  # no results
    if filter_category:
        q = q.filter_by(category=filter_category)
    if filter_level:
        q = q.filter_by(level=filter_level)
    if filter_profile:
        q = q.filter(AppLog.profile_id.contains(filter_profile))

    logs = q.order_by(AppLog.timestamp.desc()).limit(200).all()

    # Build user map for display
    user_ids = {l.user_id for l in logs if l.user_id}
    users_map = {}
    if user_ids:
        for u in User.query.filter(User.id.in_(user_ids)).all():
            users_map[u.id] = u.username

    # All usernames for the filter dropdown
    all_users = User.query.order_by(User.username).all()

    return render_template(
        "admin_logs.html",
        title="Admin • Logs",
        logs=logs,
        users_map=users_map,
        all_users=all_users,
        filter_user=filter_user,
        filter_category=filter_category,
        filter_level=filter_level,
        filter_profile=filter_profile,
    )


# ── System Settings ───────────────────────────────────────────────────────────

@bp.route("/settings", methods=["GET"])
@login_required
def admin_settings():
    import config as verif_config

    current_provider = SystemSetting.get("SMS_PROVIDER", verif_config.SMS_PROVIDER)

    settings = {
        "SMS_PROVIDER":          current_provider,
        "SMS24H_API_KEY":        SystemSetting.get("SMS24H_API_KEY",  verif_config.SMS24H_API_KEY),
        "SMS24H_COUNTRY":        SystemSetting.get("SMS24H_COUNTRY",  verif_config.SMS24H_COUNTRY),
        "SMS24H_SERVICE":        SystemSetting.get("SMS24H_SERVICE",  verif_config.SMS24H_SERVICE),
        "HEROSMS_API_KEY":       SystemSetting.get("HEROSMS_API_KEY", verif_config.HEROSMS_API_KEY),
        "HEROSMS_COUNTRY":       SystemSetting.get("HEROSMS_COUNTRY", verif_config.HEROSMS_COUNTRY),
        "HEROSMS_SERVICE":       SystemSetting.get("HEROSMS_SERVICE", verif_config.HEROSMS_SERVICE),
        "AI_PROVIDER":           SystemSetting.get("AI_PROVIDER",           "anthropic"),
        "ANTHROPIC_API_KEY_CNPJ": SystemSetting.get("ANTHROPIC_API_KEY_CNPJ", verif_config.ANTHROPIC_API_KEY),
        "ANTHROPIC_MODEL_CNPJ":  SystemSetting.get("ANTHROPIC_MODEL_CNPJ",  verif_config.CLAUDE_FAST_MODEL),
        "OPENAI_API_KEY_CNPJ":   SystemSetting.get("OPENAI_API_KEY_CNPJ",   verif_config.OPENAI_API_KEY),
        "OPENAI_MODEL_CNPJ":     SystemSetting.get("OPENAI_MODEL_CNPJ",     getattr(verif_config, "OPENAI_MODEL", "gpt-4.1-mini")),
    }
    return render_template("admin_settings.html", title="Admin • Configurações", settings=settings)


@bp.route("/settings", methods=["POST"])
@login_required
def admin_settings_save():
    import config as verif_config

    fields = [
        "SMS_PROVIDER",
        "SMS24H_API_KEY", "SMS24H_COUNTRY", "SMS24H_SERVICE",
        "HEROSMS_API_KEY", "HEROSMS_COUNTRY", "HEROSMS_SERVICE",
        "AI_PROVIDER",
        "ANTHROPIC_API_KEY_CNPJ", "ANTHROPIC_MODEL_CNPJ",
        "OPENAI_API_KEY_CNPJ", "OPENAI_MODEL_CNPJ",
    ]
    for field in fields:
        val = (request.form.get(field) or "").strip()
        if val:
            SystemSetting.set(field, val)

    log_event("info", "admin", "Configurações de SMS atualizadas", user_id=current_user.id)
    flash("Configurações salvas com sucesso.", "success")
    return redirect(url_for("admin.admin_settings"))


@bp.route("/settings/test-balance", methods=["POST"])
@login_required
def admin_test_balance():
    """Return the balance for a given provider (AJAX)."""
    import config as verif_config

    provider = (request.json or {}).get("provider", "sms24h")

    try:
        if provider == "herosms":
            from services.herosms import HeroSMSService
            api_key = SystemSetting.get("HEROSMS_API_KEY", verif_config.HEROSMS_API_KEY)
            country = SystemSetting.get("HEROSMS_COUNTRY", verif_config.HEROSMS_COUNTRY)
            service = SystemSetting.get("HEROSMS_SERVICE", verif_config.HEROSMS_SERVICE)
            svc = HeroSMSService(api_key, country, service)
        else:
            from services.sms24h import SMS24HService
            api_key = SystemSetting.get("SMS24H_API_KEY", verif_config.SMS24H_API_KEY)
            country = SystemSetting.get("SMS24H_COUNTRY", verif_config.SMS24H_COUNTRY)
            service = SystemSetting.get("SMS24H_SERVICE", verif_config.SMS24H_SERVICE)
            svc = SMS24HService(api_key, country, service)

        balance = svc.get_balance()
        if balance is not None:
            return jsonify({"ok": True, "balance": balance})
        return jsonify({"ok": False, "error": "Sem resposta da API (chave inválida?)"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
