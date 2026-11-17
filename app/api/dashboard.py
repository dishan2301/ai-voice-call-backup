from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_generation_service
from app.api.settings import SettingsPatch, effective_settings
from app.core.config import get_settings
from app.database.db import get_db
from app.database.models import Briefing, BriefingItem, BriefingRun, Email, EmailAnalysis, Setting
from app.services.persistent_briefing_service import PersistentBriefingService
from app.services.speech_service import SpeechService, get_tts_provider, tts_provider_dependency
from app.speech.base import TTSProvider


router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


@router.get("/")
async def dashboard(
    request: Request,
    briefing_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    preferences = effective_settings(db)
    data_source = get_settings().email_provider
    from zoneinfo import ZoneInfo

    local_date = datetime.now(timezone.utc).astimezone(ZoneInfo(preferences["app_timezone"])).date()
    briefing_query = (
        select(Briefing)
        .join(BriefingRun, BriefingRun.briefing_id == Briefing.id)
        .where(BriefingRun.provider == data_source)
    )
    if briefing_id is not None:
        briefing_query = briefing_query.where(Briefing.id == briefing_id)
    else:
        briefing_query = briefing_query.where(Briefing.briefing_date == local_date)
    today = db.scalar(briefing_query.order_by(Briefing.generated_at.desc()).limit(1))
    items = []
    if today:
        items = db.execute(
            select(BriefingItem, EmailAnalysis, Email)
            .join(EmailAnalysis, EmailAnalysis.id == BriefingItem.email_analysis_id)
            .join(Email, Email.id == EmailAnalysis.email_id)
            .where(BriefingItem.briefing_id == today.id)
            .order_by(BriefingItem.rank)
        ).all()
    history = db.scalars(
        select(Briefing)
        .join(BriefingRun, BriefingRun.briefing_id == Briefing.id)
        .where(BriefingRun.provider == data_source)
        .order_by(Briefing.generated_at.desc())
        .limit(30)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="dashboard/index.html",
        context={
            "today": today,
            "items": items,
            "history": history,
            "settings": preferences,
            "data_source": data_source,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/dashboard/generate")
async def dashboard_generate(
    service: PersistentBriefingService = Depends(get_generation_service),
):
    try:
        service.generate(datetime.now(timezone.utc))
    except Exception:
        return RedirectResponse(
            url="/?error=Email+sync+or+analysis+failed.+Today%27s+briefing+may+be+incomplete.",
            status_code=303,
        )
    return RedirectResponse(url="/", status_code=303)


@router.post("/dashboard/briefings/{briefing_id}/speak")
async def dashboard_speak(
    briefing_id: int,
    provider: TTSProvider = Depends(tts_provider_dependency),
):
    try:
        SpeechService(provider).speak(briefing_id)
    except Exception:
        return RedirectResponse(
            url="/?error=Text-to-speech+is+unavailable.+The+text+briefing+is+still+available.",
            status_code=303,
        )
    return RedirectResponse(url="/", status_code=303)


@router.post("/dashboard/speech/stop")
async def dashboard_stop(provider: TTSProvider = Depends(tts_provider_dependency)):
    provider.stop()
    return RedirectResponse(url="/", status_code=303)


@router.post("/dashboard/items/{item_id}/{action}")
async def dashboard_item_action(item_id: int, action: str, db: Session = Depends(get_db)):
    item = db.get(BriefingItem, item_id)
    if item and action in {"done", "track", "snooze"}:
        if action == "done":
            item.user_status = "RESOLVED"
            item.snoozed_until = None
        elif action == "track":
            analysis = db.get(EmailAnalysis, item.email_analysis_id)
            item.user_status = "NEEDS_REPLY" if analysis and analysis.needs_reply else "NEEDS_ACTION"
            item.snoozed_until = None
        else:
            from datetime import timedelta

            item.user_status = "WAITING"
            item.snoozed_until = datetime.utcnow() + timedelta(days=1)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/dashboard/settings")
async def dashboard_settings(
    briefing_time: str = Form(...),
    app_timezone: str = Form(...),
    tts_enabled: Optional[str] = Form(None),
    tts_rate: int = Form(...),
    tts_volume: float = Form(...),
    model: str = Form(""),
    vip_senders: str = Form(""),
    vip_domains: str = Form(""),
    important_keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    values = SettingsPatch(
        briefing_time=briefing_time,
        app_timezone=app_timezone,
        tts_enabled=tts_enabled is not None,
        tts_rate=tts_rate,
        tts_volume=tts_volume,
        model=model,
        vip_senders=[value.strip() for value in vip_senders.split(",") if value.strip()],
        vip_domains=[value.strip() for value in vip_domains.split(",") if value.strip()],
        important_keywords=[value.strip() for value in important_keywords.split(",") if value.strip()],
    ).model_dump(exclude_none=True)
    for key, value in values.items():
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
    return RedirectResponse(url="/", status_code=303)
