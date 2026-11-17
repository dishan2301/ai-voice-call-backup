from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.database.models import BriefingItem, EmailAnalysis


router = APIRouter(prefix="/items", tags=["items"])


class SnoozeRequest(BaseModel):
    minutes: int = Field(default=1440, ge=1, le=43_200)


def _item(db: Session, item_id: int) -> BriefingItem:
    item = db.get(BriefingItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Briefing item not found")
    return item


def _response(item: BriefingItem):
    return {
        "id": item.id,
        "user_status": item.user_status,
        "snoozed_until": item.snoozed_until,
    }


@router.post("/{item_id}/done")
async def done(item_id: int, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    item.user_status = "RESOLVED"
    item.snoozed_until = None
    db.commit()
    return _response(item)


@router.post("/{item_id}/snooze")
async def snooze(item_id: int, request: SnoozeRequest, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    item.user_status = "WAITING"
    item.snoozed_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=request.minutes)
    db.commit()
    return _response(item)


@router.post("/{item_id}/track")
async def track(item_id: int, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    analysis = db.get(EmailAnalysis, item.email_analysis_id)
    item.user_status = "NEEDS_REPLY" if analysis and analysis.needs_reply else "NEEDS_ACTION"
    item.snoozed_until = None
    db.commit()
    return _response(item)
