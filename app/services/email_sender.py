import asyncio
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from email.header import Header
from email.utils import formataddr

from app.models.email_account import EmailAccount, AccountType
from app.services import smtp as smtp_service
from app.services import gmail as gmail_service
from app.services import outlook as outlook_service

_executor = ThreadPoolExecutor(max_workers=10)


def _formatted_from(account: EmailAccount) -> str:
    display_name = (account.display_name or "").strip()
    if not display_name or display_name.lower() == account.email_address.lower():
        return account.email_address
    return formataddr((str(Header(display_name, "utf-8")), account.email_address))


async def send_email(
    account: EmailAccount,
    to: list[str],
    subject: str,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> bool:
    from_address = _formatted_from(account)

    if account.account_type == AccountType.GMAIL:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor,
            _send_gmail,
            account.encrypted_credentials,
            from_address, to, subject, body_text, body_html, cc, bcc, in_reply_to,
        )
    elif account.account_type == AccountType.OUTLOOK:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor,
            outlook_service.send_message,
            account.encrypted_credentials, to, subject, body_html, body_text, cc, in_reply_to,
        )
    else:
        return await smtp_service.send_email(
            encrypted_credentials=account.encrypted_credentials,
            smtp_host=account.smtp_host,
            smtp_port=account.smtp_port,
            smtp_ssl=account.smtp_ssl,
            from_address=from_address,
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            in_reply_to=in_reply_to,
        )


def _send_gmail(
    encrypted_credentials, from_address, to, subject,
    body_text, body_html, cc, bcc, in_reply_to
):
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from app.services.gmail import _build_service

    service, _ = _build_service(encrypted_credentials)
    msg = MIMEMultipart("alternative")
    msg["From"] = from_address
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return True
