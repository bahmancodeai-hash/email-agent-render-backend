from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.api.auth import get_current_user
from app.api.accounts import _serialize_account, _sort_accounts_az
from app.database import get_db
from app.models.email_account import AccountStatus, AccountType, EmailAccount
from app.models.user import User
from app.services import cpanel
from app.services.crypto import encrypt_credentials
from app.services import imap as imap_service
from app.services.network_guard import ALLOWED_IMAP_PORTS, ALLOWED_SMTP_PORTS, validate_mail_endpoint
from app.tasks.inprocess_sync_queue import enqueue_account_sync


router = APIRouter()


class CpanelStatusOut(BaseModel):
    configured: bool
    missing: list[str]
    domains: list[str]


class HostingMailboxOut(BaseModel):
    email_address: str
    domain: str
    local_part: str
    disk_used: str | None = None
    disk_quota: str | None = None
    in_agent: bool
    account_id: str | None = None
    account_status: str | None = None


class ConnectMailboxRequest(BaseModel):
    email_address: str
    password: str
    display_name: str | None = None


class CreateMailboxRequest(BaseModel):
    email_address: str
    password: str
    display_name: str | None = None
    quota_mb: int = Field(default=0, ge=0)
    connect_to_agent: bool = True


@router.get("/cpanel/status", response_model=CpanelStatusOut)
async def cpanel_status(current_user: User = Depends(get_current_user)):
    return CpanelStatusOut(
        configured=cpanel.is_configured(),
        missing=cpanel.missing_settings(),
        domains=cpanel.configured_domains(),
    )


@router.get("/cpanel/mailboxes", response_model=list[HostingMailboxOut])
async def list_cpanel_mailboxes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    mailboxes = []
    for domain in cpanel.configured_domains():
        mailboxes.extend(await cpanel.list_mailboxes(domain))

    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == current_user.id,
            EmailAccount.email_address.in_([mailbox.email_address for mailbox in mailboxes]),
        )
    )
    accounts_by_email = {account.email_address.lower(): account for account in result.scalars().all()}
    return [
        HostingMailboxOut(
            email_address=mailbox.email_address,
            domain=mailbox.domain,
            local_part=mailbox.local_part,
            disk_used=mailbox.disk_used,
            disk_quota=mailbox.disk_quota,
            in_agent=bool(accounts_by_email.get(mailbox.email_address) and accounts_by_email[mailbox.email_address].is_active),
            account_id=str(accounts_by_email[mailbox.email_address].id) if accounts_by_email.get(mailbox.email_address) else None,
            account_status=(
                accounts_by_email[mailbox.email_address].status.value
                if accounts_by_email.get(mailbox.email_address) else None
            ),
        )
        for mailbox in mailboxes
    ]


@router.post("/cpanel/connect", response_model=dict)
async def connect_existing_mailbox(
    data: ConnectMailboxRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await _connect_mailbox_to_agent(
        db=db,
        user_id=current_user.id,
        email_address=data.email_address,
        password=data.password,
        display_name=data.display_name,
    )
    await db.flush()
    await _sort_accounts_az(db, current_user.id)
    await db.commit()
    enqueue_account_sync(str(account.id))
    return {"connected": True, "account": _serialize_account(account)}


@router.post("/cpanel/mailboxes", response_model=dict, status_code=201)
async def create_cpanel_mailbox(
    data: CreateMailboxRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    mailbox = await cpanel.create_mailbox(data.email_address, data.password, data.quota_mb)
    account = None
    if data.connect_to_agent:
        account = await _connect_mailbox_to_agent(
            db=db,
            user_id=current_user.id,
            email_address=mailbox.email_address,
            password=data.password,
            display_name=data.display_name,
        )
        await db.flush()
        await _sort_accounts_az(db, current_user.id)
        await db.commit()
        enqueue_account_sync(str(account.id))
    return {
        "created": True,
        "mailbox": mailbox.__dict__,
        "account": _serialize_account(account) if account else None,
    }


async def _connect_mailbox_to_agent(
    db: AsyncSession,
    user_id: uuid.UUID,
    email_address: str,
    password: str,
    display_name: str | None = None,
) -> EmailAccount:
    normalized = email_address.strip().lower()
    local_part, domain = cpanel.split_email(normalized)
    imap_host, smtp_host = cpanel.mail_hosts_for_domain(domain)
    imap_host = await validate_mail_endpoint(imap_host, 993, ALLOWED_IMAP_PORTS)
    smtp_host = await validate_mail_endpoint(smtp_host, 465, ALLOWED_SMTP_PORTS)

    username = f"{local_part}@{domain}"
    ok = await imap_service.test_connection(imap_host, 993, username, password, True)
    if not ok:
        raise HTTPException(status_code=400, detail="Cannot connect to mailbox with this password")

    encrypted_credentials = encrypt_credentials({"username": username, "password": password})
    result = await db.execute(
        select(EmailAccount).where(
            EmailAccount.user_id == user_id,
            EmailAccount.email_address.ilike(username),
        )
    )
    account = result.scalar_one_or_none()
    if account:
        account.account_type = AccountType.IMAP
        account.display_name = display_name or account.display_name or username
        account.encrypted_credentials = encrypted_credentials
        account.imap_host = imap_host
        account.imap_port = 993
        account.imap_ssl = True
        account.smtp_host = smtp_host
        account.smtp_port = 465
        account.smtp_ssl = True
        account.status = AccountStatus.ACTIVE
        account.error_message = None
        account.group_tag = "namecheap"
        account.is_active = True
        return account

    account = EmailAccount(
        user_id=user_id,
        account_type=AccountType.IMAP,
        email_address=username,
        display_name=display_name or username,
        encrypted_credentials=encrypted_credentials,
        imap_host=imap_host,
        imap_port=993,
        imap_ssl=True,
        smtp_host=smtp_host,
        smtp_port=465,
        smtp_ssl=True,
        group_tag="namecheap",
    )
    db.add(account)
    return account
