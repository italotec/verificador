import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent  # I:\Verificador Interface\
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "verificador-secret-key-2025")

    # ── Database ─────────────────────────────────────────────────────────────
    # Default: PostgreSQL. Set DATABASE_URL env var to override.
    # Falls back to SQLite for quick local dev if USE_SQLITE=1.
    _use_sqlite = os.getenv("USE_SQLITE", "0").strip() in ("1", "true", "yes")
    if _use_sqlite:
        (BASE_DIR / 'instance').mkdir(parents=True, exist_ok=True)
        # Use forward slashes (as_posix) to avoid Windows backslash issues in SQLite URIs
        _db_path = (BASE_DIR / 'instance' / 'verificador.db').as_posix()
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{_db_path}"
    else:
        SQLALCHEMY_DATABASE_URI = os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/verificador"
        )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Worker / VPS mode ────────────────────────────────────────────────────
    USE_WORKER: bool = os.getenv("USE_WORKER", "0").strip() in ("1", "true", "yes")
    WORKER_API_KEY: str = os.getenv("WORKER_API_KEY", "change-this-secret-key")

    # ── Celery + Redis ───────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

    # Max concurrent AdsPower profiles running at once (per worker machine)
    MAX_CONCURRENT_PROFILES: int = int(os.getenv("MAX_CONCURRENT_PROFILES", "5"))

    # ── Feature flags ────────────────────────────────────────────────────────
    USE_CELERY: bool = os.getenv("USE_CELERY", "1").strip() in ("1", "true", "yes")
