"""Daily briefing controls and snapshots always resolve authenticated ownership."""
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, require_mailbox_context, validate_mailbox_context
from app.database.db import get_db
from app.database.models import DailyBriefingSchedule, DailyBriefingRun, ConversationState, Reminder
from app.services.daily_briefing import scope, schedule_id
from app.services import planner

router = APIRouter(prefix='/phone/daily', tags=['daily briefing'])


class Preferences(BaseModel):
    enabled: bool = False
    time: str = '09:00'
    timezone: str = 'Asia/Kolkata'
    catchup_minutes: int = Field(default=180, ge=15, le=360)
    vip_senders: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('time')
    @classmethod
    def valid_time(cls, value):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise ValueError('Use HH:MM')
        return value

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError('Use an IANA timezone') from exc
        return value


@router.get('/schedule')
def read_schedule(context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    row = db.get(DailyBriefingSchedule, schedule_id(context))
    values = {field: getattr(row, field) for field in Preferences.model_fields} if row else Preferences().model_dump()
    return {**values, 'push_status': 'not_configured', 'android_delivery': 'optional_background_polling_not_exact',
            'scheduler_requires_server_activation': True}


@router.post('/schedule')
def save_schedule(request: Preferences, context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    row = db.get(DailyBriefingSchedule, schedule_id(context))
    if row is None:
        row = DailyBriefingSchedule(id=schedule_id(context), user_id=context.user_id,
            mailbox_id=context.mailbox_id, workspace_id=context.workspace_id, provider=context.provider,
            legacy_local=context.legacy_local)
        db.add(row)
    for key, value in request.model_dump().items():
        setattr(row, key, value)
    if not request.enabled:
        ids = select(DailyBriefingRun.id).where(*scope(DailyBriefingRun, context))
        from sqlalchemy import update
        db.execute(update(Reminder).where(Reminder.id.in_(ids), Reminder.status.in_(['DUE', 'SCHEDULED', 'DELIVERING'])).values(status='CANCELLED'))
    db.commit()
    return read_schedule(context, db)


@router.get('/runs')
def runs(context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    rows = db.scalars(select(DailyBriefingRun).where(*scope(DailyBriefingRun, context)).order_by(
        DailyBriefingRun.scheduled_at.desc()).limit(14)).all()
    return {'runs': [{'id': row.id, 'status': row.status, 'scheduled_at': row.scheduled_at,
                     'generated_at': row.generated_at, 'failure_code': row.failure_code} for row in rows]}


@router.get('/runs/{run_id}')
def get_run(run_id: str, context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    validate_mailbox_context(db, context)
    row = db.scalar(select(DailyBriefingRun).where(DailyBriefingRun.id == run_id, *scope(DailyBriefingRun, context)))
    if not row:
        raise HTTPException(404, 'Briefing not found in this account. Sign in to the owning account.')
    age = (datetime.utcnow() - row.generated_at).total_seconds() if row.generated_at else None
    return {'id': row.id, 'status': row.status, 'generated_at': row.generated_at,
            'saved_snapshot': True, 'stale': age is None or age > 1800,
            'failure_code': row.failure_code, 'document': row.document if row.status == 'COMPLETE' else {}}


class OpenRequest(BaseModel):
    session_key: str = Field(min_length=16, max_length=255)


@router.post('/runs/{run_id}/open')
def open_run(run_id: str, request: OpenRequest, context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    result = get_run(run_id, context, db)
    if result['status'] != 'COMPLETE':
        raise HTTPException(409, 'The briefing did not complete; check its status.')
    state = db.scalar(select(ConversationState).where(ConversationState.mailbox_id == context.mailbox_id,
        ConversationState.session_key == request.session_key))
    if state is None:
        state = ConversationState(mailbox_id=context.mailbox_id, session_key=request.session_key,
                                   expires_at=datetime.utcnow())
        db.add(state)
    # References only: MailAgent still revalidates source facts on follow-up turns.
    from datetime import timedelta
    items = result['document'].get('items', [])
    state.result_ids_json = [item['message_id'] for item in items]
    threads = {item['thread_id'] for item in items}
    state.selected_thread_id = next(iter(threads)) if len(threads) == 1 else None
    state.selected_message_id = items[0]['message_id'] if len(items) == 1 else None
    state.topics_json = [{key: item.get(key, '') for key in ('message_id', 'thread_id', 'sender', 'subject')} for item in items[:5]]
    state.planner_context_json = {}
    state.expires_at = datetime.utcnow() + timedelta(hours=2)
    db.commit()
    return result


@router.post('/notifications/claim')
def notifications(context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    row = db.get(DailyBriefingSchedule, schedule_id(context))
    if not row or not row.enabled:
        return {'notifications': []}
    ids = list(db.scalars(select(DailyBriefingRun.id).where(*scope(DailyBriefingRun, context),
        DailyBriefingRun.status == 'COMPLETE')))
    values = planner.claim_notifications(db, context, resource_ids=ids)
    return {'notifications': [{'id': item['id'], 'delivery_token': item['delivery_token']} for item in values]}
