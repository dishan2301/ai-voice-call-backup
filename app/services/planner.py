"""Owned, durable Calendar proposals and recoverable reminder delivery."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update

from app.core.auth import validate_mailbox_context
from app.database.models import PlannerAction, PlannerAudit, Reminder, Email


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def owned(model, context):
    return (model.user_id == context.user_id, model.mailbox_id == context.mailbox_id)


def audit(db, context, resource_id, event):
    db.add(PlannerAudit(user_id=context.user_id, mailbox_id=context.mailbox_id,
                        resource_id=resource_id, event=event))


def instant(value, zone):
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Choose a valid IANA timezone") from exc
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Specify an exact time with timezone offset")
    # Reject nonexistent local times and offsets inconsistent with the named zone.
    local = parsed.astimezone(ZoneInfo(zone))
    if local.replace(tzinfo=None) != parsed.replace(tzinfo=None) or local.utcoffset() != parsed.utcoffset():
        raise ValueError("Time offset does not match the timezone")
    naive = local.replace(tzinfo=None)
    if naive.replace(tzinfo=ZoneInfo(zone), fold=0).utcoffset() != naive.replace(tzinfo=ZoneInfo(zone), fold=1).utcoffset():
        raise ValueError("This local time is ambiguous; choose a time outside the clock change")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def prepare(db, context, session_key, payload, provider=None):
    validate_mailbox_context(db, context)
    invalidate_review(db, context, session_key)
    data = dict(payload)
    operation = data.get("operation")
    if operation not in {"create_event", "edit_event", "cancel_event", "create_reminder", "edit_reminder",
                          "complete_reminder", "cancel_reminder"}:
        raise ValueError("Choose a supported Calendar or reminder operation")
    if operation.endswith("event"):
        if operation != "create_event":
            if not data.get("event_id"):
                raise ValueError("Select an exact Calendar event first")
            current = provider.get(data["event_id"])
            if not current.get("etag") or current.get("status") == "cancelled":
                raise ValueError("The selected event is unavailable")
            # Display current verified details even for cancellation.
            data["etag"] = current["etag"]
            data["before"] = {k: current.get(k) for k in ("id", "summary", "start", "end", "attendees", "location")}
            if operation == "edit_event":
                for key, default in (("attendees", []), ("location", "")):
                    if key not in data:
                        data[key] = current.get(key, default)
        if operation != "cancel_event":
            zone = data.get("timezone", "")
            start = instant(data.get("start_at", ""), zone)
            end = instant(data.get("end_at", ""), zone)
            if end <= start or not str(data.get("summary", "")).strip():
                raise ValueError("Specify title, start and a later end time")
            attendees = data.get("attendees", [])
            if not isinstance(attendees, list) or any(not isinstance(a, dict) or not re.fullmatch(
                    r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", a.get("email", "")) for a in attendees):
                raise ValueError("Provide exact attendee email addresses; names alone are ambiguous")
            data["start"] = {"dateTime": data["start_at"], "timeZone": zone}
            data["end"] = {"dateTime": data["end_at"], "timeZone": zone}
    else:
        if operation != "create_reminder":
            row = db.scalar(select(Reminder).where(Reminder.id == data.get("reminder_id"), *owned(Reminder, context)))
            if not row:
                raise ValueError("Reminder was not found")
            data["before"] = {"title": row.title, "scheduled_at": row.scheduled_at.isoformat(), "status": row.status}
        if operation in {"create_reminder", "edit_reminder"}:
            instant(data.get("scheduled_at", ""), data.get("timezone", ""))
            if not str(data.get("title", "")).strip():
                raise ValueError("What should I remind you about?")
            if data.get("gmail_thread_id") and not db.scalar(select(Email.id).where(
                    Email.mailbox_id == context.mailbox_id, Email.provider == context.provider,
                    Email.thread_id == data["gmail_thread_id"], Email.is_deleted.is_(False))):
                raise ValueError("The linked Gmail thread is not verified in this mailbox")
            if data.get("calendar_event_id"):
                if not provider.get(data["calendar_event_id"]).get("id"):
                    raise ValueError("The linked Calendar event is unavailable")
    fingerprint = digest(json.dumps(data, sort_keys=True, ensure_ascii=False))
    validate_mailbox_context(db, context)
    existing = db.scalar(select(PlannerAction).where(*owned(PlannerAction, context),
        PlannerAction.session_key == session_key, PlannerAction.fingerprint == fingerprint).order_by(PlannerAction.created_at.desc()))
    if existing and existing.status in {"EXECUTING", "DONE", "UNKNOWN"}:
        return {"state": existing.status, "action_id": existing.id, "result": existing.result,
                "answer": "This operation was already handled. Check its status before making a new request."}
    token = secrets.token_urlsafe(32)
    row = existing or PlannerAction(id=secrets.token_hex(16), user_id=context.user_id, mailbox_id=context.mailbox_id,
        session_key=session_key, fingerprint=fingerprint, token_hash=digest(token), payload=data,
        expires_at=datetime.utcnow() + timedelta(minutes=10))
    row.status, row.token_hash = "PREPARED", digest(token)
    row.expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.add(row); audit(db, context, row.id, "PREPARED"); db.commit()
    return {"state": "REVIEW", "action_id": row.id, "confirmation_token": token,
            "details": data, "answer": "Review these exact details, then explicitly confirm this operation."}


def invalidate_review(db, context, session_key):
    db.execute(update(PlannerAction).where(*owned(PlannerAction, context),
        PlannerAction.session_key == session_key, PlannerAction.status == "PREPARED").values(status="SUPERSEDED"))
    db.commit()


def confirm(db, context, session_key, action_id, token, confirmation, provider=None, cancelled=lambda: False):
    validate_mailbox_context(db, context)
    row = db.scalar(select(PlannerAction).where(PlannerAction.id == action_id, *owned(PlannerAction, context),
                                               PlannerAction.session_key == session_key))
    if not row or not hmac.compare_digest(row.token_hash, digest(token)):
        raise ValueError("This reviewed operation is unavailable in this session")
    if row.status != "PREPARED":
        return {"state": row.status, "result": row.result}
    if row.expires_at <= datetime.utcnow() or cancelled():
        raise ValueError("Review expired or interrupted; review the details again")
    if confirmation.casefold().strip() not in {"confirm", "confirm calendar", "confirm reminder", "पुष्टि करें", "haan confirm"}:
        raise ValueError("Explicitly confirm the reviewed operation")
    changed = db.execute(update(PlannerAction).where(PlannerAction.id == row.id,
        PlannerAction.status == "PREPARED", PlannerAction.expires_at > datetime.utcnow()).values(status="EXECUTING")).rowcount
    db.commit()
    if not changed:
        return {"state": "ALREADY_HANDLED"}
    data = row.payload
    try:
        validate_mailbox_context(db, context)
        if cancelled():
            raise ValueError("Operation interrupted")
        if data["operation"].endswith("event"):
            result = provider.mutate(row.id, data)
            if not result.get("id"):
                raise RuntimeError("No Calendar receipt")
        else:
            operation = data["operation"]
            reminder = (Reminder(id=row.id, user_id=context.user_id, mailbox_id=context.mailbox_id)
                        if operation == "create_reminder" else db.scalar(select(Reminder).where(
                            Reminder.id == data["reminder_id"], *owned(Reminder, context))))
            if reminder is None:
                raise ValueError("Reminder unavailable")
            if operation != "create_reminder" and data.get("before") != {
                    "title": reminder.title, "scheduled_at": reminder.scheduled_at.isoformat(), "status": reminder.status}:
                raise ValueError("Reminder changed; review it again")
            if operation in {"create_reminder", "edit_reminder"}:
                reminder.title, reminder.timezone = data["title"], data["timezone"]
                reminder.scheduled_at = instant(data["scheduled_at"], data["timezone"])
                reminder.gmail_thread_id, reminder.calendar_event_id = data.get("gmail_thread_id"), data.get("calendar_event_id")
                reminder.status, reminder.notification_status = "SCHEDULED", "PENDING"
                reminder.attempts, reminder.lease_until, reminder.delivery_token_hash = 0, None, ""
            else:
                reminder.status = "COMPLETED" if operation == "complete_reminder" else "CANCELLED"
            db.add(reminder)
            result = {"id": reminder.id, "status": reminder.status}
        row.status, row.result = "DONE", {k: result.get(k) for k in ("id", "status")}
        audit(db, context, row.id, "DONE"); db.commit()
    except Exception:
        db.rollback()
        db.execute(update(PlannerAction).where(PlannerAction.id == row.id).values(status="UNKNOWN"))
        audit(db, context, row.id, "OUTCOME_UNKNOWN"); db.commit()
        return {"state": "UNKNOWN", "answer": "The outcome could not be confirmed. Check Calendar before retrying; no automatic retry will occur."}
    return {"state": "DONE", "result": row.result, "answer": "The reviewed operation completed."}


def recover_reminders(db, now=None):
    now = now or datetime.utcnow()
    db.execute(update(Reminder).where(Reminder.status == "SCHEDULED", Reminder.scheduled_at <= now).values(status="DUE"))
    db.execute(update(Reminder).where(Reminder.status == "DELIVERING", Reminder.lease_until <= now).values(
        status="DUE", notification_status="UNCONFIRMED"))
    db.commit()


def claim_notifications(db, context, now=None, resource_ids=None):
    validate_mailbox_context(db, context)
    now = now or datetime.utcnow()
    recover_reminders(db, now)
    from app.database.models import DailyBriefingRun
    filters = ([Reminder.id.in_(resource_ids)] if resource_ids is not None else
               [Reminder.id.not_in(select(DailyBriefingRun.id))])
    rows = db.scalars(select(Reminder).where(*owned(Reminder, context), *filters, Reminder.status == "DUE",
                                           Reminder.attempts < 5).order_by(Reminder.scheduled_at).limit(10)).all()
    results = []
    for row in rows:
        token = secrets.token_urlsafe(24)
        changed = db.execute(update(Reminder).where(Reminder.id == row.id, Reminder.status == "DUE").values(
            status="DELIVERING", attempts=Reminder.attempts + 1, lease_until=now + timedelta(minutes=2),
            delivery_token_hash=digest(token))).rowcount
        if changed:
            results.append({"id": row.id, "title": row.title, "scheduled_at": row.scheduled_at.isoformat() + "Z",
                            "delivery_token": token})
            audit(db, context, row.id, "DELIVERY_ATTEMPT")
    db.commit()
    return results


def acknowledge(db, context, reminder_id, token, status):
    validate_mailbox_context(db, context)
    if status not in {"SUBMITTED", "DENIED", "FAILED"}:
        raise ValueError("Unknown notification status")
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, *owned(Reminder, context)))
    if not row or row.status != "DELIVERING" or not hmac.compare_digest(row.delivery_token_hash, digest(token)):
        raise ValueError("Notification attempt is unavailable")
    # A late acknowledgement must not revive a cancelled/completed/edited reminder.
    changed = db.execute(update(Reminder).where(Reminder.id == reminder_id, *owned(Reminder, context),
        Reminder.status == "DELIVERING", Reminder.delivery_token_hash == digest(token)).values(
            notification_status=status, status="NOTIFIED" if status == "SUBMITTED" else "DUE",
            lease_until=datetime.utcnow() + timedelta(minutes=2))).rowcount
    if not changed:
        raise ValueError("Notification attempt changed")
    audit(db, context, row.id, "NOTIFICATION_" + status); db.commit()
    return {"status": status}
