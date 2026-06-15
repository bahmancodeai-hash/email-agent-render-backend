from celery import Celery
from celery.schedules import crontab
from app.config import settings

if not settings.redis_url:
    raise RuntimeError("REDIS_URL is required when TASK_QUEUE_BACKEND=celery")

celery = Celery(
    "email_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.sync_tasks"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

celery.conf.beat_schedule = {
    "sync-all-accounts": {
        "task": "app.tasks.sync_tasks.sync_all_accounts",
        "schedule": crontab(minute=f"*/{settings.sync_interval_minutes}"),
    },
    "send-scheduled-emails": {
        "task": "app.tasks.sync_tasks.send_scheduled_emails",
        "schedule": crontab(minute="*"),  # every minute
    },
}
