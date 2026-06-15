import httpx
from datetime import datetime, timedelta
from typing import Optional

import msal

from app.config import settings
from app.services.crypto import decrypt_credentials, encrypt_credentials


SCOPES = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _build_msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=settings.outlook_client_id,
        client_credential=settings.outlook_client_secret,
        authority=f"https://login.microsoftonline.com/{settings.outlook_tenant}",
    )


def get_auth_url(state: str) -> str:
    app = _build_msal_app()
    result = app.get_authorization_request_url(
        scopes=SCOPES,
        state=state,
        redirect_uri=settings.outlook_redirect_uri,
    )
    return result


def exchange_code(code: str) -> dict:
    app = _build_msal_app()
    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=SCOPES,
        redirect_uri=settings.outlook_redirect_uri,
    )
    if "error" in result:
        raise ValueError(result.get("error_description", result["error"]))
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "expires_in": result.get("expires_in", 3600),
        "token_type": result.get("token_type", "Bearer"),
        "scope": " ".join(SCOPES),
    }


def _get_valid_token(encrypted_credentials: str) -> str:
    creds = decrypt_credentials(encrypted_credentials)
    app = _build_msal_app()
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    result = app.acquire_token_by_refresh_token(creds["refresh_token"], scopes=SCOPES)
    if "error" in result:
        raise ValueError(result.get("error_description", result["error"]))
    return result["access_token"]


def get_user_email(encrypted_credentials: str) -> str:
    token = _get_valid_token(encrypted_credentials)
    resp = httpx.get(
        f"{GRAPH_BASE}/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["mail"] or resp.json()["userPrincipalName"]


def list_folders(encrypted_credentials: str) -> list[dict]:
    token = _get_valid_token(encrypted_credentials)
    resp = httpx.get(
        f"{GRAPH_BASE}/me/mailFolders",
        headers={"Authorization": f"Bearer {token}"},
        params={"$top": 100},
        timeout=15,
    )
    resp.raise_for_status()
    folders = []
    for f in resp.json().get("value", []):
        folders.append({
            "remote_name": f["id"],
            "name": f["displayName"],
            "folder_type": _detect_folder_type(f.get("wellKnownName", "")),
            "total_messages": f.get("totalItemCount", 0),
            "unread_count": f.get("unreadItemCount", 0),
        })
    return folders


def _detect_folder_type(well_known: str) -> str:
    well_known = well_known.lower()
    mapping = {
        "inbox": "inbox", "sentitems": "sent", "drafts": "drafts",
        "deleteditems": "trash", "junkemail": "spam", "archive": "archive",
    }
    return mapping.get(well_known, "custom")


def list_messages(encrypted_credentials: str, folder_id: str = "inbox", limit: int = 50) -> list[dict]:
    token = _get_valid_token(encrypted_credentials)
    resp = httpx.get(
        f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"$top": limit, "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,flag,hasAttachments,bodyPreview,conversationId,internetMessageId,body"},
        timeout=20,
    )
    resp.raise_for_status()
    return [_parse_message(m) for m in resp.json().get("value", [])]


def _parse_message(m: dict) -> dict:
    sender = m.get("from", {}).get("emailAddress", {})
    received = m.get("receivedDateTime")
    if received:
        received = datetime.fromisoformat(received.replace("Z", "+00:00"))

    return {
        "remote_id": m.get("id"),
        "message_id": m.get("internetMessageId", m["id"]),
        "thread_id": m.get("conversationId"),
        "subject": m.get("subject", ""),
        "from_address": sender.get("address", ""),
        "from_name": sender.get("name", ""),
        "to_addresses": [{"email": r["emailAddress"]["address"], "name": r["emailAddress"].get("name", "")} for r in m.get("toRecipients", [])],
        "cc_addresses": [{"email": r["emailAddress"]["address"], "name": r["emailAddress"].get("name", "")} for r in m.get("ccRecipients", [])],
        "body_text": m.get("body", {}).get("content") if m.get("body", {}).get("contentType") == "text" else None,
        "body_html": m.get("body", {}).get("content") if m.get("body", {}).get("contentType") == "html" else None,
        "preview": m.get("bodyPreview", ""),
        "is_read": m.get("isRead", False),
        "is_flagged": m.get("flag", {}).get("flagStatus") == "flagged",
        "is_draft": False,
        "is_deleted": False,
        "is_spam": False,
        "received_at": received,
        "attachments": [],
        "uid": None,
    }


def send_message(
    encrypted_credentials: str,
    to: list[str],
    subject: str,
    body_html: Optional[str] = None,
    body_text: Optional[str] = None,
    cc: list[str] | None = None,
    reply_to_message_id: Optional[str] = None,
) -> bool:
    token = _get_valid_token(encrypted_credentials)
    content = body_html or body_text or ""
    content_type = "html" if body_html else "text"

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
            "ccRecipients": [{"emailAddress": {"address": addr}} for addr in (cc or [])],
        },
        "saveToSentItems": True,
    }

    resp = httpx.post(
        f"{GRAPH_BASE}/me/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    return True


def reply_to_message(encrypted_credentials: str, graph_message_id: str, body: str, reply_all: bool = False) -> bool:
    token = _get_valid_token(encrypted_credentials)
    action = "replyAll" if reply_all else "reply"
    resp = httpx.post(
        f"{GRAPH_BASE}/me/messages/{graph_message_id}/{action}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": {}, "comment": body},
        timeout=20,
    )
    resp.raise_for_status()
    return True
