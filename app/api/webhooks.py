import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.webhook import Webhook, WebhookEvent
from app.services.network_guard import validate_webhook_url

router = APIRouter()


class WebhookCreate(BaseModel):
    url: str
    events: list[str]
    secret: str | None = None


class WebhookOut(BaseModel):
    id: str
    url: str
    events: list
    is_active: bool
    total_deliveries: int
    failed_deliveries: int
    last_status_code: int | None

    class Config:
        from_attributes = True


@router.get("/", response_model=list[WebhookOut])
async def list_webhooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Webhook).where(Webhook.user_id == current_user.id)
    )
    return list(result.scalars().all())


@router.post("/", response_model=WebhookOut, status_code=201)
async def create_webhook(
    data: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data.url = await validate_webhook_url(data.url)
    valid_events = {e.value for e in WebhookEvent}
    for ev in data.events:
        if ev not in valid_events:
            raise HTTPException(status_code=400, detail=f"Unknown event: {ev}")
    wh = Webhook(
        user_id=current_user.id,
        url=data.url,
        events=data.events,
        secret=data.secret,
    )
    db.add(wh)
    await db.flush()
    await db.refresh(wh)
    return wh


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Webhook).where(Webhook.id == webhook_id, Webhook.user_id == current_user.id)
    )
    wh = result.scalar_one_or_none()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await db.delete(wh)
    return {"deleted": True}
