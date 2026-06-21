import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


logger = logging.getLogger(__name__)

_sync_url = settings.database_url_sync or settings.database_url.replace("+asyncpg", "")
_engine = create_engine(_sync_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
_SessionLocal = sessionmaker(bind=_engine)
SYNC_FOLDER_TYPES = {"inbox", "sent", "drafts", "trash", "spam", "archive"}
GMAIL_SYNC_FOLDER_TYPES = SYNC_FOLDER_TYPES - {"archive"}
GMAIL_FETCH_LIMIT = 250
IMAP_FETCH_LIMIT = 250
OUTLOOK_FETCH_LIMIT = 250
GMAIL_RECONCILE_LIMIT = 1000
GMAIL_STALE_STATE_LIMIT = 200

_FOLDER_PRIORITY = {
    "inbox": ["inbox"],
    "sent": ["sent", "sent mail", "sent messages", "sent items"],
    "drafts": ["drafts", "draft"],
    "trash": ["trash", "deleted messages", "deleted items", "bin"],
    "spam": ["spam", "junk", "bulk mail"],
    "archive": ["archive", "all mail"],
}


def _imap_skip_entries() -> set[tuple[str, str, int]]:
    entries: set[tuple[str, str, int]] = set()
    raw = settings.imap_skip_uids or ""
    for item in raw.split(","):
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 3:
            continue
        email, folder, uid = parts
        try:
            entries.add((email.lower(), folder.lower(), int(uid)))
        except ValueError:
            continue
    return entries


def _should_skip_imap_message(skip_entries: set[tuple[str, str, int]], account, folder, message: dict[str, Any]) -> bool:
    uid = message.get("uid")
    if uid is None:
        return False
    try:
        uid_value = int(uid)
    except (TypeError, ValueError):
        return False
    return (
        account.email_address.lower(),
        (folder.remote_name or folder.name or "").lower(),
        uid_value,
    ) in skip_entries


def _strict_clamp_accounts() -> set[str]:
    return {
        item.strip().lower()
        for item in (settings.imap_strict_clamp_accounts or "").split(",")
        if item.strip()
    }


def _clamp_address_list(value: Any, limit: int = 200) -> Any:
    if not isinstance(value, list):
        return value
    clamped = []
    for item in value:
        if not isinstance(item, dict):
            clamped.append(item)
            continue
        clamped.append({
            key: (_truncate_text(val, limit) if isinstance(val, str) else val)
            for key, val in item.items()
        })
    return clamped


def _clamp_attachments(value: Any, limit: int = 200) -> Any:
    if not isinstance(value, list):
        return value
    clamped = []
    for item in value:
        if not isinstance(item, dict):
            clamped.append(item)
            continue
        clamped.append({
            key: (_truncate_text(val, limit) if isinstance(val, str) else val)
            for key, val in item.items()
        })
    return clamped


def _strict_clamp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clamped = dict(payload)
    for field in (
        "remote_id",
        "message_id",
        "thread_id",
        "subject",
        "from_address",
        "from_name",
        "reply_to",
        "in_reply_to",
        "preview",
    ):
        clamped[field] = _truncate_text(clamped.get(field), 200)
    clamped["from_address"] = clamped.get("from_address") or ""
    clamped["to_addresses"] = _clamp_address_list(clamped.get("to_addresses"))
    clamped["cc_addresses"] = _clamp_address_list(clamped.get("cc_addresses"))
    clamped["bcc_addresses"] = _clamp_address_list(clamped.get("bcc_addresses"))
    clamped["attachments"] = _clamp_attachments(clamped.get("attachments"))
    return clamped

_GMAIL_FOLDER_PRIORITY = {
    "sent": ["[gmail]/sent mail", "sent mail", "sent"],
    "drafts": ["[gmail]/drafts", "drafts"],
    "trash": ["[gmail]/trash", "trash", "bin"],
    "spam": ["[gmail]/spam", "spam", "junk"],
    "archive": ["[gmail]/all mail", "all mail", "archive"],
}


def _get_db() -> Session:
    return _SessionLocal()


def _normalize_message_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"none", "null"}:
        return None
    return normalized


def _truncate_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _normalized_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = dict(message)
    payload["message_id"] = _normalize_message_id(payload.get("message_id"))
    payload["remote_id"] = _normalize_message_id(payload.get("remote_id"))
    payload["in_reply_to"] = _normalize_message_id(payload.get("in_reply_to"))
    if not payload.get("thread_id"):
        payload["thread_id"] = payload.get("in_reply_to") or payload.get("message_id")

    payload["remote_id"] = _truncate_text(payload.get("remote_id"), 255)
    payload["message_id"] = _truncate_text(payload.get("message_id"), 512)
    payload["thread_id"] = _truncate_text(payload.get("thread_id"), 255)
    payload["from_address"] = _truncate_text(payload.get("from_address"), 500) or ""
    payload["from_name"] = _truncate_text(payload.get("from_name"), 255)
    payload["reply_to"] = _truncate_text(payload.get("reply_to"), 500)
    payload["in_reply_to"] = _truncate_text(payload.get("in_reply_to"), 512)
    payload["preview"] = _truncate_text(payload.get("preview"), 500)
    return payload


def _find_existing_message(db: Session, Message, account_id, folder_id, message: dict[str, Any]):
    remote_id = _normalize_message_id(message.get("remote_id"))
    if remote_id:
        existing = db.query(Message).filter(
            Message.account_id == account_id,
            Message.folder_id == folder_id,
            Message.remote_id == remote_id,
        ).first()
        if existing:
            return existing

    message_id = _normalize_message_id(message.get("message_id"))
    if message_id:
        existing = db.query(Message).filter(
            Message.account_id == account_id,
            Message.folder_id == folder_id,
            Message.message_id == message_id,
        ).first()
        if existing:
            return existing

    uid = message.get("uid")
    if uid is not None:
        existing = db.query(Message).filter(
            Message.account_id == account_id,
            Message.folder_id == folder_id,
            Message.uid == uid,
        ).first()
        if existing:
            return existing

    timestamp = message.get("received_at") or message.get("sent_at")
    from_address = message.get("from_address")
    if timestamp and from_address:
        return db.query(Message).filter(
            Message.account_id == account_id,
            Message.folder_id == folder_id,
            Message.subject == message.get("subject"),
            Message.from_address == from_address,
            Message.received_at == message.get("received_at"),
            Message.sent_at == message.get("sent_at"),
            Message.preview == message.get("preview"),
        ).first()
    return None


def _merge_existing_message(existing, message: dict[str, Any], MessageStatus) -> None:
    remote_id = _normalize_message_id(message.get("remote_id"))
    if remote_id and not getattr(existing, "remote_id", None):
        existing.remote_id = remote_id

    message_id = _normalize_message_id(message.get("message_id"))
    if message_id and not existing.message_id:
        existing.message_id = message_id
    if message.get("uid") is not None and existing.uid is None:
        existing.uid = message["uid"]
    if message.get("thread_id") and not existing.thread_id:
        existing.thread_id = message["thread_id"]

    for field in ("is_read", "is_flagged", "is_draft", "is_deleted", "is_spam"):
        if field in message:
            setattr(existing, field, bool(message[field]))

    if "is_read" in message:
        existing.status = MessageStatus.READ if message["is_read"] else MessageStatus.UNREAD


def _folder_priority(account, folder) -> tuple[int, int, str]:
    remote = (folder.remote_name or folder.name or "").replace("\\", "/").lower()
    base = remote.rsplit("/", 1)[-1]
    folder_type = folder.folder_type
    preferred = _FOLDER_PRIORITY.get(folder_type, [])
    if "gmail" in (account.imap_host or "").lower():
        preferred = _GMAIL_FOLDER_PRIORITY.get(folder_type, preferred)

    for index, name in enumerate(preferred):
        if remote == name:
            return (index, len(remote), remote)
        if base == name:
            return (index + 20, len(remote), remote)
    if folder_type in remote:
        return (200, len(remote), remote)
    return (500, len(remote), remote)


def _select_sync_folders(account, folders: list) -> list:
    selected = {}
    for folder in folders:
        current = selected.get(folder.folder_type)
        if current is None or _folder_priority(account, folder) < _folder_priority(account, current):
            selected[folder.folder_type] = folder
    return sorted(selected.values(), key=lambda f: f.folder_type)


def sync_account_now(account_id: str, *, raise_on_error: bool = False) -> dict[str, Any]:
    from app.models.email_account import EmailAccount, AccountType, AccountStatus

    db = _get_db()
    try:
        account = db.get(EmailAccount, uuid.UUID(account_id))
        if not account or not account.is_active:
            return {"skipped": True, "account_id": account_id}

        acquired = db.execute(
            text("select pg_try_advisory_xact_lock(hashtext(:lock_name))"),
            {"lock_name": f"email_agent_sync:{account_id}"},
        ).scalar()
        if not acquired:
            return {"skipped": True, "account_id": account_id, "reason": "sync-in-progress"}

        if account.account_type == AccountType.IMAP:
            _sync_imap(db, account)
        elif account.account_type == AccountType.GMAIL:
            _sync_gmail(db, account)
        elif account.account_type == AccountType.OUTLOOK:
            _sync_outlook(db, account)

        _refresh_account_counts(db, account)
        account.last_sync_at = datetime.utcnow()
        account.status = AccountStatus.ACTIVE
        account.error_message = None
        db.commit()
        return {"synced": True, "account_id": account_id}
    except Exception as exc:
        db.rollback()
        _mark_sync_error(db, EmailAccount, AccountStatus, account_id, exc)
        if _is_auth_error(exc):
            return {"synced": False, "account_id": account_id, "auth_error": True}
        if raise_on_error:
            raise
        logger.warning("Account sync failed for %s: %s", account_id, exc)
        return {"synced": False, "account_id": account_id, "error": str(exc)[:500]}
    finally:
        db.close()


def _mark_sync_error(db: Session, EmailAccount, AccountStatus, account_id: str, exc: Exception) -> None:
    try:
        account = db.get(EmailAccount, uuid.UUID(account_id))
        if account:
            account.status = AccountStatus.ERROR
            account.error_message = str(exc)[:500]
            db.commit()
    except Exception:
        db.rollback()


def _is_auth_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    auth_markers = (
        "auth",
        "invalid credential",
        "invalid credentials",
        "authenticationfailed",
        "login failed",
        "invalid_grant",
        "token expired",
        "unauthorized",
        "401",
    )
    return "loginerror" in name or any(marker in message for marker in auth_markers)


def _refresh_account_counts(db: Session, account) -> None:
    from app.models.folder import Folder
    from app.models.message import Message
    from app.models.email_account import AccountType

    visible_messages = db.query(Message).filter(
        Message.account_id == account.id,
        Message.is_deleted == False,
        Message.is_draft == False,
    )
    account.total_messages = visible_messages.count()

    for folder in db.query(Folder).filter(Folder.account_id == account.id).all():
        q = db.query(Message).filter(
            Message.account_id == account.id,
            Message.folder_id == folder.id,
            Message.is_deleted == False,
            Message.is_draft == False,
        )
        if folder.folder_type == "trash":
            q = db.query(Message).filter(
                Message.account_id == account.id,
                Message.folder_id == folder.id,
            )
        folder.total_messages = q.count()
        folder.unread_count = q.filter(Message.is_read == False).count()

    if account.account_type == AccountType.GMAIL:
        _refresh_gmail_remote_counts(db, account, Folder, Message)
    elif account.account_type == AccountType.IMAP:
        _refresh_imap_remote_counts(db, account, Folder)
    elif account.account_type == AccountType.OUTLOOK:
        _refresh_outlook_remote_counts(db, account, Folder)

    inbox = db.query(Folder).filter(
        Folder.account_id == account.id,
        Folder.folder_type == "inbox",
    ).first()
    account.unread_count = inbox.unread_count if inbox else visible_messages.filter(Message.is_read == False).count()


def _refresh_imap_remote_counts(db: Session, account, Folder) -> None:
    from app.services import imap as imap_service

    loop = asyncio.new_event_loop()
    try:
        folders = loop.run_until_complete(
            imap_service.fetch_folders(
                account.encrypted_credentials,
                account.imap_host,
                account.imap_port,
                account.imap_ssl,
            )
        )
    except Exception as exc:
        logger.debug("IMAP folder stats refresh failed for %s: %s", account.id, exc)
        return
    finally:
        loop.close()

    for remote_folder in folders:
        folder = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.remote_name == remote_folder["remote_name"],
        ).first()
        if not folder:
            continue
        folder.total_messages = int(remote_folder.get("total_messages") or folder.total_messages or 0)
        folder.unread_count = int(remote_folder.get("unread_count") or 0)


def _refresh_outlook_remote_counts(db: Session, account, Folder) -> None:
    from app.services import outlook as outlook_service

    try:
        folders = outlook_service.list_folders(account.encrypted_credentials)
    except Exception as exc:
        logger.debug("Outlook folder stats refresh failed for %s: %s", account.id, exc)
        return

    for remote_folder in folders:
        folder = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.remote_name == remote_folder["remote_name"],
        ).first()
        if not folder:
            continue
        folder.total_messages = int(remote_folder.get("total_messages") or folder.total_messages or 0)
        folder.unread_count = int(remote_folder.get("unread_count") or 0)


def _refresh_gmail_remote_counts(db: Session, account, Folder, Message) -> None:
    from app.models.message import MessageStatus
    from app.services import gmail as gmail_service

    for folder_type in ("inbox", "sent", "drafts", "spam", "trash"):
        folder = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.folder_type == folder_type,
        ).first()
        if not folder:
            continue
        try:
            stats = gmail_service.get_label_stats(account.encrypted_credentials, folder_type)
            unread_ids = (
                set(gmail_service.list_unread_message_ids(
                    account.encrypted_credentials,
                    folder_type,
                    max_results=min(stats["unread_count"], GMAIL_RECONCILE_LIMIT),
                ))
                if stats["unread_count"] > 0
                else set()
            )
        except Exception as exc:
            logger.debug("Gmail label stats refresh failed for %s/%s: %s", account.id, folder_type, exc)
            continue

        folder.total_messages = stats["total_messages"]
        folder.unread_count = stats["unread_count"]

        if stats["unread_count"] <= GMAIL_RECONCILE_LIMIT:
            local_messages = db.query(Message).filter(
                Message.account_id == account.id,
                Message.folder_id == folder.id,
                Message.remote_id != None,
            ).all()
            for msg in local_messages:
                is_unread = msg.remote_id in unread_ids
                msg.is_read = not is_unread
                if not msg.is_deleted and not msg.is_draft:
                    msg.status = MessageStatus.UNREAD if is_unread else MessageStatus.READ


def _reconcile_gmail_folder(db: Session, Message, MessageStatus, account, folder, present_remote_ids: set[str]) -> None:
    if len(present_remote_ids) >= GMAIL_RECONCILE_LIMIT:
        return

    from app.models.folder import Folder
    from app.services import gmail as gmail_service

    stale = db.query(Message).filter(
        Message.account_id == account.id,
        Message.folder_id == folder.id,
        Message.remote_id != None,
        Message.is_draft == False,
        Message.remote_id.notin_(present_remote_ids),
    ).limit(GMAIL_STALE_STATE_LIMIT).all()
    for msg in stale:
        state = gmail_service.get_message_state(account.encrypted_credentials, msg.remote_id)
        if not state:
            msg.is_deleted = True
            msg.status = MessageStatus.DELETED
            continue

        labels = set(state.get("labels") or [])
        if "TRASH" in labels:
            target_type = "trash"
        elif "SPAM" in labels:
            target_type = "spam"
        elif "DRAFT" in labels:
            target_type = "drafts"
        elif "SENT" in labels:
            target_type = "sent"
        elif "INBOX" in labels:
            target_type = "inbox"
        else:
            target_type = "archive"

        target_folder = _get_or_create_folder(db, Folder, account, target_type)
        msg.folder_id = target_folder.id
        msg.is_read = bool(state.get("is_read", True))
        msg.is_flagged = bool(state.get("is_flagged", False))
        msg.is_draft = target_type == "drafts"
        msg.is_spam = target_type == "spam"
        msg.is_deleted = target_type == "trash"
        msg.status = (
            MessageStatus.DELETED
            if msg.is_deleted
            else MessageStatus.READ if msg.is_read else MessageStatus.UNREAD
        )


def _get_or_create_folder(db: Session, Folder, account, folder_type: str):
    folder = db.query(Folder).filter(
        Folder.account_id == account.id,
        Folder.folder_type == folder_type,
    ).first()
    if folder:
        return folder

    folder = Folder(
        account_id=account.id,
        name=folder_type.title(),
        remote_name=folder_type,
        folder_type=folder_type,
    )
    db.add(folder)
    db.flush()
    return folder


def _sync_imap(db: Session, account):
    from app.models.folder import Folder
    from app.models.message import Message, MessageStatus
    from app.services import imap as imap_service
    from app.services.rule_service import matches_rule, apply_rule_actions
    from app.models.rule import EmailRule

    loop = asyncio.new_event_loop()
    try:
        folders = loop.run_until_complete(
            imap_service.fetch_folders(
                account.encrypted_credentials,
                account.imap_host,
                account.imap_port,
                account.imap_ssl,
            )
        )
        for f in folders:
            existing = db.query(Folder).filter(
                Folder.account_id == account.id,
                Folder.remote_name == f["remote_name"],
            ).first()
            if existing:
                existing.name = f["name"]
                existing.folder_type = f["folder_type"]
                existing.total_messages = f.get("total_messages", existing.total_messages)
                existing.unread_count = f.get("unread_count", existing.unread_count)
            else:
                db.add(Folder(
                    account_id=account.id,
                    name=f["name"],
                    remote_name=f["remote_name"],
                    folder_type=f["folder_type"],
                    total_messages=f.get("total_messages", 0),
                    unread_count=f.get("unread_count", 0),
                ))
        db.flush()

        sync_folders = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.folder_type.in_(SYNC_FOLDER_TYPES),
        ).order_by(Folder.folder_type).all()
        sync_folders = _select_sync_folders(account, sync_folders)

        rules = db.query(EmailRule).filter(
            EmailRule.user_id == account.user_id,
            EmailRule.is_active == True,
        ).order_by(EmailRule.sort_order).all()
        skip_entries = _imap_skip_entries()
        strict_clamp = account.email_address.lower() in _strict_clamp_accounts()
        fetch_limit = min(IMAP_FETCH_LIMIT, settings.imap_strict_fetch_limit) if strict_clamp else IMAP_FETCH_LIMIT

        for folder in sync_folders:
            max_uid = db.query(Message.uid).filter(
                Message.account_id == account.id,
                Message.folder_id == folder.id,
                Message.uid != None,
            ).order_by(Message.uid.desc()).first()
            since_uid = (max_uid[0] + 1) if max_uid and max_uid[0] else None
            try:
                msgs = loop.run_until_complete(
                    imap_service.fetch_messages(
                        account.encrypted_credentials,
                        account.imap_host,
                        account.imap_port,
                        account.imap_ssl,
                        folder.remote_name,
                        since_uid=since_uid,
                        limit=fetch_limit,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Skipping IMAP folder for account=%s folder=%s: %s",
                    account.id,
                    folder.remote_name,
                    str(exc)[:300],
                )
                continue

            for m in msgs:
                if _should_skip_imap_message(skip_entries, account, folder, m):
                    logger.info(
                        "Skipping quarantined IMAP message account=%s folder=%s uid=%s",
                        account.email_address,
                        folder.remote_name,
                        m.get("uid"),
                    )
                    continue
                m = _normalized_payload(m)
                if strict_clamp:
                    m = _strict_clamp_payload(m)
                existing = _find_existing_message(db, Message, account.id, folder.id, m)
                if not existing:
                    try:
                        with db.begin_nested():
                            cols = {c.name for c in Message.__table__.columns}
                            msg = Message(
                                account_id=account.id,
                                folder_id=folder.id,
                                status=MessageStatus.UNREAD if not m["is_read"] else MessageStatus.READ,
                                **{k: v for k, v in m.items() if k in cols},
                            )
                            db.add(msg)
                            db.flush()
                            if folder.folder_type == "inbox":
                                for rule in rules:
                                    if matches_rule(msg, rule):
                                        loop.run_until_complete(apply_rule_actions(msg, rule, db))
                                        rule.times_triggered += 1
                                        rule.last_triggered_at = datetime.utcnow()
                                        if rule.stop_processing:
                                            break
                    except Exception as exc:
                        logger.warning(
                            "Skipping IMAP message for account=%s folder=%s uid=%s: %s",
                            account.id,
                            folder.remote_name,
                            m.get("uid"),
                            str(exc)[:300],
                        )
                        continue
                else:
                    _merge_existing_message(existing, m, MessageStatus)
            db.flush()
    finally:
        loop.close()


def _sync_gmail(db: Session, account):
    from app.models.folder import Folder
    from app.models.message import Message, MessageStatus
    from app.services import gmail as gmail_service

    cols = {c.name for c in Message.__table__.columns}
    for folder_type in GMAIL_SYNC_FOLDER_TYPES:
        folder = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.folder_type == folder_type,
        ).first()
        if not folder:
            folder = Folder(
                account_id=account.id,
                name=folder_type.title(),
                remote_name=folder_type,
                folder_type=folder_type,
            )
            db.add(folder)
            db.flush()

        present_remote_ids = set(gmail_service.list_message_ids(
            account.encrypted_credentials,
            folder=folder_type,
            max_results=GMAIL_RECONCILE_LIMIT,
        ))

        messages = gmail_service.list_messages(
            account.encrypted_credentials,
            folder=folder_type,
            max_results=GMAIL_FETCH_LIMIT,
        )
        for m in messages:
            m = _normalized_payload(m)
            existing = _find_existing_message(db, Message, account.id, folder.id, m)
            if not existing:
                db.add(Message(
                    account_id=account.id,
                    folder_id=folder.id,
                    status=MessageStatus.UNREAD if not m["is_read"] else MessageStatus.READ,
                    **{k: v for k, v in m.items() if k in cols},
                ))
            else:
                _merge_existing_message(existing, m, MessageStatus)

        _reconcile_gmail_folder(db, Message, MessageStatus, account, folder, present_remote_ids)
    db.flush()


def _sync_outlook(db: Session, account):
    from app.models.folder import Folder
    from app.models.message import Message, MessageStatus
    from app.services import outlook as outlook_service

    folders = outlook_service.list_folders(account.encrypted_credentials)
    for f in folders:
        existing = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.remote_name == f["remote_name"],
        ).first()
        if existing:
            existing.unread_count = f.get("unread_count", 0)
            existing.total_messages = f.get("total_messages", 0)
        else:
            db.add(Folder(
                account_id=account.id,
                name=f["name"],
                remote_name=f["remote_name"],
                folder_type=f["folder_type"],
                unread_count=f.get("unread_count", 0),
                total_messages=f.get("total_messages", 0),
            ))
    db.flush()

    cols = {c.name for c in Message.__table__.columns}
    sync_folders = db.query(Folder).filter(
        Folder.account_id == account.id,
        Folder.folder_type.in_(SYNC_FOLDER_TYPES),
    ).all()
    sync_folders = _select_sync_folders(account, sync_folders)
    for folder in sync_folders:
        messages = outlook_service.list_messages(
            account.encrypted_credentials,
            folder_id=folder.remote_name,
            limit=OUTLOOK_FETCH_LIMIT,
        )
        for m in messages:
            m = _normalized_payload(m)
            existing = _find_existing_message(db, Message, account.id, folder.id, m)
            if not existing:
                db.add(Message(
                    account_id=account.id,
                    folder_id=folder.id,
                    status=MessageStatus.UNREAD if not m["is_read"] else MessageStatus.READ,
                    **{k: v for k, v in m.items() if k in cols},
                ))
            else:
                _merge_existing_message(existing, m, MessageStatus)
    db.flush()


def sync_all_accounts_now() -> dict[str, Any]:
    from app.models.email_account import EmailAccount, AccountStatus

    db = _get_db()
    try:
        accounts = db.query(EmailAccount).filter(
            EmailAccount.is_active == True,
            EmailAccount.status != AccountStatus.DISABLED,
        ).all()
    finally:
        db.close()

    stats = {"processed": len(accounts), "synced": 0, "skipped": 0, "auth_errors": 0, "errors": 0}
    for account in accounts:
        result = sync_account_now(str(account.id))
        if result.get("synced"):
            stats["synced"] += 1
        elif result.get("skipped"):
            stats["skipped"] += 1
        elif result.get("auth_error"):
            stats["auth_errors"] += 1
        else:
            stats["errors"] += 1
    return stats


def send_scheduled_emails_now() -> dict[str, int]:
    from app.models.message import Message, MessageStatus
    from app.models.email_account import EmailAccount
    from app.services.email_sender import send_email as _send_email

    db = _get_db()
    loop = asyncio.new_event_loop()
    try:
        msgs = db.query(Message).filter(
            Message.is_draft == True,
            Message.scheduled_send_at <= datetime.utcnow(),
            Message.scheduled_send_at != None,
        ).with_for_update(skip_locked=True).limit(50).all()
        for msg in msgs:
            msg.scheduled_send_at = None
        db.commit()

        sent_count = 0
        for msg in msgs:
            account = db.get(EmailAccount, msg.account_id)
            if not account:
                continue
            try:
                to = [a.get("email", "") for a in (msg.to_addresses or [])]
                cc = [a.get("email", "") for a in (msg.cc_addresses or [])]
                loop.run_until_complete(_send_email(
                    account=account,
                    to=to,
                    subject=msg.subject or "",
                    body_text=msg.body_text,
                    body_html=msg.body_html,
                    cc=cc,
                ))
                msg.is_draft = False
                msg.status = MessageStatus.READ
                msg.sent_at = datetime.utcnow()
                sent_count += 1
            except Exception as exc:
                msg.scheduled_send_at = datetime.utcnow() + timedelta(minutes=5)
                if hasattr(msg, "error_message"):
                    msg.error_message = str(exc)[:500]
        db.commit()
        return {"sent": sent_count, "claimed": len(msgs)}
    finally:
        loop.close()
        db.close()


def run_periodic_jobs_once() -> dict[str, Any]:
    with _engine.begin() as conn:
        acquired = conn.execute(
            text("select pg_try_advisory_xact_lock(hashtext(:lock_name))"),
            {"lock_name": "email_agent_periodic_jobs"},
        ).scalar()
        if not acquired:
            return {"skipped": True, "reason": "lock-held"}

        scheduled = send_scheduled_emails_now()
        sync = sync_all_accounts_now()
        return {"skipped": False, "scheduled": scheduled, "sync": sync}


celery = None
if settings.task_queue_backend.lower() == "celery":
    try:
        from app.tasks.celery_app import celery
    except Exception:
        celery = None


if celery is not None:
    @celery.task(name="app.tasks.sync_tasks.sync_account", bind=True, max_retries=3)
    def sync_account(self, account_id: str):
        try:
            return sync_account_now(account_id, raise_on_error=True)
        except Exception as exc:
            if _is_auth_error(exc):
                return {"synced": False, "account_id": account_id, "auth_error": True}
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))

    @celery.task(name="app.tasks.sync_tasks.sync_all_accounts")
    def sync_all_accounts():
        from app.models.email_account import EmailAccount, AccountStatus

        db = _get_db()
        try:
            accounts = db.query(EmailAccount).filter(
                EmailAccount.is_active == True,
                EmailAccount.status != AccountStatus.DISABLED,
            ).all()
            for account in accounts:
                sync_account.delay(str(account.id))
            return {"queued": len(accounts)}
        finally:
            db.close()

    @celery.task(name="app.tasks.sync_tasks.send_scheduled_emails")
    def send_scheduled_emails():
        return send_scheduled_emails_now()
