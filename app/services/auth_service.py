from datetime import datetime, timedelta
from typing import Optional
import uuid

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.user import User
from app.models.device import Device, DeviceStatus


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)


def create_oauth_state(provider: str, user_id: uuid.UUID) -> str:
    return create_access_token(
        {"sub": str(user_id), "typ": "oauth_state", "provider": provider},
        expires_delta=timedelta(minutes=10),
    )


def decode_oauth_state(token: str, provider: str) -> uuid.UUID:
    payload = decode_token(token)
    if payload.get("typ") != "oauth_state" or payload.get("provider") != provider:
        raise JWTError("Invalid OAuth state")
    return uuid.UUID(payload["sub"])


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[User]:
    user = await get_user_by_email(db, email)
    if not user or not user.is_active or not verify_password(password, user.hashed_password):
        return None
    return user


async def create_user(db: AsyncSession, email: str, password: str) -> User:
    user = User(email=email, hashed_password=get_password_hash(password))
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def get_or_create_local_app_user(db: AsyncSession) -> User:
    user = await get_user_by_email(db, settings.local_app_email)
    if user:
        if not user.is_active:
            user.is_active = True
            await db.flush()
            await db.refresh(user)
        return user
    return await create_user(db, settings.local_app_email, settings.local_app_token)


async def get_trusted_devices(db: AsyncSession, user_id: uuid.UUID) -> list[Device]:
    result = await db.execute(
        select(Device).where(Device.user_id == user_id, Device.status == DeviceStatus.TRUSTED)
    )
    return list(result.scalars().all())


async def get_pending_device(db: AsyncSession, pairing_code: str) -> Optional[Device]:
    result = await db.execute(
        select(Device).where(
            Device.pairing_code == pairing_code,
            Device.status == DeviceStatus.PENDING,
            Device.pairing_expires_at > datetime.utcnow(),
        )
    )
    return result.scalar_one_or_none()
