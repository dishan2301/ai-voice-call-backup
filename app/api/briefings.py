from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_generation_service
from app.services.settings_service import get_runtime_settings
from app.database.db import get_db
from app.database.models import Briefing, BriefingRun
from app.schemas.briefing import BriefingDocument
from app.services.persistent_briefing_service import PersistentBriefingService
from app.services.persistent_briefing_service import BriefingGenerationError


router = APIRouter(prefix="/briefings", tags=["briefings"])


def _briefing_dict(row: Briefing, include_text: bool = True):
    result = {
        "id": row.id,
        "briefing_date": row.briefing_date,
        "generated_at": row.generated_at,
        "period_start": row.period_start,
        "period_end": row.period_end,
        "total_emails": row.total_emails,
        "critical_count": row.critical_count,
        "high_count": row.high_count,
        "reply_count": row.reply_count,
        "action_count": row.action_count,
        "low_priority_count": row.low_priority_count,
        "status": row.status,
    }
    if include_text:
        result.update(detailed_brief=row.detailed_brief, spoken_brief=row.spoken_brief)
    return result


@router.post("/generate")
async def generate_briefing(
    response: Response,
    service: PersistentBriefingService = Depends(get_generation_service),
) -> dict | BriefingDocument:
    # Production generation is sync-first and returns the last truthful cache
    # immediately while queued analysis catches up. Lightweight test doubles
    # that only expose generate() retain the legacy synchronous contract.
    if hasattr(service, "latest_cached_document"):
        document = service.latest_cached_document()
        if document is None:
            raise HTTPException(status_code=202, detail={
                "status": "analysis_pending",
                "analysis_pending": True,
                "message": "Mail sync is complete; analysis is still processing.",
            })
        return {"status": "analysis_pending", "analysis_pending": True, "briefing": document}
    try:
        response.status_code = status.HTTP_201_CREATED
        return service.generate(datetime.now(timezone.utc))
    except BriefingGenerationError as exc:
        message = (
            "Email sync failed. Today's briefing may be incomplete."
            if exc.stage == "EMAIL_SYNC"
            else "Email analysis failed. Today's briefing may be incomplete."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=message,
        ) from exc


@router.post("/generate-async")
async def generate_briefing_async(
    service: PersistentBriefingService = Depends(get_generation_service),
):
    """Return the last truthful briefing immediately while queued analysis continues."""
    document = service.latest_cached_document()
    if document is None:
        raise HTTPException(status_code=202, detail={
            "status": "analysis_pending",
            "message": "Mail sync is complete; analysis is still processing.",
        })
    return {"status": "analysis_pending", "analysis_pending": True, "briefing": document}


@router.get("/today")
async def today(db: Session = Depends(get_db)):
    settings = get_runtime_settings()
    local_date = datetime.now(timezone.utc).astimezone(settings.timezone).date()
    row = db.scalar(
        select(Briefing)
        .join(BriefingRun, BriefingRun.briefing_id == Briefing.id)
        .where(Briefing.briefing_date == local_date)
        .where(BriefingRun.provider == settings.email_provider)
        .order_by(Briefing.generated_at.desc())
        .limit(1)
    )
    if not row:
        raise HTTPException(status_code=404, detail="No briefing exists for today")
    return _briefing_dict(row)


@router.get("")
async def history(db: Session = Depends(get_db)) -> List[dict]:
    provider = get_runtime_settings().email_provider
    rows = db.scalars(
        select(Briefing)
        .join(BriefingRun, BriefingRun.briefing_id == Briefing.id)
        .where(BriefingRun.provider == provider)
        .order_by(Briefing.generated_at.desc())
    ).all()
    return [_briefing_dict(row, include_text=False) for row in rows]


@router.get("/{briefing_id}")
async def briefing_detail(briefing_id: int, db: Session = Depends(get_db)):
    row = db.get(Briefing, briefing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Briefing not found")
    return _briefing_dict(row)
