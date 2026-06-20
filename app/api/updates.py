from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings

router = APIRouter()


class MobileUpdateInfo(BaseModel):
    platform: str = "android"
    version: str
    version_code: int = Field(alias="versionCode")
    release_label: str
    notes: str
    apk_url: str
    required: bool

    model_config = {"populate_by_name": True}


@router.get("/mobile/latest", response_model=MobileUpdateInfo)
async def latest_mobile_update():
    return MobileUpdateInfo(
        version=settings.mobile_apk_version,
        versionCode=settings.mobile_apk_version_code,
        release_label=settings.mobile_release_label,
        notes=settings.mobile_release_notes,
        apk_url=settings.mobile_apk_url,
        required=settings.mobile_update_required,
    )
