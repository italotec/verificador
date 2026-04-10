"""
Periodic tasks run by Celery Beat.

- daily_waba_check: Enqueue browser checks for all WABAs needing status updates
- check_review_timeouts: Move stale em_revisao WABAs to nao_verificou
"""

import logging
from celery_app import celery

logger = logging.getLogger(__name__)


def _get_flask_app():
    from web_app import create_app
    return create_app()


@celery.task
def daily_waba_check():
    """
    Enqueue check_waba_status for all WABAs in em_revisao or monitorando_limite.
    Called daily at 8 AM by Celery Beat.
    """
    app = _get_flask_app()
    with app.app_context():
        from web_app.models import WabaRecord
        from tasks.check_waba import check_waba_status

        statuses_to_check = ["em_revisao", "monitorando_limite", "nao_verificou"]
        wabas = WabaRecord.query.filter(WabaRecord.status.in_(statuses_to_check)).all()

        enqueued = 0
        for waba in wabas:
            check_waba_status.apply_async(args=[waba.id], queue="check")
            enqueued += 1

        logger.info(f"Daily WABA check: enqueued {enqueued} checks")
        return {"enqueued": enqueued}


@celery.task
def check_review_timeouts():
    """
    Check all WABAs in em_revisao for 24h timeout.
    Called hourly by Celery Beat.
    """
    app = _get_flask_app()
    with app.app_context():
        from services.status_manager import StatusManager

        count = StatusManager.check_all_review_timeouts()
        if count:
            logger.info(f"Review timeout: transitioned {count} WABAs to nao_verificou")
        return {"transitioned": count}
