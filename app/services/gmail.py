import base64
import json
import os
from datetime import datetime
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings
from app.services.crypto import decrypt_credentials, encrypt_credentials


REQUIRED_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

SCOPES = [
    *REQUIRED_GMAIL_SCOPES,
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def get_oauth_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uris": [settings.gmail_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = settings.gmail_redirect_uri
    return flow


def get_auth_url(state: str) -> str:
    flow = get_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return auth_url


def exchange_code(code: str) -> dict:
    flow = get_oauth_flow()
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code)
    creds = flow.credentials
    granted_scopes = set(creds.scopes or [])
    missing_required = [scope for scope in REQUIRED_GMAIL_SCOPES if scope not in granted_scopes]
    if missing_required:
        raise ValueError(f"Gmail did not grant required permissions: {', '.join(missing_required)}")
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }


def _build_service(encrypted_credentials: str):
    creds_dict = decrypt_credentials(encrypted_credentials)
    creds = Credentials(
        token=creds_dict["token"],
        refresh_token=creds_dict["refresh_token"],
        token_uri=creds_dict["token_uri"],
        client_id=creds_dict["client_id"],
        client_secret=creds_dict["client_secret"],
        scopes=creds_dict["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds), creds


def get_user_email(encrypted_credentials: str) -> str:
    service, _ = _build_service(encrypted_credentials)
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def list_messages(encrypted_credentials: str, folder: str = "INBOX", max_results: int = 50) -> list[dict]:
    service, _ = _build_service(encrypted_credentials)
    label_id = _folder_to_label(folder)
    messages = []
    page_token = None
    while len(messages) < max_results:
        request_limit = min(500, max_results - len(messages))
        result = service.users().messages().list(
            userId="me",
            labelIds=[label_id],
            maxResults=request_limit,
            pageToken=page_token,
        ).execute()

        for item in result.get("messages", []):
            msg_data = service.users().messages().get(
                userId="me", id=item["id"], format="full"
            ).execute()
            parsed = _parse_gmail_message(msg_data)
            if parsed:
                messages.append(parsed)

        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def list_message_ids(encrypted_credentials: str, folder: str = "INBOX", max_results: int = 1000) -> list[str]:
    service, _ = _build_service(encrypted_credentials)
    label_id = _folder_to_label(folder)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        request_limit = min(500, max_results - len(ids))
        result = service.users().messages().list(
            userId="me",
            labelIds=[label_id],
            maxResults=request_limit,
            pageToken=page_token,
            fields="messages/id,nextPageToken",
        ).execute()
        ids.extend(item["id"] for item in result.get("messages", []) if item.get("id"))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return ids


def list_unread_message_ids(encrypted_credentials: str, folder: str = "INBOX", max_results: int = 1000) -> list[str]:
    service, _ = _build_service(encrypted_credentials)
    label_id = _folder_to_label(folder)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        request_limit = min(500, max_results - len(ids))
        result = service.users().messages().list(
            userId="me",
            labelIds=[label_id],
            q="is:unread",
            maxResults=request_limit,
            pageToken=page_token,
            fields="messages/id,nextPageToken",
        ).execute()
        ids.extend(item["id"] for item in result.get("messages", []) if item.get("id"))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return ids


def get_label_stats(encrypted_credentials: str, folder: str = "INBOX") -> dict:
    service, _ = _build_service(encrypted_credentials)
    label_id = _folder_to_label(folder)
    result = service.users().labels().get(userId="me", id=label_id).execute()
    return {
        "total_messages": int(result.get("messagesTotal") or 0),
        "unread_count": int(result.get("messagesUnread") or 0),
    }


def mark_read(encrypted_credentials: str, remote_id: str, is_read: bool = True) -> None:
    service, _ = _build_service(encrypted_credentials)
    body = {"removeLabelIds": ["UNREAD"]} if is_read else {"addLabelIds": ["UNREAD"]}
    service.users().messages().modify(userId="me", id=remote_id, body=body).execute()


def set_starred(encrypted_credentials: str, remote_id: str, is_flagged: bool = True) -> None:
    service, _ = _build_service(encrypted_credentials)
    body = {"addLabelIds": ["STARRED"]} if is_flagged else {"removeLabelIds": ["STARRED"]}
    service.users().messages().modify(userId="me", id=remote_id, body=body).execute()


def archive_message(encrypted_credentials: str, remote_id: str) -> None:
    service, _ = _build_service(encrypted_credentials)
    service.users().messages().modify(
        userId="me",
        id=remote_id,
        body={"removeLabelIds": ["INBOX"]},
    ).execute()


def trash_message(encrypted_credentials: str, remote_id: str) -> None:
    service, _ = _build_service(encrypted_credentials)
    service.users().messages().trash(userId="me", id=remote_id).execute()


def get_message_state(encrypted_credentials: str, remote_id: str) -> dict | None:
    service, _ = _build_service(encrypted_credentials)
    try:
        msg_data = service.users().messages().get(
            userId="me", id=remote_id, format="metadata", metadataHeaders=["Message-ID"]
        ).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in {404, 410}:
            return None
        raise
    labels = msg_data.get("labelIds", [])
    return {
        "remote_id": msg_data.get("id"),
        "thread_id": msg_data.get("threadId"),
        "is_read": "UNREAD" not in labels,
        "is_flagged": "STARRED" in labels,
        "is_draft": "DRAFT" in labels,
        "is_spam": "SPAM" in labels,
        "is_deleted": "TRASH" in labels,
        "labels": labels,
    }


def _folder_to_label(folder: str) -> str:
    mapping = {
        "INBOX": "INBOX", "inbox": "INBOX",
        "sent": "SENT", "SENT": "SENT",
        "drafts": "DRAFT", "DRAFTS": "DRAFT",
        "spam": "SPAM", "SPAM": "SPAM",
        "trash": "TRASH", "TRASH": "TRASH",
    }
    return mapping.get(folder, "INBOX")


def _parse_gmail_message(msg_data: dict) -> Optional[dict]:
    try:
        headers = {h["name"].lower(): h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
        labels = msg_data.get("labelIds", [])
        snippet = msg_data.get("snippet", "")
        thread_id = msg_data.get("threadId")
        gmail_id = msg_data.get("id")

        body_text, body_html = _extract_body(msg_data.get("payload", {}))

        return {
            "remote_id": gmail_id,
            "message_id": headers.get("message-id", gmail_id),
            "thread_id": thread_id,
            "subject": headers.get("subject", ""),
            "from_address": headers.get("from", ""),
            "to_addresses": [{"email": a.strip()} for a in headers.get("to", "").split(",") if a.strip()],
            "cc_addresses": [{"email": a.strip()} for a in headers.get("cc", "").split(",") if a.strip()],
            "body_text": body_text,
            "body_html": body_html,
            "preview": snippet,
            "is_read": "UNREAD" not in labels,
            "is_flagged": "STARRED" in labels,
            "is_draft": "DRAFT" in labels,
            "is_spam": "SPAM" in labels,
            "is_deleted": "TRASH" in labels,
            "received_at": datetime.fromtimestamp(int(msg_data.get("internalDate", 0)) / 1000),
            "attachments": [],
            "uid": None,
        }
    except Exception:
        return None


def _extract_body(payload: dict) -> tuple[Optional[str], Optional[str]]:
    body_text = None
    body_html = None
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body_text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            body_html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif "multipart" in mime_type:
        for part in payload.get("parts", []):
            t, h = _extract_body(part)
            if t and not body_text:
                body_text = t
            if h and not body_html:
                body_html = h

    return body_text, body_html
