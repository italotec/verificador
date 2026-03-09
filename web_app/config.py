import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent  # I:\Verificador Interface\


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "verificador-secret-key-2025")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'verificador.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Worker / VPS mode ────────────────────────────────────────────────────
    # Set USE_WORKER=1 on the VPS. Leave 0 for the local Windows machine.
    USE_WORKER: bool = os.getenv("USE_WORKER", "0").strip() in ("1", "true", "yes")

    # Secret shared between the VPS Flask app and the local worker.py.
    # Change this to something random before deploying!
    WORKER_API_KEY: str = os.getenv("WORKER_API_KEY", "change-this-secret-key")
