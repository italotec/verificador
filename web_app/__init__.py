import os
from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from flask_sock import Sock
from .config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"
sock = Sock()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    sock.init_app(app)

    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.admin import bp as admin_bp
    from .routes.jobs import bp as jobs_bp
    from .routes.worker import bp as worker_bp
    from .routes.agent_ws import bp as agent_ws_bp, handle_ws
    from .routes.account import bp as account_bp
    from .routes.profiles import bp as profiles_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(worker_bp)   # HTTP worker API (X-Worker-Key auth)
    app.register_blueprint(agent_ws_bp) # agent status + WS
    app.register_blueprint(account_bp)  # user account / token page
    app.register_blueprint(profiles_bp) # bulk profile creator (admin)

    @sock.route("/agent/ws")
    def agent_ws_route(ws):
        handle_ws(ws)

    @app.before_request
    def block_banned():
        if current_user.is_authenticated and getattr(current_user, "is_banned", False):
            logout_user()
            flash("Sua conta está banida. Fale com o suporte.", "error")
            return redirect(url_for("auth.login_get"))

    with app.app_context():
        from . import models  # noqa
        db.create_all()

        db.session.execute(db.text("PRAGMA journal_mode=WAL"))
        db.session.commit()

        # Reset jobs left running from a previous restart
        from .models import VerifyJob
        stuck = VerifyJob.query.filter_by(status="running").all()
        for j in stuck:
            j.status = "error"
            j.last_message = "Interrompido: servidor reiniciou."
        if stuck:
            db.session.commit()

        # Seed default admin user
        from .models import User
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin", is_admin=True, is_banned=False)
            admin.set_password("admin")
            db.session.add(admin)
            db.session.commit()

        # Ensure every user has an agent token
        needs_token = User.query.filter_by(agent_token=None).all()
        for u in needs_token:
            u.generate_agent_token()
        if needs_token:
            db.session.commit()

        # Ensure screenshots directory exists
        screenshots_dir = os.path.join(app.static_folder, "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)

    return app
