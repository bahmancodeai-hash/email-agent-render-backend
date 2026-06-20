from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.network_guard import validate_public_host


@dataclass(frozen=True)
class CpanelMailbox:
    email_address: str
    domain: str
    local_part: str
    disk_used: str | None = None
    disk_quota: str | None = None


def configured_domains() -> list[str]:
    domains = []
    for value in settings.cpanel_domains.split(","):
      domain = value.strip().lower().strip(".")
      if domain and domain not in domains:
          domains.append(domain)
    return domains


def is_configured() -> bool:
    return bool(settings.cpanel_base_url and settings.cpanel_username and settings.cpanel_api_token)


def missing_settings() -> list[str]:
    missing = []
    if not settings.cpanel_base_url:
        missing.append("CPANEL_BASE_URL")
    if not settings.cpanel_username:
        missing.append("CPANEL_USERNAME")
    if not settings.cpanel_api_token:
        missing.append("CPANEL_API_TOKEN")
    if not configured_domains():
        missing.append("CPANEL_DOMAINS")
    return missing


def split_email(email_address: str) -> tuple[str, str]:
    normalized = email_address.strip().lower()
    if "@" not in normalized:
        raise HTTPException(status_code=400, detail="Invalid email address")
    local_part, domain = normalized.rsplit("@", 1)
    if not local_part or not domain:
        raise HTTPException(status_code=400, detail="Invalid email address")
    if domain not in configured_domains():
        raise HTTPException(status_code=400, detail=f"Domain {domain} is not allowed for cPanel integration")
    return local_part, domain


def mail_hosts_for_domain(domain: str) -> tuple[str, str]:
    imap_host = settings.cpanel_imap_host.strip() or f"mail.{domain}"
    smtp_host = settings.cpanel_smtp_host.strip() or f"mail.{domain}"
    return imap_host, smtp_host


async def _base_url() -> str:
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail={"code": "cpanel_not_configured", "missing": missing_settings()},
        )
    parsed = urlparse(settings.cpanel_base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="CPANEL_BASE_URL must be an https URL")
    await validate_public_host(parsed.hostname)
    return settings.cpanel_base_url.rstrip("/")


async def _uapi(function: str, params: dict[str, str | int]) -> dict:
    base_url = await _base_url()
    url = f"{base_url}/execute/Email/{function}"
    headers = {"Authorization": f"cpanel {settings.cpanel_username}:{settings.cpanel_api_token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params, headers=headers)
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=502, detail="cPanel rejected API credentials")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"cPanel API error: HTTP {response.status_code}")
    data = response.json()
    status = data.get("status")
    errors = data.get("errors") or data.get("error")
    if status in {0, "0", False} or errors:
        message = "; ".join(errors) if isinstance(errors, list) else str(errors or "cPanel API returned an error")
        raise HTTPException(status_code=502, detail=message)
    return data


async def list_mailboxes(domain: str) -> list[CpanelMailbox]:
    normalized = domain.strip().lower().strip(".")
    if normalized not in configured_domains():
        raise HTTPException(status_code=400, detail=f"Domain {normalized} is not allowed")
    data = await _uapi("list_pops_with_disk", {"domain": normalized})
    rows = data.get("data") or []
    mailboxes: list[CpanelMailbox] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        email_address = (row.get("email") or row.get("login") or "").strip().lower()
        if "@" not in email_address:
            local_part = (row.get("user") or row.get("email") or "").strip().lower()
            if not local_part:
                continue
            email_address = f"{local_part}@{normalized}"
        local_part, mailbox_domain = split_email(email_address)
        mailboxes.append(CpanelMailbox(
            email_address=email_address,
            domain=mailbox_domain,
            local_part=local_part,
            disk_used=str(row.get("humandiskused") or row.get("diskused") or "") or None,
            disk_quota=str(row.get("humandiskquota") or row.get("diskquota") or "") or None,
        ))
    return sorted(mailboxes, key=lambda item: item.email_address)


async def create_mailbox(email_address: str, password: str, quota_mb: int = 0) -> CpanelMailbox:
    local_part, domain = split_email(email_address)
    if not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="Mailbox password must be at least 8 characters")
    await _uapi("add_pop", {
        "email": local_part,
        "domain": domain,
        "password": password,
        "quota": max(0, quota_mb),
        "send_welcome_email": 0,
    })
    return CpanelMailbox(email_address=f"{local_part}@{domain}", domain=domain, local_part=local_part)
