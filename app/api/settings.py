from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import get_settings
from app.database.db import get_db
from app.database.models import Setting


router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    briefing_time: Optional[str] = None
    app_timezone: Optional[str] = None
    tts_enabled: Optional[bool] = None
    tts_rate: Optional[int] = Field(default=None, ge=50, le=400)
    tts_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    model: Optional[str] = None
    vip_senders: Optional[List[str]] = None
    vip_domains: Optional[List[str]] = None
    important_keywords: Optional[List[str]] = None

    @field_validator("briefing_time")
    @classmethod
    def valid_time(cls, value):
        if value is None:
            return value
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("briefing_time must use HH:MM")
        hour, minute = map(int, parts)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("briefing_time is invalid")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("app_timezone must be a valid IANA timezone") from exc
        return value

def _defaults() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "briefing_time": settings.briefing_time,
        "app_timezone": settings.app_timezone,
        "tts_enabled": settings.tts_enabled,
        "tts_rate": settings.tts_rate,
        "tts_volume": settings.tts_volume,
        "model": settings.gemini_model,
        "vip_senders": [],
        "vip_domains": [],
        "important_keywords": [],
    }


def effective_settings(db: Session) -> Dict[str, Any]:
    values = _defaults()
    for row in db.scalars(select(Setting)).all():
        if row.key in values:
            values[row.key] = row.value_json
    return values


@router.get("")
async def read_settings(db: Session = Depends(get_db)):
    return effective_settings(db)


@router.patch("")
async def patch_settings(patch: SettingsPatch, db: Session = Depends(get_db)):
    for key, value in patch.model_dump(exclude_none=True).items():
        row = db.get(Setting, key)
        if row:
            row.value_json = value
        else:
            db.add(Setting(key=key, value_json=value))
    db.commit()
    from app.scheduler.morning_job import refresh_scheduler_schedule
    from app.services.speech_service import get_tts_provider

    get_tts_provider.cache_clear()
    refresh_scheduler_schedule()
    return effective_settings(db)
