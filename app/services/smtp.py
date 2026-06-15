import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional

import aiosmtplib

from app.services.crypto import decrypt_credentials


async def send_email(
    encrypted_credentials: str,
    smtp_host: str,
    smtp_port: int,
    smtp_ssl: bool,
    from_address: str,
    to: list[str],
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    attachments: list[dict] | None = None,
) -> bool:
    creds = decrypt_credentials(encrypted_credentials)
    username = creds["username"]
    password = creds["password"]

    msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
    msg["From"] = from_address
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    recipients = list(to) + (cc or []) + (bcc or [])

    use_tls = smtp_port == 465 and smtp_ssl
    use_starttls = smtp_port in (587, 25) and smtp_ssl

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=smtp_port,
        username=username,
        password=password,
        use_tls=use_tls,
        start_tls=use_starttls,
        recipients=recipients,
    )
    return True


async def test_smtp_connection(host: str, port: int, username: str, password: str, ssl: bool = True) -> bool:
    try:
        use_tls = port == 465 and ssl
        use_starttls = port in (587, 25) and ssl
        smtp = aiosmtplib.SMTP(hostname=host, port=port, use_tls=use_tls)
        await smtp.connect()
        if use_starttls:
            await smtp.starttls()
        await smtp.login(username, password)
        await smtp.quit()
        return True
    except Exception:
        return False
