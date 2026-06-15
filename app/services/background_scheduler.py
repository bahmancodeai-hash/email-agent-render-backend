import asyncio
import logging

from app.config import settings
from app.tasks.sync_tasks import run_periodic_jobs_once


logger = logging.getLogger(__name__)


class BackgroundScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        if not settings.background_jobs_enabled:
            return
        if settings.task_queue_backend.lower() == "celery":
            return
        if self._task and not self._task.done():
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="email-agent-background-scheduler")

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop_event = None

    async def _run(self) -> None:
        await asyncio.sleep(settings.scheduler_startup_delay_seconds)
        interval = max(settings.sync_interval_minutes, 1) * 60

        while self._stop_event and not self._stop_event.is_set():
            try:
                result = await asyncio.to_thread(run_periodic_jobs_once)
                logger.info("Background jobs finished: %s", result)
            except Exception:
                logger.exception("Background jobs failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


background_scheduler = BackgroundScheduler()
