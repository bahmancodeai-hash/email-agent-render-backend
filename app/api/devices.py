import uuid
import base64
from io import BytesIO
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.device import Device, DeviceStatus
from app.services.crypto import generate_device_key, generate_pairing_code
from app.services.auth_service import get_trusted_devices, get_pending_device
from app.config import settings

router = APIRouter()


class DeviceRegister(BaseModel):
    name: str
    platform: str


class DeviceOut(BaseModel):
    id: str
    name: str
    platform: str
    status: str
    last_seen_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class PairingConfirm(BaseModel):
    pairing_code: str


@router.get("/", response_model=list[DeviceOut])
async def list_devices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.user_id == current_user.id))
    return list(result.scalars().all())


@router.post("/register", response_model=DeviceOut)
async def register_first_device(
    data: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    trusted = await get_trusted_devices(db, current_user.id)
    if trusted:
        raise HTTPException(status_code=400, detail="Use pairing flow to add additional devices")
    device = Device(
        user_id=current_user.id,
        name=data.name,
        platform=data.platform,
        device_key=generate_device_key(),
        status=DeviceStatus.TRUSTED,
    )
    db.add(device)
    await db.flush()
    await db.refresh(device)
    return device


@router.post("/pairing/initiate")
async def initiate_pairing(
    data: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    trusted = await get_trusted_devices(db, current_user.id)
    if len(trusted) >= settings.max_trusted_devices:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.max_trusted_devices} trusted devices reached",
        )
    code = generate_pairing_code()
    device = Device(
        user_id=current_user.id,
        name=data.name,
        platform=data.platform,
        device_key=generate_device_key(),
        status=DeviceStatus.PENDING,
        pairing_code=code,
        pairing_expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(device)
    await db.flush()
    return {"pairing_code": code, "expires_in_minutes": 10}


@router.get("/pairing/qr")
async def get_pairing_qr(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a QR code for device pairing. Returns PNG image."""
    import qrcode

    trusted = await get_trusted_devices(db, current_user.id)
    if len(trusted) >= settings.max_trusted_devices:
        raise HTTPException(status_code=400, detail="Device limit reached")

    code = generate_pairing_code()
    device = Device(
        user_id=current_user.id,
        name="New Device",
        platform="unknown",
        device_key=generate_device_key(),
        status=DeviceStatus.PENDING,
        pairing_code=code,
        pairing_expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    db.add(device)
    await db.flush()

    qr_data = f"emailagent://pair?code={code}&user={current_user.id}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.post("/pairing/confirm")
async def confirm_pairing(
    data: PairingConfirm,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    device = await get_pending_device(db, data.pairing_code)
    if not device or device.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Invalid or expired pairing code")
    device.status = DeviceStatus.TRUSTED
    device.pairing_code = None
    device.pairing_expires_at = None
    await db.flush()
    return {"trusted": True, "device_id": str(device.id)}


@router.delete("/{device_id}")
async def revoke_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == current_user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.status = DeviceStatus.REVOKED
    return {"revoked": True}
