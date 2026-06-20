import uuid
import logging
from html import escape
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.email_account import EmailAccount, AccountType, AccountStatus
from app.models.folder import Folder
from app.services.auth_service import create_oauth_state, decode_oauth_state
from app.services.crypto import encrypt_credentials
from app.services.network_guard import (
    ALLOWED_IMAP_PORTS,
    ALLOWED_SMTP_PORTS,
    validate_mail_endpoint,
)
from app.services import gmail as gmail_service
from app.services import imap as imap_service
from app.services import outlook as outlook_service
from app.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)


class ImapAccountCreate(BaseModel):
    email_address: str
    display_name: str | None = None
    username: str
    password: str
    imap_host: str
    imap_port: int = 993
    imap_ssl: bool = True
    smtp_host: str
    smtp_port: int = 465
    smtp_ssl: bool = True
    group_tag: str | None = None


class AccountOut(BaseModel):
    id: str
    account_type: str
    email_address: str
    display_name: str | None
    status: str
    error_message: str | None = None
    last_sync_at: str | None
    unread_count: int
    group_tag: str | None

    class Config:
        from_attributes = True


class AccountUpdate(BaseModel):
    display_name: str | None = None
    group_tag: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class AccountReorderRequest(BaseModel):
    account_ids: list[uuid.UUID]


class OAuthProviderStatus(BaseModel):
    configured: bool
    redirect_uri: str
    missing: list[str]


class OAuthStatusOut(BaseModel):
    gmail: OAuthProviderStatus
    outlook: OAuthProviderStatus


def _serialize_account(account: EmailAccount, inbox_unread_count: int | None = None) -> dict:
    return {
        "id": str(account.id),
        "account_type": account.account_type.value if hasattr(account.account_type, "value") else account.account_type,
        "email_address": account.email_address,
        "display_name": account.display_name,
        "status": account.status.value if hasattr(account.status, "value") else account.status,
        "error_message": account.error_message,
        "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
        "unread_count": inbox_unread_count if inbox_unread_count is not None else account.unread_count,
        "group_tag": account.group_tag,
    }


async def _sort_accounts_az(db: AsyncSession, user_id: uuid.UUID) -> None:
    result = await db.execute(
        select(EmailAccount)
        .where(EmailAccount.user_id == user_id, EmailAccount.is_active == True)
        .order_by(EmailAccount.email_address)
    )
    for index, account in enumerate(result.scalars().all()):
        account.sort_order = index


def _oauth_success_page(provider: str, email: str) -> HTMLResponse:
    safe_provider = escape(provider)
    safe_email = escape(email)
    return HTMLResponse(
        f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Email Agent</title>
    <style>
      body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f6f7f9; color:#111827; margin:0; display:grid; place-items:center; min-height:100vh; }}
      main {{ background:white; border:1px solid #e5e7eb; border-radius:12px; padding:28px; width:min(460px, calc(100vw - 32px)); box-shadow:0 18px 60px rgba(15,23,42,.12); }}
      h1 {{ margin:0 0 8px; font-size:20px; }}
      p {{ margin:8px 0; color:#4b5563; line-height:1.5; }}
      .hint {{ font-size:13px; color:#6b7280; }}
      .ok {{ width:36px; height:36px; border-radius:50%; background:#16a34a; color:white; display:grid; place-items:center; font-weight:700; margin-bottom:14px; }}
    </style>
    <script>
      let seconds = 3;
      function tick() {{
        const node = document.getElementById("countdown");
        if (node) node.textContent = String(seconds);
        if (seconds <= 0) {{
          window.close();
          document.body.classList.add("closing");
        }}
        seconds -= 1;
      }}
      window.addEventListener("load", () => {{
        tick();
        window.setInterval(tick, 1000);
      }});
    </script>
  </head>
  <body>
    <main>
      <div class="ok">✓</div>
      <h1>{safe_provider} подключен</h1>
      <p>{safe_email}</p>
      <p>Возвращаюсь в Email Agent. Аккаунт откроется в Inbox автоматически.</p>
      <p class="hint">Это окно закроется через <span id="countdown">3</span> сек.</p>
    </main>
  </body>
</html>"""
    )


def _oauth_error_page(provider: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Email Agent</title>
    <style>
      body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f6f7f9; color:#111827; margin:0; display:grid; place-items:center; min-height:100vh; }}
      main {{ background:white; border:1px solid #fecaca; border-radius:12px; padding:28px; width:min(520px, calc(100vw - 32px)); box-shadow:0 18px 60px rgba(15,23,42,.12); }}
      h1 {{ margin:0 0 8px; font-size:20px; }}
      p {{ margin:8px 0; color:#4b5563; line-height:1.5; }}
      .bad {{ width:36px; height:36px; border-radius:50%; background:#dc2626; color:white; display:grid; place-items:center; font-weight:700; margin-bottom:14px; }}
    </style>
  </head>
  <body>
    <main>
      <div class="bad">!</div>
      <h1>{provider} не подключен</h1>
      <p>{message}</p>
      <p>Закрой это окно, вернись в Email Agent и попробуй снова после исправления настроек.</p>
    </main>
  </body>
</html>""",
        status_code=400,
    )


def _provider_status(provider: str) -> OAuthProviderStatus:
    if provider == "gmail":
        checks = {
            "GMAIL_CLIENT_ID": settings.gmail_client_id,
            "GMAIL_CLIENT_SECRET": settings.gmail_client_secret,
            "GMAIL_REDIRECT_URI": settings.gmail_redirect_uri,
        }
        redirect_uri = settings.gmail_redirect_uri
    else:
        checks = {
            "OUTLOOK_CLIENT_ID": settings.outlook_client_id,
            "OUTLOOK_CLIENT_SECRET": settings.outlook_client_secret,
            "OUTLOOK_REDIRECT_URI": settings.outlook_redirect_uri,
        }
        redirect_uri = settings.outlook_redirect_uri
    missing = [key for key, value in checks.items() if not value]
    return OAuthProviderStatus(configured=not missing, redirect_uri=redirect_uri, missing=missing)


def _require_oauth_provider(provider: str) -> None:
    status = _provider_status(provider)
    if status.configured:
        return
    name = "Gmail" if provider == "gmail" else "Outlook"
    raise HTTPException(
        status_code=503,
        detail={
            "code": f"{provider}_oauth_not_configured",
            "message": f"{name} OAuth is not configured on the backend.",
            "missing": status.missing,
            "redirect_uri": status.redirect_uri,
        },
    )


@router.get("/", response_model=list[AccountOut])
async def list_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount)
        .where(EmailAccount.user_id == current_user.id, EmailAccount.is_active == True)
        .order_by(EmailAccount.sort_order, EmailAccount.email_address)
    )
    accounts = result.scalars().all()
    if not accounts:
        return []

    inbox_counts_result = await db.execute(
        select(Folder.account_id, Folder.unread_count).where(
            Folder.account_id.in_([account.id for account in accounts]),
            Folder.folder_type == "inbox",
        )
    )
    inbox_counts = {account_id: unread_count for account_id, unread_count in inbox_counts_result.all()}
    return [_serialize_account(account, inbox_counts.get(account.id)) for account in accounts]


@router.post("/reorder")
async def reorder_accounts(
    data: AccountReorderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not data.account_ids:
        return {"updated": 0}
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == current_user.id,
            EmailAccount.is_active == True,
            EmailAccount.id.in_(data.account_ids),
        )
    )
    accounts_by_id = {account.id: account for account in result.scalars().all()}
    updated = 0
    for index, account_id in enumerate(data.account_ids):
        account = accounts_by_id.get(account_id)
        if account:
            account.sort_order = index
            updated += 1
    return {"updated": updated}


@router.post("/sort/az")
async def sort_accounts_az(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _sort_accounts_az(db, current_user.id)
    return {"sorted": True}


@router.post("/imap", response_model=AccountOut, status_code=201)
async def add_imap_account(
    data: ImapAccountCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data.imap_host = await validate_mail_endpoint(data.imap_host, data.imap_port, ALLOWED_IMAP_PORTS)
    data.smtp_host = await validate_mail_endpoint(data.smtp_host, data.smtp_port, ALLOWED_SMTP_PORTS)

    ok = await imap_service.test_connection(
        data.imap_host, data.imap_port, data.username, data.password, data.imap_ssl
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot connect to IMAP server")

    credentials = {"username": data.username, "password": data.password}
    account = EmailAccount(
        user_id=current_user.id,
        account_type=AccountType.IMAP,
        email_address=data.email_address,
        display_name=data.display_name or data.email_address,
        encrypted_credentials=encrypt_credentials(credentials),
        imap_host=data.imap_host,
        imap_port=data.imap_port,
        imap_ssl=data.imap_ssl,
        smtp_host=data.smtp_host,
        smtp_port=data.smtp_port,
        smtp_ssl=data.smtp_ssl,
        group_tag=data.group_tag,
    )
    db.add(account)
    await db.flush()
    await _sort_accounts_az(db, current_user.id)
    await db.refresh(account)
    return _serialize_account(account)


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: uuid.UUID,
    data: AccountUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.id == account_id, EmailAccount.user_id == current_user.id
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if data.display_name is not None:
        account.display_name = data.display_name
    if data.group_tag is not None:
        account.group_tag = data.group_tag
    if data.sort_order is not None:
        account.sort_order = data.sort_order
    if data.is_active is not None:
        account.is_active = data.is_active
    return _serialize_account(account)


@router.delete("/{account_id}")
async def remove_account(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.id == account_id, EmailAccount.user_id == current_user.id
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.is_active = False
    return {"removed": True}


@router.get("/{account_id}/folders")
async def get_account_folders(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.id == account_id, EmailAccount.user_id == current_user.id
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Account not found")
    result = await db.execute(
        select(Folder).where(Folder.account_id == account_id)
        .order_by(Folder.folder_type)
    )
    return list(result.scalars().all())


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

@router.get("/oauth/status", response_model=OAuthStatusOut)
async def oauth_status(current_user: User = Depends(get_current_user)):
    return OAuthStatusOut(
        gmail=_provider_status("gmail"),
        outlook=_provider_status("outlook"),
    )

@router.get("/gmail/auth-url")
async def gmail_auth_url(current_user: User = Depends(get_current_user)):
    _require_oauth_provider("gmail")
    return {"auth_url": gmail_service.get_auth_url(create_oauth_state("gmail", current_user.id))}


@router.get("/gmail/callback")
async def gmail_callback(
    code: str,
    state: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    try:
        user_id = decode_oauth_state(state, "gmail")
        creds_dict = gmail_service.exchange_code(code)
        encrypted = encrypt_credentials(creds_dict)
        email_address = gmail_service.get_user_email(encrypted)
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == user_id,
                EmailAccount.email_address.ilike(email_address),
            )
        )
        account = result.scalar_one_or_none()
        if account:
            account.account_type = AccountType.GMAIL
            account.encrypted_credentials = encrypted
            account.status = AccountStatus.ACTIVE
            account.error_message = None
            account.is_active = True
            if not account.display_name or account.display_name == account.email_address:
                account.display_name = email_address
        else:
            account = EmailAccount(
                user_id=user_id,
                account_type=AccountType.GMAIL,
                email_address=email_address,
                display_name=email_address,
                encrypted_credentials=encrypted,
            )
            db.add(account)
        await db.flush()
        await _sort_accounts_az(db, user_id)
        await db.commit()
        from app.tasks.sync_tasks import sync_account_now
        background_tasks.add_task(sync_account_now, str(account.id))
        return _oauth_success_page("Gmail", email_address)
    except Exception as exc:
        logger.exception("Gmail OAuth callback failed")
        return _oauth_error_page("Gmail", str(exc)[:500] or "OAuth callback failed.")


# ── Outlook OAuth ─────────────────────────────────────────────────────────────

@router.get("/outlook/auth-url")
async def outlook_auth_url(current_user: User = Depends(get_current_user)):
    _require_oauth_provider("outlook")
    return {"auth_url": outlook_service.get_auth_url(create_oauth_state("outlook", current_user.id))}


@router.get("/outlook/callback")
async def outlook_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    try:
        user_id = decode_oauth_state(state, "outlook")
        creds_dict = outlook_service.exchange_code(code)
        encrypted = encrypt_credentials(creds_dict)
        email_address = outlook_service.get_user_email(encrypted)
        result = await db.execute(
            select(EmailAccount).where(
                EmailAccount.user_id == user_id,
                EmailAccount.email_address.ilike(email_address),
            )
        )
        account = result.scalar_one_or_none()
        if account:
            account.account_type = AccountType.OUTLOOK
            account.encrypted_credentials = encrypted
            account.status = AccountStatus.ACTIVE
            account.error_message = None
            account.is_active = True
            if not account.display_name or account.display_name == account.email_address:
                account.display_name = email_address
        else:
            account = EmailAccount(
                user_id=user_id,
                account_type=AccountType.OUTLOOK,
                email_address=email_address,
                display_name=email_address,
                encrypted_credentials=encrypted,
            )
            db.add(account)
        await db.flush()
        await _sort_accounts_az(db, user_id)
        return _oauth_success_page("Outlook", email_address)
    except Exception as exc:
        logger.exception("Outlook OAuth callback failed")
        return _oauth_error_page("Outlook", str(exc)[:500] or "OAuth callback failed.")
