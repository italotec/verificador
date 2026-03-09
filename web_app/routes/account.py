"""
Account page — each user can view and regenerate their agent token.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from .. import db

bp = Blueprint("account", __name__, url_prefix="/account")


@bp.route("/")
@login_required
def account():
    return render_template("account.html", title="Minha Conta")


@bp.route("/regenerate-token", methods=["POST"])
@login_required
def regenerate_token():
    current_user.generate_agent_token()
    db.session.commit()
    flash("Token regenerado com sucesso. Reconecte o agent com o novo token.", "success")
    return redirect(url_for("account.account"))
