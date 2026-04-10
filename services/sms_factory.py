"""
SMS service factory.

Reads the active provider from SystemSetting (DB) and returns the
correct service instance.  Falls back to config.py defaults if the DB
is not available (e.g. CLI / local mode without Flask context).
"""
from __future__ import annotations


def get_sms_service(sms_payload: dict | None = None):
    """
    Return the active SMS service instance.

    sms_payload — when called from the agent, pass job["sms"] here.
                  It contains provider + credentials sent by the VPS,
                  bypassing the need to read the DB from the local machine.
    Falls back to DB read (Flask context) or config.py defaults.
    """
    import config as app_config

    # Prefer the payload injected by the VPS into the job message
    if sms_payload and sms_payload.get("provider"):
        provider = sms_payload["provider"]
        api_key  = sms_payload.get("api_key", "")
        country  = sms_payload.get("country", "73")
        service  = sms_payload.get("service", "fb")
        print(f"[SMS_FACTORY] Using provider from job payload: {provider}")
    else:
        settings = _read_all_settings()
        provider = settings.get("SMS_PROVIDER", app_config.SMS_PROVIDER) or "sms24h"
        print(f"[SMS_FACTORY] Using provider from DB/config: {provider}")
        if provider == "herosms":
            api_key = settings.get("HEROSMS_API_KEY") or app_config.HEROSMS_API_KEY
            country = settings.get("HEROSMS_COUNTRY") or app_config.HEROSMS_COUNTRY
            service = settings.get("HEROSMS_SERVICE") or app_config.HEROSMS_SERVICE
        else:
            api_key = settings.get("SMS24H_API_KEY") or app_config.SMS24H_API_KEY
            country = settings.get("SMS24H_COUNTRY") or app_config.SMS24H_COUNTRY
            service = settings.get("SMS24H_SERVICE") or app_config.SMS24H_SERVICE

    if provider == "herosms":
        from services.herosms import HeroSMSService
        return HeroSMSService(api_key=api_key, country=country, service=service)
    else:
        from services.sms24h import SMS24HService
        return SMS24HService(api_key=api_key, country=country, service=service)


def _read_all_settings() -> dict:
    """
    Read all system_setting rows and return as a plain dict.

    Strategy (tried in order):
    1. Flask app context (SQLAlchemy) — works when called from a Celery task
       or any code that already has an app context.
    2. Direct SQLite read — works from agent.py / main.py where there is no
       Flask context.  Uses the same DB path as the Flask app config.
    """
    # ── Strategy 1: Flask app context ────────────────────────────────────────
    try:
        from flask import current_app  # raises RuntimeError if no context
        _ = current_app._get_current_object()  # confirm context is live
        from web_app.models import SystemSetting
        rows = SystemSetting.query.all()
        return {r.key: r.value for r in rows}
    except Exception:
        pass

    # ── Strategy 2: Direct SQLite read ───────────────────────────────────────
    try:
        import sqlite3
        from pathlib import Path
        import config as app_config

        # DB lives at <project_root>/instance/verificador.db
        # config.BASE_DIR is the project root (I:\Verificador Interface\)
        db_path = Path(app_config.BASE_DIR) / "instance" / "verificador.db"

        if not db_path.exists():
            return {}

        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            cur = conn.execute("SELECT key, value FROM system_setting")
            return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        print(f"[SMS_FACTORY] Could not read settings from DB: {e}")

    return {}
