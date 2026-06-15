from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_current_user
from app.models.user import User
from app.tasks.sync_tasks import _engine
from scripts.dedupe_messages import (
    DELETE_FINGERPRINT_DUPLICATES_SQL,
    DELETE_STABLE_DUPLICATES_SQL,
    FINGERPRINT_DUPLICATES_SQL,
    REFRESH_COUNTS_SQL,
    RESET_EMPTY_ACCOUNT_COUNTS_SQL,
    STABLE_DUPLICATES_SQL,
)

router = APIRouter()


class DedupeMessagesOut(BaseModel):
    applied: bool
    stable_duplicates: int | None = None
    fingerprint_duplicates: int | None = None
    stable_deleted: int = 0
    fingerprint_deleted: int = 0
    batches_used: int = 0
    truncated: bool = False


def _run_dedupe_messages(apply: bool, batch_size: int, max_batches: int) -> dict:
    result = {
        "applied": apply,
        "stable_duplicates": None,
        "fingerprint_duplicates": None,
        "stable_deleted": 0,
        "fingerprint_deleted": 0,
        "batches_used": 0,
        "truncated": False,
    }

    with _engine.begin() as conn:
        if not apply:
            result["stable_duplicates"] = int(conn.execute(text(STABLE_DUPLICATES_SQL)).scalar_one())
            result["fingerprint_duplicates"] = int(conn.execute(text(FINGERPRINT_DUPLICATES_SQL)).scalar_one())
            return result

        for _ in range(max_batches):
            deleted = int(
                conn.execute(
                    text(DELETE_STABLE_DUPLICATES_SQL),
                    {"batch_size": batch_size},
                ).scalar_one()
            )
            result["stable_deleted"] += deleted
            result["batches_used"] += 1
            if deleted == 0:
                break
        else:
            result["truncated"] = True

        for _ in range(max_batches):
            deleted = int(
                conn.execute(
                    text(DELETE_FINGERPRINT_DUPLICATES_SQL),
                    {"batch_size": batch_size},
                ).scalar_one()
            )
            result["fingerprint_deleted"] += deleted
            result["batches_used"] += 1
            if deleted == 0:
                break
        else:
            result["truncated"] = True

        conn.execute(text(REFRESH_COUNTS_SQL))
        conn.execute(text(RESET_EMPTY_ACCOUNT_COUNTS_SQL))
        return result


@router.post("/dedupe-messages", response_model=DedupeMessagesOut)
async def dedupe_messages(
    apply: bool = Query(False),
    batch_size: int = Query(250, ge=1, le=1000),
    max_batches: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    return await run_in_threadpool(_run_dedupe_messages, apply, batch_size, max_batches)
