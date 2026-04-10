"""
Database initialisation script.

Run this once to set up the PostgreSQL database:

    python scripts/init_db.py

This will:
1. Create all tables (or apply Flask-Migrate migrations if they exist)
2. Seed the default admin user
3. Migrate existing ProfileSnapshot data → WabaRecord entries

For subsequent schema changes use:
    flask db migrate -m "description"
    flask db upgrade
"""

import sys
import os
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from web_app import create_app, db
from web_app.models import (
    User, ProfileSnapshot, WabaRecord,
    WABA_STATUS_AGUARDANDO, WABA_STATUS_EM_REVISAO,
)
import config as app_config


def migrate_profiles_to_waba_records():
    """
    Convert existing ProfileSnapshot records to WabaRecord entries.

    Mapping:
    - "Verificar" group  → status = aguardando
    - "Verificadas" group → status = em_revisao (submitted, awaiting confirmation)
    """
    snaps = ProfileSnapshot.query.all()
    if not snaps:
        print("[migrate] No ProfileSnapshot records found")
        return 0

    created = 0
    skipped = 0

    for snap in snaps:
        # Skip if WabaRecord already exists for this profile
        existing = WabaRecord.query.filter_by(profile_id=snap.profile_id).first()
        if existing:
            skipped += 1
            continue

        # Parse GERADOR block from remark
        gerador_data = _parse_gerador_block(snap.remark)

        # Determine status from group
        if snap.group_name == app_config.VERIFICADAS_GROUP_NAME:
            status = WABA_STATUS_EM_REVISAO
        else:
            status = WABA_STATUS_AGUARDANDO

        waba = WabaRecord(
            profile_id=snap.profile_id,
            user_id=snap.user_id,
            waba_name=snap.name,
            status=status,
        )

        if gerador_data:
            waba.run_id = gerador_data.get("run_id")
            waba.business_id = gerador_data.get("business_id")
            waba.bm_created = bool(gerador_data.get("business_id"))
            waba.business_info_done = bool(gerador_data.get("business_info_done"))
            waba.domain_done = bool(gerador_data.get("domain_done"))
            waba.waba_created = bool(gerador_data.get("waba_done"))

        # Check for VERIFICADA marker
        if app_config.VERIFICADA_REMARK_MARKER in (snap.remark or ""):
            waba.verification_sent = True

        db.session.add(waba)
        created += 1

    db.session.commit()
    print(f"[migrate] Created {created} WabaRecord(s), skipped {skipped} existing")
    return created


def _parse_gerador_block(remark: str) -> dict | None:
    if not remark:
        return None
    marker = app_config.GERADOR_REMARK_MARKER
    if marker not in remark:
        return None
    _, _, tail = remark.partition(marker)
    try:
        return json.loads(tail.strip())
    except Exception:
        return None


def main():
    app = create_app()
    with app.app_context():
        print("[init_db] Creating tables...")

        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite"):
            db.create_all()
            print("[init_db] SQLite tables created")
        else:
            # PostgreSQL — use Flask-Migrate
            from flask_migrate import upgrade
            try:
                upgrade()
                print("[init_db] Flask-Migrate upgrade applied")
            except Exception as e:
                print(f"[init_db] Migrate upgrade failed ({e}), falling back to create_all")
                db.create_all()

        print("[init_db] Migrating ProfileSnapshot → WabaRecord...")
        migrate_profiles_to_waba_records()

        print("[init_db] Done!")


if __name__ == "__main__":
    main()
