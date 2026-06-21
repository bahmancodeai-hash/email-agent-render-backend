from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.api.auth import get_current_user
from app.models.email_account import AccountStatus, EmailAccount
from app.models.folder import Folder
from app.models.message import Message, MessageStatus
from app.models.user import User
from app.tasks.sync_tasks import _engine
from app.services.dedupe_service import run_message_dedupe

router = APIRouter()
MAX_MAILSPRING_BATCH = 50
MAX_MAILSPRING_BODY_CHARS = 800_000


class DedupeMessagesOut(BaseModel):
    applied: bool
    stable_duplicates: int | None = None
    fingerprint_duplicates: int | None = None
    stable_deleted: int = 0
    fingerprint_deleted: int = 0
    truncated: bool = False


class MessageColumnOut(BaseModel):
    table_schema: str
    column_name: str
    data_type: str
    character_maximum_length: int | None = None


class ReactivateAccountIn(BaseModel):
    email_address: str


class ReactivateAccountOut(BaseModel):
    id: str
    email_address: str
    is_active: bool
    status: str


class SyncAccountNowIn(BaseModel):
    email_address: str


class SyncAccountNowOut(BaseModel):
    id: str
    email_address: str
    result: dict


class MailspringAddress(BaseModel):
    email: str | None = None
    name: str | None = None

    model_config = ConfigDict(extra="ignore")


class MailspringAttachment(BaseModel):
    name: str | None = None
    size: int | None = None
    content_type: str | None = None

    model_config = ConfigDict(extra="ignore")


class MailspringMessageIn(BaseModel):
    folder_type: str = "custom"
    folder_name: str | None = None
    remote_folder_id: str | None = None
    uid: int | None = None
    remote_id: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    from_address: str | None = None
    from_name: str | None = None
    to_addresses: list[MailspringAddress] | None = None
    cc_addresses: list[MailspringAddress] | None = None
    bcc_addresses: list[MailspringAddress] | None = None
    reply_to: str | None = None
    in_reply_to: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    preview: str | None = None
    attachments: list[MailspringAttachment] | None = None
    is_read: bool = True
    is_flagged: bool = False
    is_draft: bool = False
    is_deleted: bool = False
    is_spam: bool = False
    sent_at: int | None = None
    received_at: int | None = None

    model_config = ConfigDict(extra="ignore")


class MailspringImportIn(BaseModel):
    email_address: str
    messages: list[MailspringMessageIn]


class MailspringImportOut(BaseModel):
    account_id: str
    imported: int
    updated: int
    skipped: int
    folders: int


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
            SELECT table_schema, column_name, data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'messages'
            ORDER BY table_schema, ordinal_position
        """)).mappings().all()
        return [dict(row) for row in rows]


def _clean(value: str | None, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _body(value: str | None) -> str | None:
    return _clean(value, MAX_MAILSPRING_BODY_CHARS)


def _dt_from_unix(value: int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _address_payload(items: list[MailspringAddress] | None) -> list[dict] | None:
    result: list[dict] = []
    for item in items or []:
        email = _clean(item.email, 500)
        if not email:
            continue
        result.append({"email": email, "name": _clean(item.name, 255)})
    return result or None


def _attachment_payload(items: list[MailspringAttachment] | None) -> list[dict] | None:
    result: list[dict] = []
    for item in items or []:
        name = _clean(item.name, 255)
        if not name:
            continue
        result.append({
            "name": name,
            "size": int(item.size or 0),
            "content_type": _clean(item.content_type, 255) or "application/octet-stream",
            "source": "mailspring",
        })
    return result or None


def _safe_folder_type(value: str | None) -> str:
    candidate = (value or "custom").strip().lower()
    return candidate if candidate in {"inbox", "sent", "drafts", "trash", "spam", "archive"} else "custom"


def _message_status(is_read: bool, is_draft: bool, is_deleted: bool) -> MessageStatus:
    if is_deleted:
        return MessageStatus.DELETED
    if is_draft:
        return MessageStatus.DRAFT
    return MessageStatus.READ if is_read else MessageStatus.UNREAD


async def _get_or_create_mailspring_folder(
    db: AsyncSession,
    account: EmailAccount,
    data: MailspringMessageIn,
) -> Folder:
    folder_type = _safe_folder_type(data.folder_type)
    remote_name = _clean(data.folder_name, 255) or folder_type
    result = await db.execute(
        select(Folder).where(
            Folder.account_id == account.id,
            Folder.remote_name == remote_name,
        )
    )
    folder = result.scalar_one_or_none()
    if folder:
        folder.folder_type = folder_type
        return folder

    display_name = remote_name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1] or folder_type.title()
    folder = Folder(
        account_id=account.id,
        name=display_name,
        remote_name=remote_name,
        folder_type=folder_type,
    )
    db.add(folder)
    await db.flush()
    return folder


async def _find_existing_import_message(
    db: AsyncSession,
    account_id,
    folder_id,
    data: MailspringMessageIn,
) -> Message | None:
    if data.uid is not None:
        result = await db.execute(
            select(Message).where(
                Message.account_id == account_id,
                Message.folder_id == folder_id,
                Message.uid == data.uid,
            )
        )
        if existing := result.scalar_one_or_none():
            return existing

    message_id = _clean(data.message_id, 512)
    if message_id:
        result = await db.execute(
            select(Message).where(
                Message.account_id == account_id,
                Message.folder_id == folder_id,
                Message.message_id == message_id,
            )
        )
        if existing := result.scalar_one_or_none():
            return existing

    remote_id = _clean(data.remote_id, 255)
    if remote_id:
        result = await db.execute(
            select(Message).where(
                Message.account_id == account_id,
                Message.folder_id == folder_id,
                Message.remote_id == remote_id,
            )
        )
        if existing := result.scalar_one_or_none():
            return existing

    timestamp = _dt_from_unix(data.received_at) or _dt_from_unix(data.sent_at)
    from_address = _clean(data.from_address, 500)
    if timestamp and from_address:
        result = await db.execute(
            select(Message).where(
                Message.account_id == account_id,
                Message.folder_id == folder_id,
                Message.subject == (_clean(data.subject) or ""),
                Message.from_address == from_address,
                or_(Message.received_at == timestamp, Message.sent_at == timestamp),
            )
        )
        return result.scalar_one_or_none()
    return None


def _apply_import_payload(message: Message, account: EmailAccount, folder: Folder, data: MailspringMessageIn) -> None:
    message.account_id = account.id
    message.folder_id = folder.id
    message.uid = data.uid
    message.remote_id = _clean(data.remote_id, 255) or message.remote_id
    message.message_id = _clean(data.message_id, 512) or message.message_id
    message.thread_id = _clean(data.thread_id, 255) or message.thread_id
    message.subject = _clean(data.subject) or ""
    message.from_address = _clean(data.from_address, 500) or account.email_address
    message.from_name = _clean(data.from_name, 255)
    message.to_addresses = _address_payload(data.to_addresses)
    message.cc_addresses = _address_payload(data.cc_addresses)
    message.bcc_addresses = _address_payload(data.bcc_addresses)
    message.reply_to = _clean(data.reply_to, 500)
    message.in_reply_to = _clean(data.in_reply_to, 512)
    if data.body_text:
        message.body_text = _body(data.body_text)
    if data.body_html:
        message.body_html = _body(data.body_html)
    message.preview = _clean(data.preview, 500)
    message.attachments = _attachment_payload(data.attachments)
    message.is_read = bool(data.is_read)
    message.is_flagged = bool(data.is_flagged)
    message.is_draft = bool(data.is_draft or folder.folder_type == "drafts")
    message.is_deleted = bool(data.is_deleted or folder.folder_type == "trash")
    message.is_spam = bool(data.is_spam or folder.folder_type == "spam")
    message.status = _message_status(message.is_read, message.is_draft, message.is_deleted)
    message.sent_at = _dt_from_unix(data.sent_at)
    message.received_at = _dt_from_unix(data.received_at) or message.sent_at
    message.updated_at = datetime.utcnow()


async def _refresh_import_counts(db: AsyncSession, account: EmailAccount) -> int:
    result = await db.execute(select(Folder).where(Folder.account_id == account.id))
    folders = list(result.scalars().all())
    for folder in folders:
        base = select(func.count()).select_from(Message).where(
            Message.account_id == account.id,
            Message.folder_id == folder.id,
        )
        if folder.folder_type != "trash":
            base = base.where(Message.is_deleted == False)
        if folder.folder_type != "drafts":
            base = base.where(Message.is_draft == False)
        folder.total_messages = await db.scalar(base) or 0
        folder.unread_count = await db.scalar(base.where(Message.is_read == False)) or 0

    account.total_messages = sum(folder.total_messages for folder in folders if folder.folder_type != "trash")
    inbox = next((folder for folder in folders if folder.folder_type == "inbox"), None)
    account.unread_count = inbox.unread_count if inbox else 0
    account.last_sync_at = datetime.utcnow()
    account.updated_at = datetime.utcnow()
    return len(folders)


@router.get("/message-columns", response_model=list[MessageColumnOut])
async def message_columns(current_user: User = Depends(get_current_user)):
    return await run_in_threadpool(_get_message_columns)


@router.post("/reactivate-account", response_model=ReactivateAccountOut)
async def reactivate_account(
    data: ReactivateAccountIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == current_user.id,
            EmailAccount.email_address == data.email_address.strip().lower(),
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Account not found")

    account.is_active = True
    account.status = AccountStatus.ACTIVE
    account.error_message = None
    await db.flush()
    return {
        "id": str(account.id),
        "email_address": account.email_address,
        "is_active": account.is_active,
        "status": account.status.value if hasattr(account.status, "value") else account.status,
    }


@router.post("/sync-account-now", response_model=SyncAccountNowOut)
async def sync_account_now_endpoint(
    data: SyncAccountNowIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == current_user.id,
            EmailAccount.email_address == data.email_address.strip().lower(),
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Account not found")

    from app.tasks.sync_tasks import sync_account_now

    sync_result = await run_in_threadpool(sync_account_now, str(account.id))
    return {
        "id": str(account.id),
        "email_address": account.email_address,
        "result": sync_result,
    }


@router.post("/import-mailspring", response_model=MailspringImportOut)
async def import_mailspring_messages(
    data: MailspringImportIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    email_address = data.email_address.strip().lower()
    if not email_address:
        raise HTTPException(status_code=400, detail="email_address is required")
    if not data.messages:
        raise HTTPException(status_code=400, detail="messages are required")
    if len(data.messages) > MAX_MAILSPRING_BATCH:
        raise HTTPException(status_code=400, detail=f"Import at most {MAX_MAILSPRING_BATCH} messages per batch")

    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == current_user.id,
            EmailAccount.email_address == email_address,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail=f"Account {email_address} is not connected in Email Agent")

    account.is_active = True
    account.status = AccountStatus.ACTIVE
    account.error_message = None

    imported = 0
    updated = 0
    skipped = 0
    for item in data.messages:
        if not item.remote_id and item.uid is None and not item.message_id:
            skipped += 1
            continue
        folder = await _get_or_create_mailspring_folder(db, account, item)
        existing = await _find_existing_import_message(db, account.id, folder.id, item)
        if existing:
            _apply_import_payload(existing, account, folder, item)
            updated += 1
        else:
            message = Message()
            _apply_import_payload(message, account, folder, item)
            message.created_at = datetime.utcnow()
            db.add(message)
            imported += 1

    folders = await _refresh_import_counts(db, account)
    await db.flush()
    return {
        "account_id": str(account.id),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "folders": folders,
    }
