import uuid
from collections import defaultdict
from datetime import timedelta
from time import time
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr, field_serializer, field_validator

from app.database import get_db
from app.config import settings
from app.services.auth_service import (
    authenticate_user, create_user, create_access_token,
    decode_token, get_or_create_local_app_user, get_user_by_email
)
from app.models.user import User

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
_rate_attempts: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request, suffix: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{suffix}"


def _enforce_rate_limit(key: str, limit: int = 8, window_seconds: int = 300) -> None:
    now = time()
    attempts = [ts for ts in _rate_attempts[key] if now - ts < window_seconds]
    if len(attempts) >= limit:
        _rate_attempts[key] = attempts
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    attempts.append(now)
    _rate_attempts[key] = attempts


class UserCreate(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 10:
            raise ValueError("Password must be at least 10 characters")
        if value.lower() == value or value.upper() == value or not any(ch.isdigit() for ch in value):
            raise ValueError("Password must include uppercase, lowercase and a number")
        return value


class Token(BaseModel):
    access_token: str
    token_type: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str

    model_config = {"from_attributes": True}

    @field_serializer("id")
    def serialize_id(self, v: uuid.UUID) -> str:
        return str(v)


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    if settings.local_app_token and token == settings.local_app_token:
        return await get_or_create_local_app_user(db)

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        email: str = payload.get("sub")
        if not email:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = await get_user_by_email(db, email)
    if not user or not user.is_active:
        raise credentials_exception
    return user


@router.post("/register", response_model=UserOut)
async def register(data: UserCreate, request: Request, db: AsyncSession = Depends(get_db)):
    _enforce_rate_limit(_client_key(request, f"register:{data.email.lower()}"), limit=5)
    existing = await get_user_by_email(db, data.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = await create_user(db, data.email, data.password)
    return user


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    _enforce_rate_limit(_client_key(request, f"login:{form_data.username.lower()}"))
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
