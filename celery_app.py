"""
Celery application instance.

Usage:
    # Start worker:
    celery -A celery_app worker --concurrency=5 --loglevel=info

    # Start beat (periodic tasks):
    celery -A celery_app beat --loglevel=info
"""

import os
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

celery = Celery(
    "verificador",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
)

celery.conf.update(
    # Suppress Celery 6.0 deprecation warning
    broker_connection_retry_on_startup=True,

    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Timezone
    timezone="America/Sao_Paulo",
    enable_utc=True,

    # Reliability: ack late so crashed tasks are retried
    task_acks_late=True,
    worker_prefetch_multiplier=1,

    # Results expire after 1 hour
    result_expires=3600,

    # Task routing
    task_routes={
        "tasks.verify_waba.*": {"queue": "verify"},
        "tasks.check_waba.*":  {"queue": "check"},
        "tasks.periodic.*":    {"queue": "periodic"},
        "tasks.cnpj_bank.*":   {"queue": "cnpj_bank"},
    },

    # Periodic tasks (Celery Beat)
    beat_schedule={
        "daily-waba-check": {
            "task": "tasks.periodic.daily_waba_check",
            "schedule": crontab(hour=8, minute=0),  # Every day at 8 AM
            "options": {"queue": "periodic"},
        },
        "review-timeout-check": {
            "task": "tasks.periodic.check_review_timeouts",
            "schedule": crontab(minute=0),  # Every hour
            "options": {"queue": "periodic"},
        },
        "cnpj-bank-refill": {
            "task": "tasks.cnpj_bank.refill_bank",
            "schedule": crontab(minute="*/10"),  # Every 10 minutes
            "options": {"queue": "cnpj_bank"},
        },
    },
)

# Auto-discover task modules
celery.autodiscover_tasks(["tasks"])
