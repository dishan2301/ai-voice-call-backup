"""Owned daily snapshots; bounded catch-up and one durable notification outbox item."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError

from app.core.auth import MailboxContext, validate_mailbox_context
from app.core.config import get_settings
from app.database.db import SessionLocal
from app.database.models import (DailyBriefingSchedule, DailyBriefingRun, Email, AuthSession,
                                 Reminder, ConversationState, PlannerAudit)


def scope(model, context):
    return model.user_id == context.user_id, model.mailbox_id == context.mailbox_id


def schedule_id(context):
    return hashlib.sha256(f'{context.user_id}:{context.mailbox_id}'.encode()).hexdigest()


def due(schedule, now):
    local = now.astimezone(ZoneInfo(schedule.timezone))
    hour, minute = map(int, schedule.time.split(':'))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not schedule.enabled or local < target or local - target > timedelta(minutes=schedule.catchup_minutes):
        return None
    return local.date().isoformat(), target.astimezone(timezone.utc).replace(tzinfo=None)


def context_for(db, schedule, now):
    session_id = None
    if schedule.legacy_local:
        settings = get_settings()
        if settings.mailbox_id != schedule.mailbox_id or settings.email_provider != schedule.provider:
            raise ValueError('ACCOUNT_CHANGED')
    if not schedule.legacy_local:
        session_id = db.scalar(select(AuthSession.id).where(AuthSession.user_id == schedule.user_id,
            AuthSession.mailbox_id == schedule.mailbox_id, AuthSession.workspace_id == schedule.workspace_id,
            AuthSession.revoked_at.is_(None), AuthSession.expires_at > now).order_by(AuthSession.expires_at.desc()))
        if not session_id:
            raise ValueError('SIGN_IN_REQUIRED')
    context = MailboxContext(schedule.user_id, schedule.workspace_id, schedule.mailbox_id,
                             schedule.provider, session_id, schedule.legacy_local)
    validate_mailbox_context(db, context)
    return context


def build_snapshot(context, schedule, now, factory=SessionLocal):
    from app.api.dependencies import create_generation_service
    # Reuse owned on-demand Gmail sync; never invoke the legacy unscoped briefing publisher.
    create_generation_service(context, session_factory=factory).sync_only(now.replace(tzinfo=timezone.utc))
    with factory() as db:
        validate_mailbox_context(db, context)
        rows = db.scalars(select(Email).where(Email.mailbox_id == context.mailbox_id,
            Email.provider == context.provider, Email.is_deleted.is_(False),
            Email.received_at >= now - timedelta(days=30)).order_by(Email.received_at.desc()).limit(500)).all()
        candidates = []
        for row in rows:
            text = (row.normalized_body or row.snippet or '').strip()
            unread = 'UNREAD' in (row.labels_json or [])
            vip = row.sender_email.casefold() in {item.casefold() for item in schedule.vip_senders}
            relevant = bool(re.search(r'urgent|deadline|follow.up|meeting|due|pending|invoice', row.subject + ' ' + text, re.I))
            if not (unread or vip or relevant):
                continue
            candidates.append((int(vip)*4 + int(relevant)*2 + int(unread), {
                'message_id': row.provider_message_id, 'thread_id': row.thread_id,
                'sender': row.sender_name or row.sender_email, 'subject': row.subject,
                'received_at': row.received_at.isoformat() + 'Z', 'summary': ' '.join(text.split())[:240],
                'unread_at_sync': unread, 'priority_sender': vip, 'source': 'Gmail',
            }))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        items = [item for _, item in candidates[:12]]
    calendar = []
    calendar_status = 'unavailable_or_not_authorized'
    try:
        from app.services.calendar_provider import calendar_provider
        local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(schedule.timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        with factory() as db:
            result = calendar_provider(db, context, get_settings()).read(start.isoformat(), (start + timedelta(days=1)).isoformat())
            calendar = [{key: item.get(key) for key in ('id', 'summary', 'start', 'end', 'htmlLink')}
                        for item in result.get('items', [])[:10]]
            calendar_status = 'partial' if result.get('nextPageToken') else 'verified_at_generation'
    except Exception:
        pass
    return {'items': items, 'calendar': calendar, 'calendar_status': calendar_status,
            'generated_at': now.isoformat() + 'Z', 'bounded': True,
            'answer': f'Saved morning briefing: {len(items)} mail items and {len(calendar)} Calendar events. '
                      'Mail summaries are source excerpts. Open a source for full details.',
            'spoken_answer': f'Your saved morning briefing contains {len(items)} mail items. Ask about a sender or choose an email for details.'}


def run_schedule(schedule_id_value, now=None, factory=SessionLocal, builder=build_snapshot):
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError('Use timezone-aware scheduler time')
    utc = moment.astimezone(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        schedule = db.get(DailyBriefingSchedule, schedule_id_value)
        target = due(schedule, moment) if schedule else None
        if not target:
            return 'NOT_DUE'
        date, scheduled_at = target
        run_id = hashlib.sha256(f'{schedule.id}:{date}'.encode()).hexdigest()[:32]
        row = db.get(DailyBriefingRun, run_id)
        if row is None:
            try:
                db.add(DailyBriefingRun(id=run_id, user_id=schedule.user_id, mailbox_id=schedule.mailbox_id,
                    local_date=date, scheduled_at=scheduled_at, status='PENDING'))
                db.commit()
            except IntegrityError:
                db.rollback()
        lease = secrets.token_hex(16)
        changed = db.execute(update(DailyBriefingRun).where(DailyBriefingRun.id == run_id,
            DailyBriefingRun.status != 'COMPLETE', DailyBriefingRun.attempts < 3,
            or_(DailyBriefingRun.lease_until.is_(None), DailyBriefingRun.lease_until <= utc)).values(
                status='RUNNING', lease=lease, lease_until=utc + timedelta(minutes=10),
                attempts=DailyBriefingRun.attempts + 1)).rowcount
        db.commit()
        if not changed:
            return 'ALREADY_HANDLED'
        try:
            context = context_for(db, schedule, utc)
            document = builder(context, schedule, utc, factory)
            db.expire_all()
            validate_mailbox_context(db, context)
            schedule = db.get(DailyBriefingSchedule, schedule_id_value)
            if not schedule.enabled:
                raise ValueError('DISABLED_DURING_RUN')
            published = db.execute(update(DailyBriefingRun).where(DailyBriefingRun.id == run_id,
                DailyBriefingRun.lease == lease, DailyBriefingRun.status == 'RUNNING').values(
                    status='COMPLETE', document=document, generated_at=utc, failure_code='')).rowcount
            if not published:
                db.rollback(); return 'LEASE_LOST'
            db.add(Reminder(id=run_id, user_id=context.user_id, mailbox_id=context.mailbox_id,
                title='Your private morning briefing is ready.', timezone=schedule.timezone,
                scheduled_at=utc, status='DUE'))
            db.add(PlannerAudit(user_id=context.user_id, mailbox_id=context.mailbox_id,
                resource_id=run_id, event='DAILY_BRIEFING_READY'))
            db.commit()
            return 'COMPLETE'
        except Exception as exc:
            db.rollback()
            code = str(exc) if str(exc) in {'SIGN_IN_REQUIRED', 'DISABLED_DURING_RUN', 'ACCOUNT_CHANGED'} else 'PROVIDER_OR_NETWORK_UNAVAILABLE'
            db.execute(update(DailyBriefingRun).where(DailyBriefingRun.id == run_id,
                DailyBriefingRun.lease == lease).values(status='FAILED', failure_code=code,
                    lease_until=utc + timedelta(minutes=10)))
            db.commit()
            return code


def tick():
    with SessionLocal() as db:
        ids = list(db.scalars(select(DailyBriefingSchedule.id).where(DailyBriefingSchedule.enabled.is_(True))))
    for item in ids:
        run_schedule(item)
