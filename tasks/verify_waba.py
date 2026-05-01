"""
Flow A: WABA Creation + BM Verification task.

This Celery task handles the full verification flow for a single WABA record:
1. Opens AdsPower browser
2. Runs FacebookBot verification
3. Updates WabaRecord status
4. Creates ErrorReport on failure
"""

import logging
import os
import traceback
from datetime import datetime

import redis
from celery_app import celery

logger = logging.getLogger(__name__)


def _redis_client():
    return redis.from_url(os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"))


def _get_flask_app():
    """Create/get Flask app for database context."""
    from web_app import create_app
    return create_app()


@celery.task(bind=True, max_retries=3, default_retry_delay=60)
def create_and_verify(self, waba_record_id: int):
    """
    Run the full WABA creation + BM verification flow.
    """
    # Distributed lock: prevents two Celery workers from running the same WABA
    # simultaneously (e.g. a retry firing while the original is still in progress).
    # TTL=900s auto-releases if the worker crashes without releasing.
    r = _redis_client()
    lock = r.lock(f"waba_lock:{waba_record_id}", timeout=900, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        logger.warning(f"WabaRecord {waba_record_id} already locked — retrying in 30s")
        raise self.retry(countdown=30, exc=RuntimeError(f"WabaRecord {waba_record_id} already being processed"))

    try:
        return _run_verification(self, waba_record_id)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _run_verification(task, waba_record_id: int):
    app = _get_flask_app()
    with app.app_context():
        from web_app import db
        from web_app.models import WabaRecord, VerifyJob
        from services.status_manager import StatusManager
        from services.error_analyzer import analyze_error

        waba = db.session.get(WabaRecord, waba_record_id)
        if not waba:
            logger.error(f"WabaRecord {waba_record_id} not found")
            return {"status": "error", "message": "WabaRecord not found"}

        # Transition to executando
        if not StatusManager.transition(waba, "executando", reason="Tarefa Celery iniciada"):
            logger.warning(f"Cannot transition WabaRecord {waba_record_id} from {waba.status} to executando")
            return {"status": "skipped", "message": f"Invalid transition from {waba.status}"}

        try:
            # Import automation modules
            import config as app_config
            from services.adspower import AdsPowerClient
            from services.gerador_facade import GeradorService
            from services.sms_factory import get_sms_service

            ads = AdsPowerClient(app_config.ADSPOWER_BASE)
            gerador = GeradorService()
            sms = get_sms_service()

            # Get run data from Gerador
            if not waba.run_id:
                logger.error(f"WabaRecord {waba_record_id} has no run_id")
                raise ValueError("No run_id assigned to this WABA record")

            run_data = gerador.get_run(waba.run_id)
            from web_app.models import SystemSetting as _SS
            run_data["domain_verification_method"] = _SS.get("DOMAIN_VERIFICATION_METHOD", "meta_tag")

            # Open AdsPower browser
            profile_id = waba.profile_id
            browser_data = ads.open_browser(profile_id)
            ws_endpoint = browser_data.get("ws", {}).get("puppeteer", "")

            if not ws_endpoint:
                raise RuntimeError(f"Failed to get WebSocket endpoint for profile {profile_id}")

            try:
                # Run the verification
                from services.facebook_bot import FacebookBot

                bot = FacebookBot(
                    ws_endpoint=ws_endpoint,
                    run_data=run_data,
                    gerador=gerador,
                    sms=sms,
                    email_mode=run_data.get("email_mode", "own"),
                )

                # Parse existing step flags from WabaRecord
                result = bot.run_verification(
                    username=run_data.get("username", ""),
                    password=run_data.get("password", ""),
                    fakey=run_data.get("fakey", ""),
                    cookies=run_data.get("cookies", ""),
                    business_id=waba.business_id or "",
                )

                # Update WabaRecord with results
                if result.get("success"):
                    waba.verification_sent = True
                    waba.business_id = result.get("business_id", waba.business_id)
                    StatusManager.transition(waba, "em_revisao", reason="Verificação enviada com sucesso")
                    logger.info(f"WabaRecord {waba_record_id} verification successful")
                    return {"status": "success", "waba_record_id": waba_record_id}
                else:
                    raise RuntimeError(result.get("error", "Verification failed without details"))

            finally:
                # Always close the browser
                try:
                    ads.close_browser(profile_id)
                except Exception:
                    pass

        except Exception as e:
            from services.facebook_bot import BmRestrictedException, VerificationStepError, DomainVerificationError
            error_msg = str(e)
            tb = traceback.format_exc()
            logger.error(f"WabaRecord {waba_record_id} verification failed: {error_msg}\n{tb}")

            waba.last_error = error_msg

            # BM restricted — no point retrying; mark immediately.
            # Also catch the case where BmRestrictedException was wrapped as
            # VerificationStepError("unexpected", ...) by the generic handler.
            _bm_restricted = isinstance(e, BmRestrictedException) or (
                "portfólio bloqueado para anúncios" in error_msg
                or "business portfolio to advertise" in error_msg
            )
            if _bm_restricted:
                StatusManager.transition(waba, "restrita", reason=error_msg, force=True)
                logger.warning(f"WabaRecord {waba_record_id} marked as restrita")
                return {"status": "restrita", "message": error_msg}

            # Domain not verified — non-retryable; mark as erro immediately.
            _domain_failed = isinstance(e, DomainVerificationError) or (
                "[DOMAIN] Domain was not verified" in error_msg
            )
            if _domain_failed:
                StatusManager.transition(waba, "erro", reason=error_msg, force=True)
                logger.warning(f"WabaRecord {waba_record_id} domain not verified — marked as erro")
                return {"status": "error", "message": error_msg}

            waba.error_count += 1

            # Use screenshot from the specific step error when available
            step_screenshot = getattr(e, "screenshot_path", None) or waba.last_screenshot
            step_name = getattr(e, "step", "create_and_verify")

            # Create error report
            analyze_error(
                waba_record_id=waba_record_id,
                error_type=type(e).__name__,
                error_message=error_msg,
                screenshot_path=step_screenshot,
                step_name=step_name,
                traceback_str=tb,
            )

            # Retry or mark as error
            if task.request.retries < task.max_retries:
                StatusManager.transition(waba, "aguardando", reason=f"Tentativa {task.request.retries + 1} de {task.max_retries}", force=True)
                raise task.retry(exc=e)
            else:
                StatusManager.transition(waba, "erro", reason=f"Falha após {task.max_retries} tentativas: {error_msg}", force=True)
                return {"status": "error", "message": error_msg}
