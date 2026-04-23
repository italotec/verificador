import os
from flask import Flask, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, logout_user
from flask_migrate import Migrate
from flask_sock import Sock
from .config import Config

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login_get"
sock = Sock()


def _sqlite_migrate(db):
    """Add new columns to existing SQLite tables without losing data."""
    migrations = [
        # verify_job new columns
        ("verify_job", "waba_record_id",  "INTEGER"),
        ("verify_job", "job_type",        "VARCHAR(32) DEFAULT 'create_verify'"),
        ("verify_job", "priority",        "INTEGER DEFAULT 0"),
        ("verify_job", "retry_count",     "INTEGER DEFAULT 0"),
        ("verify_job", "max_retries",     "INTEGER DEFAULT 3"),
        ("verify_job", "scheduled_at",    "DATETIME"),
        # profile_snapshot new columns
        ("profile_snapshot", "user_id",   "INTEGER"),
        # waba_record proxy
        ("waba_record", "proxy_port",     "INTEGER"),
        # system_setting is created via db.create_all() — no ALTER needed
    ]
    for table, column, col_type in migrations:
        try:
            db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
            db.session.commit()
        except Exception:
            db.session.rollback()  # Column already exists — that's fine


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
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
    from .routes.errors import bp as errors_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(worker_bp)
    app.register_blueprint(agent_ws_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(errors_bp)

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
        from . import models  # noqa — register models with SQLAlchemy

        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")

        try:
            # Create tables if not using migrations (dev convenience)
            # In production, use: flask db upgrade
            if db_uri.startswith("sqlite"):
                db.create_all()
                db.session.execute(db.text("PRAGMA journal_mode=WAL"))
                db.session.commit()
                _sqlite_migrate(db)
            else:
                # PostgreSQL — create all tables directly (migrations handled via flask db upgrade)
                db.create_all()

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

        except Exception as e:
            print(f"[startup] DB setup skipped (DB not ready?): {e}")
            print("[startup] Run 'python scripts/init_db.py' once the database is available.")

        # Ensure screenshots directory exists (always, even without DB)
        screenshots_dir = os.path.join(app.static_folder, "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)
        os.makedirs(os.path.join(screenshots_dir, "checks"), exist_ok=True)

    return app
