import secrets
import traceback
from datetime import datetime, timedelta
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
    group_name = db.Column(db.String(64),  default="", nullable=False)
    remark     = db.Column(db.Text,        default="", nullable=False)
    synced_at  = db.Column(db.DateTime,    default=datetime.utcnow, nullable=False)

    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


# ── WABA Status Constants ────────────────────────────────────────────────────

WABA_STATUS_AGUARDANDO         = "aguardando"
WABA_STATUS_EXECUTANDO         = "executando"
WABA_STATUS_EM_REVISAO         = "em_revisao"
WABA_STATUS_NAO_VERIFICOU      = "nao_verificou"
WABA_STATUS_MONITORANDO_LIMITE = "monitorando_limite"
WABA_STATUS_250                = "250"
WABA_STATUS_2K                 = "2k"
WABA_STATUS_RESTRITA           = "restrita"
WABA_STATUS_DESATIVADA         = "desativada"
WABA_STATUS_ERRO               = "erro"

ALL_WABA_STATUSES = [
    WABA_STATUS_AGUARDANDO,
    WABA_STATUS_EXECUTANDO,
    WABA_STATUS_EM_REVISAO,
    WABA_STATUS_NAO_VERIFICOU,
    WABA_STATUS_MONITORANDO_LIMITE,
    WABA_STATUS_250,
    WABA_STATUS_2K,
    WABA_STATUS_RESTRITA,
    WABA_STATUS_DESATIVADA,
    WABA_STATUS_ERRO,
]

# Portuguese labels for dashboard display
WABA_STATUS_LABELS = {
    WABA_STATUS_AGUARDANDO:         "Na Fila",
    WABA_STATUS_EXECUTANDO:         "Executando",
    WABA_STATUS_EM_REVISAO:         "Em Revisão",
    WABA_STATUS_NAO_VERIFICOU:      "Não Verificou",
    WABA_STATUS_MONITORANDO_LIMITE: "Monitorando Limite",
    WABA_STATUS_250:                "BM 250",
    WABA_STATUS_2K:                 "BM 2K",
    WABA_STATUS_RESTRITA:           "Restrita",
    WABA_STATUS_DESATIVADA:         "Desativada",
    WABA_STATUS_ERRO:               "Erro",
}

# Badge colors (Tailwind classes) for each status
WABA_STATUS_COLORS = {
    WABA_STATUS_AGUARDANDO:         "bg-blue-600",
    WABA_STATUS_EXECUTANDO:         "bg-yellow-500",
    WABA_STATUS_EM_REVISAO:         "bg-orange-500",
    WABA_STATUS_NAO_VERIFICOU:      "bg-rose-400",
    WABA_STATUS_MONITORANDO_LIMITE: "bg-indigo-500",
    WABA_STATUS_250:                "bg-amber-500",
    WABA_STATUS_2K:                 "bg-green-500",
    WABA_STATUS_RESTRITA:           "bg-red-600",
    WABA_STATUS_DESATIVADA:         "bg-zinc-500",
    WABA_STATUS_ERRO:               "bg-red-400",
}


class WabaRecord(db.Model):
    """Central tracking entity for each WABA lifecycle."""
    __tablename__ = "waba_record"

    id          = db.Column(db.Integer, primary_key=True)
    profile_id  = db.Column(db.String(64), db.ForeignKey("profile_snapshot.profile_id"), nullable=True, index=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    waba_id     = db.Column(db.String(64),  nullable=True)   # Meta WABA ID once created
    waba_name   = db.Column(db.String(255), nullable=True)
    business_id = db.Column(db.String(64),  nullable=True)   # Facebook BM ID
    domain      = db.Column(db.String(255), nullable=True)
    run_id      = db.Column(db.Integer,     nullable=True)   # Gerador run_id

    # ── Status lifecycle ─────────────────────────────────────────────────────
    status = db.Column(db.String(32), default=WABA_STATUS_AGUARDANDO, nullable=False, index=True)

    # ── Step flags (moved from AdsPower remarks) ─────────────────────────────
    bm_created        = db.Column(db.Boolean, default=False, nullable=False)
    business_info_done = db.Column(db.Boolean, default=False, nullable=False)
    domain_done       = db.Column(db.Boolean, default=False, nullable=False)
    domain_zone       = db.Column(db.String(255), nullable=True)
    waba_created      = db.Column(db.Boolean, default=False, nullable=False)
    waba_verified     = db.Column(db.Boolean, default=False, nullable=False)
    verification_sent = db.Column(db.Boolean, default=False, nullable=False)

    # ── Limit monitoring ─────────────────────────────────────────────────────
    messaging_limit     = db.Column(db.String(32), nullable=True)  # TIER_250, TIER_1K, etc.
    last_limit_check    = db.Column(db.DateTime,   nullable=True)
    limit_first_seen_at = db.Column(db.DateTime,   nullable=True)

    # ── Browser-check fields ─────────────────────────────────────────────────
    phone_number                 = db.Column(db.String(32),  nullable=True)
    phone_quality_rating         = db.Column(db.String(16),  nullable=True)
    account_review_status        = db.Column(db.String(32),  nullable=True)
    business_verification_status = db.Column(db.String(32),  nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────────
    submitted_at  = db.Column(db.DateTime, nullable=True)   # sent to central de segurança
    verified_at   = db.Column(db.DateTime, nullable=True)   # "Verificada" confirmed
    restricted_at = db.Column(db.DateTime, nullable=True)
    disabled_at   = db.Column(db.DateTime, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # ── Proxy ────────────────────────────────────────────────────────────────
    proxy_port = db.Column(db.Integer, nullable=True)  # DataImpulse fixed port (10015+)

    # ── Error tracking ───────────────────────────────────────────────────────
    last_error     = db.Column(db.Text,       nullable=True)
    error_count    = db.Column(db.Integer,    default=0, nullable=False)
    last_screenshot = db.Column(db.String(512), nullable=True)

    # ── Relationships ────────────────────────────────────────────────────────
    transitions = db.relationship("StatusTransition", backref="waba_record", lazy="dynamic")
    error_reports = db.relationship("ErrorReport", backref="waba_record", lazy="dynamic")
    jobs = db.relationship("VerifyJob", backref="waba_record", lazy="dynamic")

    def __repr__(self):
        return f"<WabaRecord {self.id} profile={self.profile_id} status={self.status}>"


class StatusTransition(db.Model):
    """Audit trail for WABA status changes."""
    __tablename__ = "status_transition"

    id              = db.Column(db.Integer, primary_key=True)
    waba_record_id  = db.Column(db.Integer, db.ForeignKey("waba_record.id"), nullable=False, index=True)
    from_status     = db.Column(db.String(32), nullable=False)
    to_status       = db.Column(db.String(32), nullable=False)
    reason          = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<StatusTransition {self.from_status} → {self.to_status}>"


class ErrorReport(db.Model):
    """Structured error tracking with LLM analysis."""
    __tablename__ = "error_report"

    id              = db.Column(db.Integer, primary_key=True)
    waba_record_id  = db.Column(db.Integer, db.ForeignKey("waba_record.id"), nullable=True, index=True)
    job_id          = db.Column(db.Integer, db.ForeignKey("verify_job.id"), nullable=True)

    error_type      = db.Column(db.String(64),  nullable=False)  # selector_not_found, timeout, api_error, etc.
    error_message   = db.Column(db.Text,        nullable=False)
    screenshot_path = db.Column(db.String(512), nullable=True)
    page_url        = db.Column(db.String(1024), nullable=True)
    step_name       = db.Column(db.String(128), nullable=True)

    llm_analysis    = db.Column(db.Text, nullable=True)
    fix_suggestion  = db.Column(db.Text, nullable=True)   # prompt format for Claude Code
    is_recurring    = db.Column(db.Boolean, default=False, nullable=False)
    resolved        = db.Column(db.Boolean, default=False, nullable=False)

    created_at      = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ErrorReport {self.id} type={self.error_type} recurring={self.is_recurring}>"


class BrowserRecording(db.Model):
    """Recorded automation flows for replay."""
    __tablename__ = "browser_recording"

    id               = db.Column(db.Integer, primary_key=True)
    task_name        = db.Column(db.String(128), unique=True, nullable=False)
    steps_json       = db.Column(db.Text, nullable=True)     # raw recorded steps
    generated_script = db.Column(db.Text, nullable=True)     # auto-generated Playwright script
    polished_script  = db.Column(db.Text, nullable=True)     # Claude-polished script
    is_tested        = db.Column(db.Boolean, default=False, nullable=False)
    success_count    = db.Column(db.Integer, default=0, nullable=False)
    failure_count    = db.Column(db.Integer, default=0, nullable=False)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<BrowserRecording {self.task_name} tested={self.is_tested}>"


class WorkerCommand(db.Model):
    """Commands queued by the VPS for the local agent to execute."""
    id           = db.Column(db.Integer,    primary_key=True)
    command_type = db.Column(db.String(32), nullable=False)
    profile_id   = db.Column(db.String(64), nullable=False)
    status       = db.Column(db.String(16), default="pending", nullable=False)
    created_at   = db.Column(db.DateTime,   default=datetime.utcnow, nullable=False)


class VerifyJob(db.Model):
    """Tracks each verification attempt for an AdsPower profile."""
    __tablename__ = "verify_job"

    id = db.Column(db.Integer, primary_key=True)

    profile_id = db.Column(db.String(64), nullable=False, index=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    # Link to WabaRecord
    waba_record_id = db.Column(db.Integer, db.ForeignKey("waba_record.id"), nullable=True, index=True)

    # Job type: create_verify (Flow A), check_status (Flow B), check_limit
    job_type = db.Column(db.String(32), default="create_verify", nullable=False)

    # queued / running / success / error
    status = db.Column(db.String(32), default="idle", nullable=False)

    # Priority: higher = picked sooner
    priority = db.Column(db.Integer, default=0, nullable=False)

    # Retry tracking
    retry_count = db.Column(db.Integer, default=0, nullable=False)
    max_retries = db.Column(db.Integer, default=3, nullable=False)

    # Deferred execution
    scheduled_at = db.Column(db.DateTime, nullable=True)

    # Optional Business Manager ID passed before running
    business_id = db.Column(db.String(64), default="", nullable=False)

    screenshot_path = db.Column(db.String(512), default="", nullable=False)
    last_message    = db.Column(db.Text, default="", nullable=False)

    started_at  = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AppLog(db.Model):
    """Persistent application log for admin debugging."""
    id         = db.Column(db.Integer, primary_key=True)
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    level      = db.Column(db.String(16), default="info", nullable=False)
    category   = db.Column(db.String(32), default="general", nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    profile_id = db.Column(db.String(64), nullable=True)
    job_id     = db.Column(db.Integer, nullable=True)
    message    = db.Column(db.Text, nullable=False)
    detail     = db.Column(db.Text, nullable=True)


# ── Gerador CNPJ (native) ────────────────────────────────────────────────────

class UsedCNPJ(db.Model):
    """Global uniqueness lock — prevents generating the same CNPJ twice."""
    __tablename__ = "used_cnpj"

    id         = db.Column(db.Integer, primary_key=True)
    cnpj       = db.Column(db.String(14), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CNPJRun(db.Model):
    """Tracks each generated CNPJ package (website + PDF + deployment)."""
    __tablename__ = "cnpj_run"

    id             = db.Column(db.Integer, primary_key=True)
    cnpj           = db.Column(db.String(14), nullable=False, index=True)
    razao_social   = db.Column(db.String(255), nullable=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    day_key        = db.Column(db.String(10), nullable=False, index=True)

    # Storage paths (relative to GERADOR_STORAGE_DIR)
    folder_rel     = db.Column(db.String(500), nullable=False)
    index_rel      = db.Column(db.String(500), nullable=False)
    link_rel       = db.Column(db.String(500), nullable=False)
    pdf_rel        = db.Column(db.String(500), nullable=False)

    # Deployment
    site_url       = db.Column(db.String(500), nullable=True)
    deploy_url     = db.Column(db.String(500), nullable=True)

    # Bank pre-generation
    is_pre_generated = db.Column(db.Boolean, default=False, nullable=False)
    claimed_at       = db.Column(db.DateTime, nullable=True)

    # Company raw data cache (JSON blob)
    data_json      = db.Column(db.Text, nullable=True)


class SystemSetting(db.Model):
    """Key-value store for admin-configurable system settings."""
    __tablename__ = "system_setting"

    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key: str, value: str):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()


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


def delete_waba_cascade(waba_record):
    """Delete a WabaRecord and all dependent rows (no DB-level cascade configured)."""
    StatusTransition.query.filter_by(waba_record_id=waba_record.id).delete()
    ErrorReport.query.filter(ErrorReport.waba_record_id == waba_record.id).delete()
    VerifyJob.query.filter_by(waba_record_id=waba_record.id).delete()
    db.session.delete(waba_record)
