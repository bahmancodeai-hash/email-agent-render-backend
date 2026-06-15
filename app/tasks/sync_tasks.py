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


def _get_db() -> Session:
    return _SessionLocal()


def sync_account_now(account_id: str, *, raise_on_error: bool = False) -> dict[str, Any]:
    from app.models.email_account import EmailAccount, AccountType, AccountStatus

    db = _get_db()
    try:
        account = db.get(EmailAccount, uuid.UUID(account_id))
        if not account or not account.is_active:
            return {"skipped": True, "account_id": account_id}

        if account.account_type == AccountType.IMAP:
            _sync_imap(db, account)
        elif account.account_type == AccountType.GMAIL:
            _sync_gmail(db, account)
        elif account.account_type == AccountType.OUTLOOK:
            _sync_outlook(db, account)

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
            if not existing:
                db.add(Folder(
                    account_id=account.id,
                    name=f["name"],
                    remote_name=f["remote_name"],
                    folder_type=f["folder_type"],
                ))
        db.flush()

        sync_folders = db.query(Folder).filter(
            Folder.account_id == account.id,
            Folder.folder_type.in_(SYNC_FOLDER_TYPES),
        ).order_by(Folder.folder_type).all()

        rules = db.query(EmailRule).filter(
            EmailRule.user_id == account.user_id,
            EmailRule.is_active == True,
        ).order_by(EmailRule.sort_order).all()

        for folder in sync_folders:
            max_uid = db.query(Message.uid).filter(
                Message.account_id == account.id,
                Message.folder_id == folder.id,
                Message.uid != None,
            ).order_by(Message.uid.desc()).first()
            since_uid = (max_uid[0] + 1) if max_uid and max_uid[0] else None
            msgs = loop.run_until_complete(
                imap_service.fetch_messages(
                    account.encrypted_credentials,
                    account.imap_host,
                    account.imap_port,
                    account.imap_ssl,
                    folder.remote_name,
                    since_uid=since_uid,
                    limit=100,
                )
            )

            for m in msgs:
                existing = db.query(Message).filter(
                    Message.account_id == account.id,
                    Message.uid == m["uid"],
                    Message.folder_id == folder.id,
                ).first()
                if not existing:
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

        messages = gmail_service.list_messages(
            account.encrypted_credentials,
            folder=folder_type,
            max_results=100,
        )
        for m in messages:
            existing = db.query(Message).filter(
                Message.account_id == account.id,
                Message.message_id == m["message_id"],
                Message.folder_id == folder.id,
            ).first()
            if not existing:
                db.add(Message(
                    account_id=account.id,
                    folder_id=folder.id,
                    status=MessageStatus.UNREAD if not m["is_read"] else MessageStatus.READ,
                    **{k: v for k, v in m.items() if k in cols},
                ))
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
    for folder in sync_folders:
        messages = outlook_service.list_messages(
            account.encrypted_credentials,
            folder_id=folder.remote_name,
            limit=100,
        )
        for m in messages:
            existing = db.query(Message).filter(
                Message.account_id == account.id,
                Message.message_id == m["message_id"],
                Message.folder_id == folder.id,
            ).first()
            if not existing:
                db.add(Message(
                    account_id=account.id,
                    folder_id=folder.id,
                    status=MessageStatus.UNREAD if not m["is_read"] else MessageStatus.READ,
                    **{k: v for k, v in m.items() if k in cols},
                ))
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
