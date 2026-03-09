from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from .. import db
from ..models import User, VerifyJob

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
    flash("Senha atualizada.", "success")
    return redirect(url_for("admin.admin_user_detail", user_id=user_id))
