from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from ..models import User

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET"])
def login_get():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    return render_template("login.html", title="Login")


@bp.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        flash("Login inválido.", "error")
        return redirect(url_for("auth.login_get"))

    if user.is_banned:
        flash("Sua conta está banida. Fale com o suporte.", "error")
        return redirect(url_for("auth.login_get"))

    login_user(user)
    return redirect(url_for("dashboard.index"))


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login_get"))
