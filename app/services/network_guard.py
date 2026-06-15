import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException


BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
BLOCKED_PORTS = {0}
ALLOWED_IMAP_PORTS = {143, 993}
ALLOWED_SMTP_PORTS = {25, 465, 587, 2525}


def _blocked_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _normalize_host(host: str) -> str:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if not normalized or "/" in normalized or "\\" in normalized or ":" in normalized:
        raise HTTPException(status_code=400, detail="Invalid host")
    return normalized.encode("idna").decode("ascii")


async def _resolve_ips(host: str) -> set[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Host could not be resolved")
    return {info[4][0] for info in infos}


async def validate_public_host(host: str) -> str:
    normalized = _normalize_host(host)
    if normalized in BLOCKED_HOSTNAMES or normalized.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Local hosts are not allowed")

    try:
        if _blocked_ip(normalized):
            raise HTTPException(status_code=400, detail="Private or local IPs are not allowed")
        return normalized
    except ValueError:
        pass

    ips = await _resolve_ips(normalized)
    if not ips or any(_blocked_ip(ip) for ip in ips):
        raise HTTPException(status_code=400, detail="Host resolves to a private or local address")
    return normalized


async def validate_mail_endpoint(host: str, port: int, allowed_ports: set[int]) -> str:
    if port in BLOCKED_PORTS or port not in allowed_ports:
        raise HTTPException(status_code=400, detail=f"Port {port} is not allowed")
    return await validate_public_host(host)


async def validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Webhook URL must use https")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="Webhook URL must include a host")
    await validate_public_host(parsed.hostname)
    if parsed.port and parsed.port not in {443}:
        raise HTTPException(status_code=400, detail="Webhook URL must use port 443")
    return url
