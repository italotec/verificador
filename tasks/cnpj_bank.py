"""
Celery tasks for CNPJ bank pre-generation.
Replaces the raw threading.Thread + queue.Queue approach in the old Gerador CNPJ app.

Tasks:
    generate_cnpj_run  — run the full pipeline for one CNPJ, save as pre-generated
    refill_bank        — check bank level and spawn generation tasks for any deficit

Beat schedule (configured in celery_app.py):
    refill_bank runs every 10 minutes.

Worker note: PDF generation uses Playwright (sync chromium). Workers must use
the prefork pool (default). Do NOT use eventlet or gevent pools.
"""

import logging

from celery_app import celery

logger = logging.getLogger(__name__)


def _get_flask_app():
    from web_app import create_app
    return create_app()


@celery.task(
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    time_limit=600,           # PDF generation + SSH deploy can take a while
    soft_time_limit=540,
    name="tasks.cnpj_bank.generate_cnpj_run",
)
def generate_cnpj_run(self, specific_cnpj: str | None = None):
    """
    Generate one full CNPJ run (search → lookup → HTML → deploy → PDF → DB).
    Stores the result as is_pre_generated=True in the CNPJRun table.
    """
    app = _get_flask_app()
    with app.app_context():
        try:
            from web_app import db
            from web_app.models import CNPJRun
            from services.cnpj_pipeline import generate_cnpj_run as _gen

            run = _gen(specific_cnpj=specific_cnpj)

            # Mark as pre-generated for the bank
            run.is_pre_generated = True
            run.claimed_at = None
            db.session.commit()

            logger.info(f"[BANK] Pre-generated run {run.id} — CNPJ {run.cnpj}")
            return {"run_id": run.id, "cnpj": run.cnpj}

        except Exception as e:
            logger.error(f"[BANK] Generation failed: {e}")
            raise self.retry(exc=e)


@celery.task(
    name="tasks.cnpj_bank.refill_bank",
    time_limit=60,
)
def refill_bank():
    """
    Check the current bank level against CNPJ_BANK_TARGET.
    Spawn generate_cnpj_run tasks for any deficit.
    """
    app = _get_flask_app()
    with app.app_context():
        import config as cfg
        from web_app.models import CNPJRun

        if not cfg.CNPJ_BANK_ENABLED:
            return {"skipped": True, "reason": "CNPJ_BANK_ENABLED is False"}

        current = CNPJRun.query.filter_by(is_pre_generated=True, claimed_at=None).count()
        target = cfg.CNPJ_BANK_TARGET
        deficit = max(0, target - current)

        for _ in range(deficit):
            generate_cnpj_run.apply_async(queue="cnpj_bank")

        logger.info(f"[BANK] current={current} target={target} spawned={deficit}")
        return {"current": current, "target": target, "spawned": deficit}
