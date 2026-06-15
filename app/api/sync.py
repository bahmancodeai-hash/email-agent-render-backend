import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.email_account import EmailAccount

router = APIRouter()


@router.post("/trigger/{account_id}")
async def trigger_sync(
    account_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(EmailAccount).where(EmailAccount.id == account_id, EmailAccount.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if settings.task_queue_backend.lower() == "celery":
        from app.tasks.sync_tasks import sync_account
        task = sync_account.delay(str(account_id))
        return {"task_id": task.id, "message": "Sync started", "backend": "celery"}

    from app.tasks.sync_tasks import sync_account_now
    background_tasks.add_task(sync_account_now, str(account_id))
    return {"message": "Sync started", "backend": "inprocess"}


@router.post("/trigger-all")
async def trigger_sync_all(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(EmailAccount).where(EmailAccount.user_id == current_user.id, EmailAccount.is_active == True)
    )
    accounts = result.scalars().all()

    if settings.task_queue_backend.lower() == "celery":
        from app.tasks.sync_tasks import sync_account
        task_ids = []
        for account in accounts:
            task = sync_account.delay(str(account.id))
            task_ids.append(task.id)
        return {"task_ids": task_ids, "accounts_queued": len(accounts), "backend": "celery"}

    from app.tasks.sync_tasks import sync_account_now
    for account in accounts:
        background_tasks.add_task(sync_account_now, str(account.id))

    return {"accounts_queued": len(accounts), "backend": "inprocess"}
