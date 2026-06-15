import uuid
from typing import Optional
from sqlalchemy import select, or_

from app.database import AsyncSessionLocal
from app.models.email_account import EmailAccount
from app.models.message import Message, MessageStatus
from app.models.folder import Folder


# ── Read tools ────────────────────────────────────────────────────────────────

async def list_email_accounts(user_id: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        return [
            {
                "id": str(a.id),
                "email": a.email_address,
                "type": a.account_type.value,
                "status": a.status.value,
                "unread_count": a.unread_count,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            }
            for a in result.scalars().all()
        ]


async def search_emails(
    user_id: str,
    query: str,
    account_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account_ids = [row[0] for row in result.all()]

        q = select(Message).where(
            Message.account_id.in_(account_ids),
            Message.is_deleted == False,
            or_(
                Message.subject.ilike(f"%{query}%"),
                Message.from_address.ilike(f"%{query}%"),
                Message.preview.ilike(f"%{query}%"),
                Message.body_text.ilike(f"%{query}%"),
            ),
        )
        if account_id:
            q = q.where(Message.account_id == uuid.UUID(account_id))
        q = q.order_by(Message.received_at.desc()).limit(min(limit, 100))

        result = await db.execute(q)
        return [
            {
                "id": str(m.id),
                "account_id": str(m.account_id),
                "subject": m.subject,
                "from": m.from_address,
                "preview": m.preview,
                "received_at": m.received_at.isoformat() if m.received_at else None,
                "is_read": m.is_read,
                "is_flagged": m.is_flagged,
                "has_attachments": bool(m.attachments),
            }
            for m in result.scalars().all()
        ]


async def get_email(user_id: str, message_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account_ids = [row[0] for row in result.all()]

        result = await db.execute(
            select(Message).where(
                Message.id == uuid.UUID(message_id),
                Message.account_id.in_(account_ids),
            )
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return None
        return {
            "id": str(msg.id),
            "account_id": str(msg.account_id),
            "subject": msg.subject,
            "from_address": msg.from_address,
            "to_addresses": msg.to_addresses,
            "cc_addresses": msg.cc_addresses,
            "body_text": msg.body_text,
            "body_html": msg.body_html,
            "received_at": msg.received_at.isoformat() if msg.received_at else None,
            "is_read": msg.is_read,
            "is_flagged": msg.is_flagged,
            "attachments": msg.attachments,
            "thread_id": msg.thread_id,
        }


async def list_folders(user_id: str, account_id: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.id == uuid.UUID(account_id),
                EmailAccount.user_id == uuid.UUID(user_id),
            )
        )
        if not result.scalar_one_or_none():
            return []
        result = await db.execute(
            select(Folder).where(Folder.account_id == uuid.UUID(account_id))
        )
        return [
            {
                "id": str(f.id),
                "name": f.name,
                "type": f.folder_type,
                "unread_count": f.unread_count,
                "total_messages": f.total_messages,
            }
            for f in result.scalars().all()
        ]


async def get_attachments(user_id: str, message_id: str) -> list[dict]:
    msg = await get_email(user_id, message_id)
    if not msg:
        return []
    return msg.get("attachments") or []


# ── Write tools (scoped send/manage) ─────────────────────────────────────────

async def send_email_tool(
    user_id: str,
    account_id: str,
    to: list[str],
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.id == uuid.UUID(account_id),
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"error": "Account not found"}

        if dry_run:
            return {"dry_run": True, "would_send_to": to, "subject": subject}

        from app.services.email_sender import send_email
        await send_email(
            account=account,
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc or [],
        )
        await db.commit()
        return {"sent": True, "to": to, "subject": subject}


async def draft_email_tool(
    user_id: str,
    account_id: str,
    to: list[str],
    subject: str,
    body_text: Optional[str] = None,
) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.id == uuid.UUID(account_id),
                EmailAccount.user_id == uuid.UUID(user_id),
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"error": "Account not found"}

        msg = Message(
            account_id=account.id,
            subject=subject,
            from_address=account.email_address,
            to_addresses=[{"email": a} for a in to],
            body_text=body_text,
            is_draft=True,
            status=MessageStatus.DRAFT,
        )
        db.add(msg)
        await db.flush()
        await db.refresh(msg)
        await db.commit()
        return {"draft_id": str(msg.id), "subject": subject}


async def reply_to_email_tool(
    user_id: str,
    message_id: str,
    body_text: str,
    dry_run: bool = False,
) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account_ids = [row[0] for row in result.all()]

        result = await db.execute(
            select(Message).where(
                Message.id == uuid.UUID(message_id),
                Message.account_id.in_(account_ids),
            )
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return {"error": "Message not found"}

        result = await db.execute(
            select(EmailAccount).where(EmailAccount.id == msg.account_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"error": "Account not found"}

        if dry_run:
            return {"dry_run": True, "would_reply_to": msg.from_address}

        from app.services.email_sender import send_email
        await send_email(
            account=account,
            to=[msg.from_address],
            subject=f"Re: {msg.subject or ''}",
            body_text=body_text,
            in_reply_to=msg.message_id,
        )
        msg.status = MessageStatus.ANSWERED
        await db.commit()
        return {"replied": True, "to": msg.from_address}


async def archive_email_tool(user_id: str, message_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account_ids = [row[0] for row in result.all()]

        result = await db.execute(
            select(Message).where(
                Message.id == uuid.UUID(message_id),
                Message.account_id.in_(account_ids),
            )
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return {"error": "Message not found"}

        result = await db.execute(
            select(Folder).where(
                Folder.account_id == msg.account_id,
                Folder.folder_type == "archive",
            )
        )
        folder = result.scalar_one_or_none()
        if folder:
            msg.folder_id = folder.id
        msg.is_read = True
        await db.commit()
        return {"archived": True}


async def create_email_rule_tool(
    user_id: str,
    name: str,
    conditions: list,
    actions: list,
    account_id: Optional[str] = None,
    conditions_match: str = "all",
    stop_processing: bool = False,
) -> dict:
    from app.models.rule import EmailRule

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        if not result.scalars().first():
            return {"error": "No active accounts found for user"}

        rule = EmailRule(
            user_id=uuid.UUID(user_id),
            account_id=uuid.UUID(account_id) if account_id else None,
            name=name,
            conditions=conditions,
            conditions_match=conditions_match,
            actions=actions,
            is_active=True,
            stop_processing=stop_processing,
        )
        db.add(rule)
        await db.flush()
        await db.refresh(rule)
        await db.commit()
        return {"rule_id": str(rule.id), "name": rule.name, "created": True}


async def mark_email_read_tool(user_id: str, message_id: str, is_read: bool = True) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EmailAccount.id).where(
                EmailAccount.user_id == uuid.UUID(user_id),
                EmailAccount.is_active == True,
            )
        )
        account_ids = [row[0] for row in result.all()]

        result = await db.execute(
            select(Message).where(
                Message.id == uuid.UUID(message_id),
                Message.account_id.in_(account_ids),
            )
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return {"error": "Message not found"}

        msg.is_read = is_read
        msg.status = MessageStatus.READ if is_read else MessageStatus.UNREAD
        await db.commit()
        return {"updated": True, "is_read": is_read}
