from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.auth import get_current_user
from app.models.user import User
from app.tasks.sync_tasks import _engine
from app.services.dedupe_service import run_message_dedupe

router = APIRouter()


class DedupeMessagesOut(BaseModel):
    applied: bool
    stable_duplicates: int | None = None
    fingerprint_duplicates: int | None = None
    stable_deleted: int = 0
    fingerprint_deleted: int = 0
    truncated: bool = False


class MessageColumnOut(BaseModel):
    column_name: str
    data_type: str
    character_maximum_length: int | None = None


def _run_dedupe_messages(apply: bool, batch_size: int, max_batches: int) -> dict:
    with _engine.begin() as conn:
        from sqlalchemy.orm import Session

        db = Session(bind=conn)
        return run_message_dedupe(db, apply=apply, max_delete=batch_size * max_batches)


@router.post("/dedupe-messages", response_model=DedupeMessagesOut)
async def dedupe_messages(
    apply: bool = Query(False),
    batch_size: int = Query(250, ge=1, le=1000),
    max_batches: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    return await run_in_threadpool(_run_dedupe_messages, apply, batch_size, max_batches)


def _get_message_columns() -> list[dict]:
    from sqlalchemy import text

    with _engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT column_name, data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'messages'
            ORDER BY ordinal_position
        """)).mappings().all()
        return [dict(row) for row in rows]


@router.get("/message-columns", response_model=list[MessageColumnOut])
async def message_columns(current_user: User = Depends(get_current_user)):
    return await run_in_threadpool(_get_message_columns)
