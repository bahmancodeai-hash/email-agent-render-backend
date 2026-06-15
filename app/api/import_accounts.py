"""Bulk import of IMAP/SMTP accounts from CSV files."""
import csv
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.email_account import EmailAccount, AccountType, AccountStatus
from app.services.crypto import encrypt_credentials
from app.services import imap as imap_service
from app.services.network_guard import ALLOWED_IMAP_PORTS, ALLOWED_SMTP_PORTS, validate_mail_endpoint

router = APIRouter()

REQUIRED_COLS = {"email", "password"}
OPTIONAL_COLS = {"username", "display_name", "imap_host", "imap_port", "imap_ssl", "smtp_host", "smtp_port", "smtp_ssl", "group_tag"}
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 1000


EMAIL_KEYS = ("email", "e-mail", "email_address", "емайл", "почта", "электронная почта")
PASSWORD_KEYS = (
    "password",
    "pass",
    "email_password",
    "email pass",
    "mail_password",
    "пароль",
    "пароль от емайла",
    "пароль от email",
)
DISPLAY_NAME_KEYS = ("display_name", "name", "full_name", "имя фамилия", "фио", "имя")


def _normalize_key(value: str | None) -> str:
    return (value or "").strip().lower()


def _first(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return ""


def _clean_email(value: str) -> str:
    return value.strip().strip("\ufeff").strip().lower()


def _infer_hosts(email_address: str) -> tuple[str, str]:
    domain = email_address.split("@", 1)[-1].strip().lower()
    known = {
        "gmail.com": ("imap.gmail.com", "smtp.gmail.com"),
        "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
        "outlook.com": ("outlook.office365.com", "smtp.office365.com"),
        "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
        "live.com": ("outlook.office365.com", "smtp.office365.com"),
        "msn.com": ("outlook.office365.com", "smtp.office365.com"),
        "office365.com": ("outlook.office365.com", "smtp.office365.com"),
        "yahoo.com": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"),
        "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com"),
        "me.com": ("imap.mail.me.com", "smtp.mail.me.com"),
        "edumail.az": ("premium68-1.web-hosting.com", "premium68-1.web-hosting.com"),
    }
    if domain in known:
        return known[domain]
    return f"mail.{domain}", f"mail.{domain}"


async def _sort_accounts_az(db: AsyncSession, user_id) -> None:
    result = await db.execute(
        select(EmailAccount)
        .where(EmailAccount.user_id == user_id, EmailAccount.is_active == True)
        .order_by(EmailAccount.email_address)
    )
    for index, account in enumerate(result.scalars().all()):
        account.sort_order = index


def _reader_rows(text: str) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return []

    first_row_keys = {_normalize_key(cell) for cell in rows[0]}
    has_header = bool(first_row_keys & set(EMAIL_KEYS))
    if has_header:
        reader = csv.DictReader(io.StringIO(text))
        return [
            {_normalize_key(k): (v or "").strip() for k, v in row.items() if k}
            for row in reader
        ]

    return [
        {
            "display_name": (row[0] if len(row) > 0 else "").strip(),
            "email": (row[1] if len(row) > 1 else "").strip(),
            "password": (row[2] if len(row) > 2 else "").strip(),
        }
        for row in rows
    ]


def _parse_rows(rows: list[dict[str, str]]) -> list[dict]:
    parsed = []
    for i, row in enumerate(rows):
        row = {_normalize_key(k): v.strip() for k, v in row.items() if k and v}
        email_address = _clean_email(_first(row, EMAIL_KEYS))
        password = _first(row, PASSWORD_KEYS)
        missing = {col for col, value in {"email": email_address, "password": password}.items() if not value}
        if missing:
            raise ValueError(f"Row {i + 2}: missing columns: {missing}")

        imap_host, smtp_host = _infer_hosts(email_address)
        imap_host = row.get("imap_host") or imap_host
        smtp_host = row.get("smtp_host") or smtp_host
        display_name = _first(row, DISPLAY_NAME_KEYS) or email_address

        parsed.append({
            "email_address": email_address,
            "username": row.get("username") or email_address,
            "password": password,
            "imap_host": imap_host,
            "imap_port": int(row.get("imap_port") or 993),
            "imap_ssl": row.get("imap_ssl", "true").lower() not in ("false", "0", "no"),
            "smtp_host": smtp_host,
            "smtp_port": int(row.get("smtp_port") or 465),
            "smtp_ssl": row.get("smtp_ssl", "true").lower() not in ("false", "0", "no"),
            "display_name": display_name,
            "group_tag": row.get("group_tag"),
        })
    return parsed


@router.post("/csv")
async def import_from_csv(
    file: UploadFile = File(...),
    skip_errors: bool = True,
    validate_connection: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import IMAP accounts from a CSV file.

    Supported minimal format: display_name,email,password. Headers are optional.
    Optional headers: username, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, group_tag.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported")

    content = await file.read(MAX_CSV_BYTES + 1)
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV file is too large")
    text = content.decode("utf-8-sig")  # handle BOM
    raw_rows = _reader_rows(text)
    if len(raw_rows) > MAX_CSV_ROWS:
        raise HTTPException(status_code=413, detail=f"CSV can contain at most {MAX_CSV_ROWS} rows")

    try:
        rows = _parse_rows(raw_rows)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    added, skipped, errors = [], [], []
    for row in rows:
        try:
            existing = await db.execute(
                select(EmailAccount.id).where(
                    EmailAccount.user_id == current_user.id,
                    func.lower(EmailAccount.email_address) == row["email_address"].lower(),
                )
            )
            if existing.scalar_one_or_none():
                skipped.append(row["email_address"])
                continue

            row["imap_host"] = await validate_mail_endpoint(row["imap_host"], row["imap_port"], ALLOWED_IMAP_PORTS)
            row["smtp_host"] = await validate_mail_endpoint(row["smtp_host"], row["smtp_port"], ALLOWED_SMTP_PORTS)
            status = AccountStatus.ACTIVE
            error_message = None
            if validate_connection:
                ok = await imap_service.test_connection(
                    row["imap_host"], row["imap_port"],
                    row["username"], row["password"], row["imap_ssl"],
                )
                if not ok:
                    status = AccountStatus.ERROR
                    error_message = "Cannot connect to IMAP server"

            creds = {"username": row["username"], "password": row["password"]}
            account = EmailAccount(
                user_id=current_user.id,
                account_type=AccountType.IMAP,
                email_address=row["email_address"],
                display_name=row["display_name"],
                encrypted_credentials=encrypt_credentials(creds),
                imap_host=row["imap_host"],
                imap_port=row["imap_port"],
                imap_ssl=row["imap_ssl"],
                smtp_host=row["smtp_host"],
                smtp_port=row["smtp_port"],
                smtp_ssl=row["smtp_ssl"],
                group_tag=row["group_tag"],
                status=status,
                error_message=error_message,
            )
            db.add(account)
            await db.flush()
            added.append(row["email_address"])
        except Exception as e:
            if skip_errors:
                errors.append({"email": row["email_address"], "error": str(e)})
            else:
                await db.rollback()
                raise HTTPException(status_code=400, detail=f"Failed on {row['email_address']}: {e}")

    if added:
        await _sort_accounts_az(db, current_user.id)

    return {
        "added": len(added),
        "skipped": len(skipped),
        "errors": len(errors),
        "added_accounts": added,
        "skipped_accounts": skipped,
        "error_details": errors,
    }


@router.get("/template")
async def download_csv_template(current_user: User = Depends(get_current_user)):
    """Download a CSV template for bulk import."""
    from fastapi.responses import Response

    header = "display_name,email,password\n"
    example = "User Name,user@example.com,email-password\n"
    content = header + example
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts_template.csv"},
    )
