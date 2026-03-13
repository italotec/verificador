import secrets
import traceback
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from . import db, login_manager


class User(db.Model, UserMixin):
    id            = db.Column(db.Integer,     primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin      = db.Column(db.Boolean,     default=False, nullable=False)
    is_banned     = db.Column(db.Boolean,     default=False, nullable=False)

    # Unique token used by the local agent to authenticate with the VPS.
    # Each user gets their own token so the VPS knows which agent belongs to whom.
    agent_token   = db.Column(db.String(64),  unique=True, nullable=True)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)

    def generate_agent_token(self):
        """Generate (or regenerate) a cryptographically random agent token."""
        self.agent_token = secrets.token_urlsafe(32)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class ProfileSnapshot(db.Model):
    """
    Profile data pushed periodically by the local agent.
    Used in VPS mode so the dashboard doesn't need to reach AdsPower directly.
    Each snapshot is owned by the user whose agent pushed it.
    """
    profile_id = db.Column(db.String(64), primary_key=True)
    name       = db.Column(db.String(255), default="", nullable=False)
    group_name = db.Column(db.String(64),  default="", nullable=False)  # "Verificar" | "Verificadas"
    remark     = db.Column(db.Text,        default="", nullable=False)
    synced_at  = db.Column(db.DateTime,    default=datetime.utcnow, nullable=False)

    # Which user's agent pushed this profile
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


class WorkerCommand(db.Model):
    """
    Commands queued by the VPS for the local agent to execute
    (e.g. open a browser profile).
    """
    id           = db.Column(db.Integer,    primary_key=True)
    command_type = db.Column(db.String(32), nullable=False)   # "open_browser"
    profile_id   = db.Column(db.String(64), nullable=False)
    status       = db.Column(db.String(16), default="pending", nullable=False)  # pending / done
    created_at   = db.Column(db.DateTime,   default=datetime.utcnow, nullable=False)


class VerifyJob(db.Model):
    """Tracks each verification attempt for an AdsPower profile."""
    id = db.Column(db.Integer, primary_key=True)

    # AdsPower profile user_id (e.g. "jfhx9du")
    profile_id = db.Column(db.String(64), nullable=False, index=True)

    # Which web user triggered this job
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    # queued / running / success / error
    status = db.Column(db.String(32), default="idle", nullable=False)

    # Optional Business Manager ID passed before running
    business_id = db.Column(db.String(64), default="", nullable=False)

    # Relative path to the captured screenshot (inside static/screenshots/)
    screenshot_path = db.Column(db.String(512), default="", nullable=False)

    last_message = db.Column(db.Text, default="", nullable=False)

    started_at  = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AppLog(db.Model):
    """Persistent application log for admin debugging."""
    id         = db.Column(db.Integer, primary_key=True)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    level      = db.Column(db.String(16), default="info", nullable=False)      # info / warning / error
    category   = db.Column(db.String(32), default="general", nullable=False)   # job / agent / auth / admin / worker
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    profile_id = db.Column(db.String(64), nullable=True)
    job_id     = db.Column(db.Integer, nullable=True)
    message    = db.Column(db.Text, nullable=False)
    detail     = db.Column(db.Text, nullable=True)


def log_event(level: str, category: str, message: str, *,
              detail: str | None = None, user_id: int | None = None,
              profile_id: str | None = None, job_id: int | None = None):
    """Create an AppLog entry. Safe to call from any context."""
    try:
        entry = AppLog(
            level=level, category=category, message=message,
            detail=detail, user_id=user_id,
            profile_id=profile_id, job_id=job_id,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
