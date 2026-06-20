import logging
import queue
import threading

from app.tasks.sync_tasks import sync_account_now


logger = logging.getLogger(__name__)

_queue: "queue.Queue[str | None]" = queue.Queue()
_pending: set[str] = set()
_lock = threading.Lock()
_worker_started = False


def _worker() -> None:
    while True:
        account_id = _queue.get()
        if account_id is None:
            _queue.task_done()
            continue
        try:
            sync_account_now(account_id)
        except Exception as exc:
            logger.warning("Queued account sync failed for %s: %s", account_id, exc)
        finally:
            with _lock:
                _pending.discard(account_id)
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker, name="email-agent-sync-queue", daemon=True)
        thread.start()
        _worker_started = True


def enqueue_account_sync(account_id: str) -> dict[str, int | bool | str]:
    _ensure_worker()
    with _lock:
        if account_id in _pending:
            return {"queued": False, "reason": "already-queued", "pending": len(_pending)}
        _pending.add(account_id)
        pending = len(_pending)
    _queue.put(account_id)
    return {"queued": True, "pending": pending}
