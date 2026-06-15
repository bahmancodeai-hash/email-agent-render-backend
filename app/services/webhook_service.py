import hashlib
import hmac
import json
from datetime import datetime

import httpx

from app.models.webhook import Webhook, WebhookEvent
from app.services.network_guard import validate_webhook_url


async def fire_webhook(webhook: Webhook, event: WebhookEvent, payload: dict, db) -> bool:
    body = json.dumps({
        "event": event.value,
        "timestamp": datetime.utcnow().isoformat(),
        "data": payload,
    }, default=str)

    headers = {"Content-Type": "application/json", "X-Email-Agent-Event": event.value}
    if webhook.secret:
        sig = hmac.new(webhook.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-Email-Agent-Signature"] = f"sha256={sig}"

    try:
        await validate_webhook_url(webhook.url)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook.url, content=body, headers=headers)
        webhook.last_status_code = resp.status_code
        webhook.total_deliveries += 1
        webhook.last_triggered_at = datetime.utcnow()
        if not resp.is_success:
            webhook.failed_deliveries += 1
        return resp.is_success
    except Exception:
        webhook.failed_deliveries += 1
        webhook.total_deliveries += 1
        return False


async def fire_event(db, user_id, event: WebhookEvent, payload: dict):
    import uuid
    from sqlalchemy import select

    result = await db.execute(
        select(Webhook).where(
            Webhook.user_id == uuid.UUID(str(user_id)),
            Webhook.is_active == True,
        )
    )
    webhooks = result.scalars().all()
    for wh in webhooks:
        if event.value in wh.events:
            await fire_webhook(wh, event, payload, db)
