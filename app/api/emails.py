import asyncio
import uuid
import os
import re
from datetime import datetime, date, timedelta
from email.utils import parseaddr
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_, String, func
from pydantic import BaseModel
import httpx

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.email_account import EmailAccount, AccountType
from app.models.message import Message, MessageStatus
from app.models.folder import Folder

router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────────────

class MessageOut(BaseModel):
    id: str
    account_id: str
    subject: str | None
    from_address: str
    from_name: str | None
    preview: str | None
    is_read: bool
    is_flagged: bool
    received_at: datetime | None
    has_attachments: bool

    class Config:
        from_attributes = True


class MessageDetail(MessageOut):
    body_text: str | None
    body_html: str | None
    to_addresses: list | None
    cc_addresses: list | None
    attachments: list | None
    thread_id: str | None
    message_id: str | None
    in_reply_to: str | None


class SendRequest(BaseModel):
    account_id: str
    to: list[str]
    cc: list[str] = []
    bcc: list[str] = []
    subject: str
    body_text: str | None = None
    body_html: str | None = None
    scheduled_send_at: datetime | None = None


class ReplyRequest(BaseModel):
    body_text: str | None = None
    body_html: str | None = None
    reply_all: bool = False


class ForwardRequest(BaseModel):
    to: list[str]
    body_text: str | None = None
    body_html: str | None = None


class MoveRequest(BaseModel):
    folder_id: str


class SnoozeRequest(BaseModel):
    snooze_until: datetime


class AssistantDraftRequest(BaseModel):
    instruction: str | None = None


class AssistantDraftResponse(BaseModel):
    subject: str
    body_text: str
    context_summary: str
    related_count: int
    similar_count: int
    style_source_count: int
    safety_note: str


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _get_user_account_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(
        select(EmailAccount.id).where(
            EmailAccount.user_id == user_id,
            EmailAccount.is_active == True,
        )
    )
    return [row[0] for row in result.all()]


async def _get_account(db: AsyncSession, account_id: uuid.UUID, user_id: uuid.UUID) -> EmailAccount:
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.id == account_id,
            EmailAccount.user_id == user_id,
            EmailAccount.is_active == True,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


async def _get_message(db: AsyncSession, message_id: uuid.UUID, account_ids: list[uuid.UUID]) -> Message:
    result = await db.execute(
        select(Message).where(
            Message.id == message_id,
            Message.account_id.in_(account_ids),
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


async def _push_gmail_action(account: EmailAccount, msg: Message, action: str, **kwargs) -> None:
    if account.account_type != AccountType.GMAIL or not msg.remote_id:
        return

    from app.services import gmail as gmail_service

    def _run() -> None:
        if action == "read":
            gmail_service.mark_read(account.encrypted_credentials, msg.remote_id, kwargs.get("is_read", True))
        elif action == "flag":
            gmail_service.set_starred(account.encrypted_credentials, msg.remote_id, kwargs.get("is_flagged", True))
        elif action == "archive":
            gmail_service.archive_message(account.encrypted_credentials, msg.remote_id)
        elif action == "delete":
            gmail_service.trash_message(account.encrypted_credentials, msg.remote_id)

    await asyncio.to_thread(_run)


async def _refresh_counts(db: AsyncSession, account_id: uuid.UUID) -> None:
    account = await db.get(EmailAccount, account_id)
    if not account:
        return

    visible = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.account_id == account_id,
            Message.is_deleted == False,
            Message.is_draft == False,
        )
    )
    unread = visible.where(Message.is_read == False)
    account.total_messages = await db.scalar(visible) or 0
    account.unread_count = await db.scalar(unread) or 0

    result = await db.execute(select(Folder).where(Folder.account_id == account_id))
    for folder in result.scalars().all():
        folder_visible = (
            select(func.count())
            .select_from(Message)
            .where(
                Message.account_id == account_id,
                Message.folder_id == folder.id,
                Message.is_deleted == False,
                Message.is_draft == False,
            )
        )
        if folder.folder_type == "trash":
            folder_visible = (
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == account_id, Message.folder_id == folder.id)
            )
        folder.total_messages = await db.scalar(folder_visible) or 0
        folder.unread_count = await db.scalar(folder_visible.where(Message.is_read == False)) or 0


def _serialize(m: Message) -> dict:
    return {
        "id": str(m.id),
        "account_id": str(m.account_id),
        "subject": m.subject,
        "from_address": m.from_address,
        "from_name": m.from_name,
        "preview": m.preview,
        "is_read": m.is_read,
        "is_flagged": m.is_flagged,
        "received_at": m.received_at,
        "has_attachments": bool(m.attachments),
        "body_text": m.body_text,
        "body_html": m.body_html,
        "to_addresses": m.to_addresses,
        "cc_addresses": m.cc_addresses,
        "attachments": m.attachments,
        "thread_id": m.thread_id,
        "message_id": m.message_id,
        "in_reply_to": m.in_reply_to,
    }


def _list_dedupe_key(m: Message) -> tuple:
    remote_id = (getattr(m, "remote_id", None) or "").strip()
    if remote_id:
        return (str(m.account_id), "remote", remote_id)
    return (
        str(m.account_id),
        (m.subject or "").strip().lower(),
        (m.from_address or "").strip().lower(),
        m.received_at.isoformat() if m.received_at else "",
        (m.preview or "").strip().lower(),
    )


def _dedupe_message_list(messages: list[Message], limit: int, offset: int = 0) -> list[Message]:
    seen = set()
    result = []
    for message in messages:
        key = _list_dedupe_key(message)
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
        if len(result) >= offset + limit:
            break
    return result[offset:offset + limit]


def _email_only(value: str | None) -> str:
    if not value:
        return ""
    parsed = parseaddr(value)[1]
    return (parsed or value).strip().lower()


def _message_text(m: Message, limit: int = 900) -> str:
    text = (m.body_text or m.preview or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _keywords(value: str | None) -> list[str]:
    if not value:
        return []
    cleaned = re.sub(r"\b(re|fw|fwd)\s*:", " ", value, flags=re.I)
    words = re.findall(r"[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._-]{2,}", cleaned)
    stop = {
        "the", "and", "for", "with", "your", "you", "our", "this", "that",
        "from", "to", "on", "in", "of", "re", "fwd", "fw", "email",
        "message", "application", "student",
    }
    result: list[str] = []
    for word in words:
        low = word.lower()
        if low in stop or low in result:
            continue
        result.append(low)
        if len(result) >= 6:
            break
    return result


def _address_list_contains(column, email: str):
    return column.cast(String).ilike(f"%{email}%")


def _build_prompt(
    current: Message,
    instruction: str,
    related: list[Message],
    similar: list[Message],
    style_sources: list[Message],
) -> str:
    related_block = "\n".join(
        f"- {m.received_at or m.sent_at or m.created_at}: {m.subject or '(no subject)'} | {_message_text(m, 500)}"
        for m in related[:8]
    ) or "No previous messages found."
    similar_block = "\n".join(
        f"- {m.subject or '(no subject)'} | From: {m.from_address} | {_message_text(m, 500)}"
        for m in similar[:8]
    ) or "No similar messages found."
    style_block = "\n\n---\n\n".join(_message_text(m, 1200) for m in style_sources[:3]) or "No sent examples found."

    return f"""
Current incoming email:
From: {current.from_name or ''} <{current.from_address}>
Subject: {current.subject or '(no subject)'}
Preview/body: {_message_text(current, 1600)}

User instruction:
{instruction or 'Prepare a concise helpful reply.'}

Previous emails from the same student / same thread:
{related_block}

Similar emails from other students:
{similar_block}

Previous sent style examples:
{style_block}

Write only a ready-to-review email reply draft. Do not include analysis. Do not claim anything that is not supported by the context. If important data is missing, ask for it politely inside the draft.
""".strip()


async def _generate_llm_draft(prompt: str) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "input": [
                        {
                            "role": "system",
                            "content": (
                                "You draft email replies for an admissions/support email agent. "
                                "Never send emails. Produce only a clear, professional draft."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.35,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    text = "\n".join(chunks).strip()
    return text or None


def _fallback_draft(current: Message, instruction: str, related: list[Message], similar: list[Message]) -> str:
    name = (current.from_name or "").strip()
    first_name = name.split()[0] if name else ""
    greeting = f"Dear {first_name}," if first_name else "Hello,"
    subject = current.subject or "your email"
    context_line = "I reviewed your message"
    if related:
        context_line += " together with our previous correspondence"
    if similar:
        context_line += " and similar student cases"

    instruction_line = instruction.strip()
    if instruction_line:
        if re.search(r"[А-Яа-я]", instruction_line):
            low = instruction_line.lower()
            sentences: list[str] = []
            if any(word in low for word in ["провер", "посмотр", "review", "check"]):
                sentences.append("We will review the details carefully.")
            if any(word in low for word in ["следующ", "дальш", "next", "шаг"]):
                sentences.append("After that, we will share the next steps with you.")
            if any(word in low for word in ["уточ", "не хватает", "missing", "документ"]):
                sentences.append("If any additional information or documents are required, we will let you know.")
            if any(word in low for word in ["корот", "кратк", "short"]):
                sentences = sentences[:2] or ["We will check this and get back to you shortly."]
            if not sentences:
                sentences.append("Thank you for the details. We will check this and get back to you shortly.")
            action = "\n\n" + " ".join(sentences)
        else:
            action = f"\n\n{instruction_line}"
    else:
        action = (
            "\n\nThank you for your email. I will review the details carefully and get back to you "
            "with the correct next steps."
        )

    return (
        f"{greeting}\n\n"
        f"{context_line} about \"{subject}\".{action}\n\n"
        "Please note that I will confirm the final details before any action is taken.\n\n"
        "Best regards,"
    )


def _context_summary(current: Message, related: list[Message], similar: list[Message], style_sources: list[Message]) -> str:
    sender = current.from_name or _email_only(current.from_address) or current.from_address
    return (
        f"Контекст: {sender}; найдено прошлых писем/цепочек: {len(related)}, "
        f"похожих писем других студентов: {len(similar)}, примеров исходящих ответов: {len(style_sources)}."
    )


# ── Smart folders ────────────────────────────────────────────────────────────

@router.get("/smart/{folder_name}", response_model=list[MessageOut])
async def smart_folder(
    folder_name: str,
    account_id: uuid.UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    if not account_ids:
        return []

    base = select(Message).where(
        Message.account_id.in_(account_ids),
        Message.is_deleted == False,
    )
    if account_id:
        base = base.where(Message.account_id == account_id)

    today_start = datetime.combine(date.today(), datetime.min.time())

    if folder_name == "unread":
        q = base.where(Message.is_read == False)
    elif folder_name == "inbox":
        q = base.where(Message.is_draft == False)
    elif folder_name == "today":
        q = base.where(Message.received_at >= today_start)
    elif folder_name == "flagged":
        q = base.where(Message.is_flagged == True)
    elif folder_name == "attachments":
        q = base.where(Message.attachments != None)
    elif folder_name == "needs-reply":
        q = base.where(
            Message.is_read == True,
            Message.is_flagged == True,
            Message.status != MessageStatus.ANSWERED,
        )
    elif folder_name == "snoozed":
        q = base.where(
            Message.snooze_until != None,
            Message.snooze_until > datetime.utcnow(),
        )
    elif folder_name == "scheduled":
        q = base.where(
            Message.scheduled_send_at != None,
            Message.is_draft == True,
        )
    else:
        raise HTTPException(status_code=404, detail=f"Unknown smart folder: {folder_name}")

    query_limit = offset + (limit * 3)
    q = q.order_by(Message.received_at.desc()).limit(query_limit)
    result = await db.execute(q)
    messages = result.scalars().all()
    messages = _dedupe_message_list(messages, limit, offset)
    return [_serialize(m) for m in messages]


# ── List / Get ───────────────────────────────────────────────────────────────

@router.get("/", response_model=list[MessageOut])
async def list_emails(
    account_id: uuid.UUID | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    folder_type: str | None = Query(None),
    is_unread: bool | None = Query(None),
    is_flagged: bool | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    if not account_ids:
        return []

    q = select(Message).where(Message.account_id.in_(account_ids))
    if account_id:
        q = q.where(Message.account_id == account_id)
    if folder_id:
        q = q.where(Message.folder_id == folder_id)
    if folder_type:
        q = q.outerjoin(Folder, Message.folder_id == Folder.id)
        if folder_type == "trash":
            q = q.where(or_(Message.is_deleted == True, Folder.folder_type == "trash"))
        elif folder_type == "drafts":
            q = q.where(
                Message.is_deleted == False,
                or_(Message.is_draft == True, Folder.folder_type == "drafts"),
            )
        elif folder_type == "spam":
            q = q.where(
                Message.is_deleted == False,
                Message.is_draft == False,
                or_(Message.is_spam == True, Folder.folder_type == "spam"),
            )
        else:
            q = q.where(
                Message.is_deleted == False,
                Message.is_draft == False,
                Folder.folder_type == folder_type,
            )
    else:
        q = q.where(
            Message.is_deleted == False,
            Message.is_draft == False,
        )
    if is_unread is not None:
        q = q.where(Message.is_read == (not is_unread))
    if is_flagged is not None:
        q = q.where(Message.is_flagged == is_flagged)
    if search:
        q = q.where(
            or_(
                Message.subject.ilike(f"%{search}%"),
                Message.from_address.ilike(f"%{search}%"),
                Message.preview.ilike(f"%{search}%"),
                Message.body_text.ilike(f"%{search}%"),
            )
        )

    query_limit = offset + (limit * 3)
    q = q.order_by(Message.received_at.desc()).limit(query_limit)
    result = await db.execute(q)
    messages = _dedupe_message_list(result.scalars().all(), limit, offset)
    return [_serialize(m) for m in messages]


@router.get("/{message_id}", response_model=MessageDetail)
async def get_email(
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    if not msg.is_read:
        account = await _get_account(db, msg.account_id, current_user.id)
        await _push_gmail_action(account, msg, "read", is_read=True)
        msg.is_read = True
        msg.status = MessageStatus.READ
        await _refresh_counts(db, msg.account_id)
    return _serialize(msg)


@router.post("/{message_id}/assistant/draft", response_model=AssistantDraftResponse)
async def assistant_reply_draft(
    message_id: uuid.UUID,
    data: AssistantDraftRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    current = await _get_message(db, message_id, account_ids)
    student_email = _email_only(current.from_address)
    search_terms = _keywords(current.subject or current.preview)

    related_conditions = []
    if student_email:
        related_conditions.extend(
            [
                Message.from_address.ilike(f"%{student_email}%"),
                _address_list_contains(Message.to_addresses, student_email),
                _address_list_contains(Message.cc_addresses, student_email),
            ]
        )
    if current.thread_id:
        related_conditions.append(Message.thread_id == current.thread_id)
    if current.message_id:
        related_conditions.append(Message.in_reply_to == current.message_id)

    related: list[Message] = []
    if related_conditions:
        related_result = await db.execute(
            select(Message)
            .where(
                Message.account_id.in_(account_ids),
                Message.id != current.id,
                Message.is_deleted == False,
                or_(*related_conditions),
            )
            .order_by(Message.received_at.desc().nullslast(), Message.sent_at.desc().nullslast())
            .limit(10)
        )
        related = list(related_result.scalars().all())

    similar_conditions = []
    for term in search_terms[:4]:
        like = f"%{term}%"
        similar_conditions.extend(
            [
                Message.subject.ilike(like),
                Message.preview.ilike(like),
                Message.body_text.ilike(like),
            ]
        )

    similar: list[Message] = []
    if similar_conditions:
        similar_result = await db.execute(
            select(Message)
            .where(
                Message.account_id.in_(account_ids),
                Message.id != current.id,
                Message.is_deleted == False,
                or_(*similar_conditions),
            )
            .order_by(Message.received_at.desc().nullslast(), Message.sent_at.desc().nullslast())
            .limit(12)
        )
        similar = list(similar_result.scalars().all())

    style_query = (
        select(Message)
        .outerjoin(Folder, Message.folder_id == Folder.id)
        .where(
            Message.account_id.in_(account_ids),
            Message.id != current.id,
            Message.is_deleted == False,
            Message.body_text != None,
            or_(Folder.folder_type == "sent", Message.sent_at != None),
        )
        .order_by(Message.sent_at.desc().nullslast(), Message.received_at.desc().nullslast())
        .limit(6)
    )
    style_result = await db.execute(style_query)
    style_sources = list(style_result.scalars().all())

    instruction = (data.instruction or "").strip()
    prompt = _build_prompt(current, instruction, related, similar, style_sources)
    draft = await _generate_llm_draft(prompt)
    if not draft:
        draft = _fallback_draft(current, instruction, related, similar)

    subject = current.subject or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    return AssistantDraftResponse(
        subject=subject or "Re:",
        body_text=draft,
        context_summary=_context_summary(current, related, similar, style_sources),
        related_count=len(related),
        similar_count=len(similar),
        style_source_count=len(style_sources),
        safety_note="Черновик подготовлен только для проверки. Ничего не отправлено автоматически.",
    )


# ── Send ─────────────────────────────────────────────────────────────────────

@router.post("/send", status_code=201)
async def send_email(
    data: SendRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account(db, uuid.UUID(data.account_id), current_user.id)

    if data.scheduled_send_at and data.scheduled_send_at > datetime.utcnow():
        msg = Message(
            account_id=account.id,
            subject=data.subject,
            from_address=account.email_address,
            to_addresses=[{"email": a} for a in data.to],
            cc_addresses=[{"email": a} for a in data.cc],
            bcc_addresses=[{"email": a} for a in data.bcc],
            body_text=data.body_text,
            body_html=data.body_html,
            is_draft=True,
            scheduled_send_at=data.scheduled_send_at,
            status=MessageStatus.DRAFT,
        )
        db.add(msg)
        await db.flush()
        return {"scheduled": True, "message_id": str(msg.id)}

    from app.services.email_sender import send_email as _send
    await _send(
        account=account,
        to=data.to,
        subject=data.subject,
        body_text=data.body_text,
        body_html=data.body_html,
        cc=data.cc or [],
        bcc=data.bcc or [],
    )
    return {"sent": True}


# ── Reply ─────────────────────────────────────────────────────────────────────

@router.post("/{message_id}/reply", status_code=201)
async def reply_email(
    message_id: uuid.UUID,
    data: ReplyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)

    to = [msg.from_address]
    if data.reply_all and msg.to_addresses:
        to += [a.get("email", "") for a in msg.to_addresses if a.get("email") != account.email_address]

    subject = msg.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    from app.services.email_sender import send_email as _send
    await _send(
        account=account,
        to=to,
        subject=subject,
        body_text=data.body_text,
        body_html=data.body_html,
        in_reply_to=msg.message_id,
    )

    msg.status = MessageStatus.ANSWERED
    return {"sent": True}


# ── Forward ───────────────────────────────────────────────────────────────────

@router.post("/{message_id}/forward", status_code=201)
async def forward_email(
    message_id: uuid.UUID,
    data: ForwardRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)

    subject = msg.subject or ""
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"

    fwd_prefix_text = (
        f"\n\n---------- Forwarded message ----------\n"
        f"From: {msg.from_address}\n"
        f"Subject: {msg.subject}\n\n"
    )
    fwd_prefix_html = (
        f"<br><br><hr><b>---------- Forwarded message ----------</b><br>"
        f"<b>From:</b> {msg.from_address}<br>"
        f"<b>Subject:</b> {msg.subject}<br><br>"
    )

    body_text = (data.body_text or "") + fwd_prefix_text + (msg.body_text or "")
    body_html = None
    if msg.body_html:
        body_html = (data.body_html or data.body_text or "") + fwd_prefix_html + msg.body_html

    from app.services.email_sender import send_email as _send
    await _send(
        account=account,
        to=data.to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )
    return {"sent": True}


# ── Actions ───────────────────────────────────────────────────────────────────

@router.patch("/{message_id}/read")
async def mark_read(
    message_id: uuid.UUID,
    is_read: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)
    await _push_gmail_action(account, msg, "read", is_read=is_read)
    msg.is_read = is_read
    msg.status = MessageStatus.READ if is_read else MessageStatus.UNREAD
    await _refresh_counts(db, msg.account_id)
    return {"updated": True}


@router.patch("/{message_id}/flag")
async def toggle_flag(
    message_id: uuid.UUID,
    is_flagged: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)
    await _push_gmail_action(account, msg, "flag", is_flagged=is_flagged)
    msg.is_flagged = is_flagged
    await _refresh_counts(db, msg.account_id)
    return {"updated": True}


@router.patch("/{message_id}/archive")
async def archive_email(
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)
    await _push_gmail_action(account, msg, "archive")

    result = await db.execute(
        select(Folder).where(
            Folder.account_id == msg.account_id,
            Folder.folder_type == "archive",
        )
    )
    archive_folder = result.scalar_one_or_none()
    if archive_folder:
        msg.folder_id = archive_folder.id
    msg.is_read = True
    await _refresh_counts(db, msg.account_id)
    return {"archived": True}


@router.patch("/{message_id}/move")
async def move_email(
    message_id: uuid.UUID,
    data: MoveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    folder_uuid = uuid.UUID(data.folder_id)
    result = await db.execute(
        select(Folder).where(
            Folder.id == folder_uuid,
            Folder.account_id == msg.account_id,
        )
    )
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    msg.folder_id = folder.id
    return {"moved": True}


@router.patch("/{message_id}/snooze")
async def snooze_email(
    message_id: uuid.UUID,
    data: SnoozeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    msg.snooze_until = data.snooze_until
    return {"snoozed_until": data.snooze_until}


@router.delete("/{message_id}")
async def delete_email(
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    msg = await _get_message(db, message_id, account_ids)
    account = await _get_account(db, msg.account_id, current_user.id)
    await _push_gmail_action(account, msg, "delete")
    msg.is_deleted = True
    msg.status = MessageStatus.DELETED
    await _refresh_counts(db, msg.account_id)
    return {"deleted": True}


@router.get("/thread/{thread_id}", response_model=list[MessageDetail])
async def get_thread(
    thread_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account_ids = await _get_user_account_ids(db, current_user.id)
    result = await db.execute(
        select(Message).where(
            Message.account_id.in_(account_ids),
            Message.thread_id == thread_id,
            Message.is_deleted == False,
        ).order_by(Message.received_at.asc())
    )
    return [_serialize(m) for m in result.scalars().all()]
