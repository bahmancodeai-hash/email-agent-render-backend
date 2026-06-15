import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.folder import Folder
from app.models.message import Message


REFRESH_COUNTS_SQL = """
WITH counts AS (
    SELECT
        account_id,
        count(*) FILTER (WHERE is_deleted = false AND is_draft = false) AS total_messages,
        count(*) FILTER (WHERE is_deleted = false AND is_draft = false AND is_read = false) AS unread_count
    FROM messages
    GROUP BY account_id
)
UPDATE email_accounts AS account
SET
    total_messages = coalesce(counts.total_messages, 0),
    unread_count = coalesce(counts.unread_count, 0),
    updated_at = now()
FROM counts
WHERE account.id = counts.account_id
"""


RESET_EMPTY_ACCOUNT_COUNTS_SQL = """
UPDATE email_accounts AS account
SET total_messages = 0, unread_count = 0, updated_at = now()
WHERE NOT EXISTS (
    SELECT 1 FROM messages WHERE messages.account_id = account.id
)
"""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value).strip()


def _message_id(value: Any) -> str:
    normalized = _clean(value).lower()
    return "" if normalized in {"", "none", "null"} else normalized


def _folder_scope(row) -> str:
    return row.folder_type or f"folder:{row.folder_id or 'none'}"


def _stable_key(row) -> tuple:
    message_id = _message_id(row.message_id)
    if message_id:
        return (row.account_id, _folder_scope(row), "mid", message_id)
    if row.uid is not None:
        return (row.account_id, f"folder:{row.folder_id or 'none'}", "uid", row.uid)
    return _fingerprint_key(row)


def _fingerprint_key(row) -> tuple:
    return (
        row.account_id,
        _folder_scope(row),
        _clean(row.subject).lower(),
        _clean(row.from_address).lower(),
        _clean(row.to_addresses).lower(),
        _clean(row.received_at),
        _clean(row.sent_at),
        _clean(row.preview).lower(),
    )


def _message_rows(db: Session):
    return (
        db.query(
            Message.id,
            Message.account_id,
            Message.folder_id,
            Folder.folder_type.label("folder_type"),
            Message.uid,
            Message.message_id,
            Message.subject,
            Message.from_address,
            Message.to_addresses,
            Message.received_at,
            Message.sent_at,
            Message.preview,
            Message.created_at,
        )
        .outerjoin(Folder, Message.folder_id == Folder.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .yield_per(1000)
    )


def _collect_duplicate_ids(db: Session, mode: str, excluded_ids: set | None = None) -> list:
    excluded_ids = excluded_ids or set()
    seen = set()
    duplicates = []
    key_fn = _stable_key if mode == "stable" else _fingerprint_key
    for row in _message_rows(db):
        if row.id in excluded_ids:
            continue
        key = key_fn(row)
        if key in seen:
            duplicates.append(row.id)
        else:
            seen.add(key)
    return duplicates


def _delete_ids(db: Session, ids: list) -> int:
    deleted = 0
    for index in range(0, len(ids), 500):
        chunk = ids[index:index + 500]
        deleted += db.query(Message).filter(Message.id.in_(chunk)).delete(synchronize_session=False)
        db.flush()
    return deleted


def refresh_account_counts(db: Session) -> None:
    db.execute(text(REFRESH_COUNTS_SQL))
    db.execute(text(RESET_EMPTY_ACCOUNT_COUNTS_SQL))


def run_message_dedupe(db: Session, *, apply: bool, max_delete: int | None = None) -> dict:
    stable_ids = _collect_duplicate_ids(db, "stable")
    fingerprint_ids = _collect_duplicate_ids(db, "fingerprint", excluded_ids=set(stable_ids))

    result = {
        "applied": apply,
        "stable_duplicates": len(stable_ids),
        "fingerprint_duplicates": len(fingerprint_ids),
        "stable_deleted": 0,
        "fingerprint_deleted": 0,
        "truncated": False,
    }

    if not apply:
        return result

    remaining = max_delete
    stable_to_delete = stable_ids if remaining is None else stable_ids[:remaining]
    result["stable_deleted"] = _delete_ids(db, stable_to_delete)
    if remaining is not None:
        remaining -= len(stable_to_delete)

    if remaining is None or remaining > 0:
        fingerprint_ids = _collect_duplicate_ids(db, "fingerprint")
        fingerprint_to_delete = fingerprint_ids if remaining is None else fingerprint_ids[:remaining]
        result["fingerprint_deleted"] = _delete_ids(db, fingerprint_to_delete)
        if remaining is not None:
            remaining -= len(fingerprint_to_delete)

    result["truncated"] = max_delete is not None and remaining == 0
    refresh_account_counts(db)
    return result
