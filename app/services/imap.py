import asyncio
import email
import email.policy
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from imapclient import IMAPClient

from app.services.crypto import decrypt_credentials


_executor = ThreadPoolExecutor(max_workers=20)


def _connect_imap(host: str, port: int, username: str, password: str, ssl: bool = True) -> IMAPClient:
    client = IMAPClient(host, port=port, ssl=ssl, timeout=30)
    client.login(username, password)
    return client


def _fetch_folders_sync(host: str, port: int, username: str, password: str, ssl: bool) -> list[dict]:
    client = _connect_imap(host, port, username, password, ssl)
    try:
        folders = []
        for flags, delimiter, name in client.list_folders():
            folder_type = _detect_folder_type(flags, name)
            folders.append({
                "remote_name": name,
                "name": name.split(delimiter.decode() if delimiter else "/")[-1] if delimiter else name,
                "folder_type": folder_type,
                "flags": [str(f) for f in flags],
            })
        return folders
    finally:
        client.logout()


def _detect_folder_type(flags, name: str) -> str:
    flag_map = {
        b"\\Inbox": "inbox", b"\\Sent": "sent", b"\\Drafts": "drafts",
        b"\\Trash": "trash", b"\\Junk": "spam", b"\\Spam": "spam",
        b"\\Archive": "archive", b"\\All": "all",
    }
    for flag in flags:
        if flag in flag_map:
            return flag_map[flag]
    name_lower = name.lower()
    if "inbox" in name_lower:
        return "inbox"
    if "sent" in name_lower:
        return "sent"
    if "draft" in name_lower:
        return "drafts"
    if "trash" in name_lower or "deleted" in name_lower:
        return "trash"
    if "spam" in name_lower or "junk" in name_lower:
        return "spam"
    if "archive" in name_lower:
        return "archive"
    return "custom"


def _fetch_messages_sync(
    host: str, port: int, username: str, password: str, ssl: bool,
    folder_name: str, since_uid: Optional[int] = None, limit: int = 50
) -> list[dict]:
    client = _connect_imap(host, port, username, password, ssl)
    try:
        client.select_folder(folder_name, readonly=True)
        if since_uid:
            uids = client.search(["UID", f"{since_uid}:*"])
        else:
            uids = client.search(["ALL"])

        uids = uids[-limit:]
        if not uids:
            return []

        messages = []
        fetch_data = client.fetch(uids, ["RFC822", "FLAGS", "INTERNALDATE", "UID"])
        for uid, data in fetch_data.items():
            try:
                raw = data.get(b"RFC822", b"")
                flags = data.get(b"FLAGS", [])
                internal_date = data.get(b"INTERNALDATE")
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                parsed = _parse_message(msg, uid, flags, internal_date)
                messages.append(parsed)
            except Exception:
                continue
        return messages
    finally:
        client.logout()


def _parse_message(msg, uid: int, flags: list, internal_date) -> dict:
    def decode_header(value) -> str:
        if not value:
            return ""
        try:
            from email.header import decode_header as dh
            parts = dh(str(value))
            result = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    result.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    result.append(str(part))
            return " ".join(result)
        except Exception:
            return str(value)

    def parse_addresses(header_value) -> list[dict]:
        if not header_value:
            return []
        from email.utils import getaddresses
        return [{"name": name, "email": addr} for name, addr in getaddresses([str(header_value)])]

    body_text = None
    body_html = None
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disposition = str(part.get_content_disposition() or "")
            if "attachment" in disposition or part.get_filename():
                attachments.append({
                    "name": part.get_filename() or "attachment",
                    "content_type": ct,
                    "size": len(part.get_payload(decode=True) or b""),
                })
            elif ct == "text/plain" and not body_text:
                try:
                    body_text = part.get_content()
                except Exception:
                    body_text = part.get_payload(decode=True).decode("utf-8", errors="replace")
            elif ct == "text/html" and not body_html:
                try:
                    body_html = part.get_content()
                except Exception:
                    body_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
    else:
        ct = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = payload.decode("utf-8", errors="replace") if payload else ""
        if ct == "text/html":
            body_html = content
        else:
            body_text = content

    is_read = b"\\Seen" in flags
    is_flagged = b"\\Flagged" in flags
    is_draft = b"\\Draft" in flags
    is_deleted = b"\\Deleted" in flags

    preview = ""
    if body_text:
        preview = " ".join(body_text.split())[:200]
    elif body_html:
        import re
        text = re.sub(r"<[^>]+>", " ", body_html)
        preview = " ".join(text.split())[:200]

    return {
        "uid": uid,
        "message_id": str(msg.get("Message-ID", "")),
        "subject": decode_header(msg.get("Subject")),
        "from_address": str(msg.get("From", "")),
        "from_name": "",
        "to_addresses": parse_addresses(msg.get("To")),
        "cc_addresses": parse_addresses(msg.get("CC")),
        "bcc_addresses": parse_addresses(msg.get("BCC")),
        "reply_to": str(msg.get("Reply-To", "")),
        "in_reply_to": str(msg.get("In-Reply-To", "")),
        "body_text": body_text,
        "body_html": body_html,
        "preview": preview,
        "attachments": attachments,
        "is_read": is_read,
        "is_flagged": is_flagged,
        "is_draft": is_draft,
        "is_deleted": is_deleted,
        "received_at": internal_date,
    }


async def fetch_folders(encrypted_credentials: str, imap_host: str, imap_port: int, imap_ssl: bool) -> list[dict]:
    creds = decrypt_credentials(encrypted_credentials)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        _fetch_folders_sync,
        imap_host, imap_port, creds["username"], creds["password"], imap_ssl
    )


async def fetch_messages(
    encrypted_credentials: str, imap_host: str, imap_port: int, imap_ssl: bool,
    folder_name: str, since_uid: Optional[int] = None, limit: int = 50
) -> list[dict]:
    creds = decrypt_credentials(encrypted_credentials)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        _fetch_messages_sync,
        imap_host, imap_port, creds["username"], creds["password"], imap_ssl,
        folder_name, since_uid, limit
    )


async def test_connection(host: str, port: int, username: str, password: str, ssl: bool = True) -> bool:
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            _connect_imap, host, port, username, password, ssl
        )
        return True
    except Exception:
        return False
