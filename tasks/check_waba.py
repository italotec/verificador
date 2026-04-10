"""
Flow B: Check WABA status via browser automation.

Opens AdsPower browser and checks:
1. Central de Segurança for "Verificada" status
2. Sending limits (WhatsApp Manager)
3. Restricted/disabled status
"""

import logging
import traceback
from datetime import datetime

from celery_app import celery

logger = logging.getLogger(__name__)


def _get_flask_app():
    from web_app import create_app
    return create_app()


@celery.task(bind=True, max_retries=1, default_retry_delay=120)
def check_waba_status(self, waba_record_id: int):
    """
    Check a single WABA's status via browser automation.
    """
    app = _get_flask_app()
    with app.app_context():
        from web_app import db
        from web_app.models import WabaRecord
        from services.waba_checker import WabaChecker
        from services.error_analyzer import analyze_error

        waba = db.session.get(WabaRecord, waba_record_id)
        if not waba:
            logger.error(f"WabaRecord {waba_record_id} not found")
            return {"status": "error", "message": "WabaRecord not found"}

        try:
            checker = WabaChecker()
            result = checker.check(waba)
            logger.info(f"WabaRecord {waba_record_id} check result: {result}")
            return {"status": "success", "result": result}

        except Exception as e:
            error_msg = str(e)
            tb = traceback.format_exc()
            logger.error(f"WabaRecord {waba_record_id} check failed: {error_msg}\n{tb}")

            analyze_error(
                waba_record_id=waba_record_id,
                error_type=type(e).__name__,
                error_message=error_msg,
                step_name="check_waba_status",
                traceback_str=tb,
            )

            if self.request.retries < self.max_retries:
                raise self.retry(exc=e)

            return {"status": "error", "message": error_msg}
