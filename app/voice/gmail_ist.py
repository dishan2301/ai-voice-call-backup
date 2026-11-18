"""Shared calendar ranges and Gmail filters for the continuous SIP tools."""
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parseaddr
import re
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
RANGES = ("today", "yesterday", "last_7_days", "this_week", "last_month", "custom", "all_time")
SCOPES = ("received", "inbox", "sent")
FILTER_SCHEMA = {
    "range": {"type": "STRING", "enum": list(RANGES),
              "description": "Set this whenever the caller names a period, even for unread or sender-filtered mail. Use all_time for an explicitly undated count."},
    "from_date": {"type": "STRING", "description": "Inclusive YYYY-MM-DD in IST for a custom range."},
    "to_date": {"type": "STRING", "description": "Inclusive YYYY-MM-DD in IST for a custom range."},
    "sender": {"type": "STRING", "description": "Sender address or unambiguous sender name."},
    "unread_only": {"type": "BOOLEAN", "description": "Set true for unread mail. Combine with the caller's date range if given."},
    "scope": {"type": "STRING", "enum": list(SCOPES)},
}


def strict_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use YYYY-MM-DD for a custom date.")
    return date.fromisoformat(value)


def date_window(now, range_name, from_date=None, to_date=None):
    today = now.astimezone(IST).date()
    one = timedelta(days=1)
    if range_name not in (None, "custom") and (from_date is not None or to_date is not None):
        raise ValueError("Choose a named range or custom dates.")
    if range_name in (None, "all_time"):
        if from_date is not None or to_date is not None:
            raise ValueError("Use a custom range for explicit dates.")
        return None
    if range_name == "custom":
        start, last = strict_date(from_date), strict_date(to_date)
        if start > last or last == date.max:
            raise ValueError("Choose an end date on or after the start date.")
        end = last + one
    elif range_name == "yesterday":
        start, end = today - one, today
    elif range_name == "last_month":
        end = today.replace(day=1)
        start = (end - one).replace(day=1)
    else:
        first = {"today": today, "last_7_days": today - 6 * one,
                 "this_week": today - today.weekday() * one}
        if range_name not in first:
            raise ValueError("Choose an unambiguous date range.")
        start, end = first[range_name], today + one
    return datetime.combine(start, time.min, IST), datetime.combine(end, time.min, IST)


def spoken_ranges(text):
    """Recognize clear period words in Live's transcript for a consistency check."""
    return {name for name, phrase in {
        "today": r"\btoday\b|\baaj\b|आज|આજે",
        "yesterday": r"\byesterday\b",
        "this_week": r"\bthis week\b",
        "last_month": r"\blast month\b",
        "last_7_days": r"\blast (?:seven|7) days\b",
    }.items() if re.search(phrase, text, re.I)}


def spoken_ist(value):
    if isinstance(value, str):
        if not value:
            return ""
        value = datetime.fromisoformat(value)
    local = value.astimezone(IST)
    return f"{local.day} {local:%B %Y}, {local:%I:%M %p} IST"


def mail_row(row):
    name, address = parseaddr(row.get("sender", ""))
    return {**row, "sender_name": name, "sender_email": address, "date": spoken_ist(row.get("date", ""))}


def gmail_filters(query, *, now, scope, filters):
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError("Use a shorter Gmail search.")
    # Keep dates and scopes out of Gmail free text so a raw operator cannot
    # silently override the server's interpretation of a structured filter.
    if re.search(r"(?i)\b(?:(?:after|before|newer|older|newer_than|older_than|in|label)\s*:|"
                 r"is\s*:\s*(?:unread|read)\b)", query):
        raise ValueError("Use the named date, unread and folder filters.")
    if scope not in SCOPES:
        raise ValueError("Choose received, inbox or sent.")
    unread = filters.get("unread_only", False)
    sender = filters.get("sender", "")
    if type(unread) is not bool or not isinstance(sender, str) or len(sender) > 320:
        raise ValueError("Use a valid sender or unread filter.")
    sender = sender.strip()
    if sender and (not any(ch.isalnum() for ch in sender) or re.search(r'["{}()<>:\\]|[\x00-\x1f]', sender)):
        raise ValueError("Use an exact sender address or plain sender name.")
    bounds = date_window(now, filters.get("range"), filters.get("from_date"), filters.get("to_date"))
    parts = [f"({query.strip()})"] if query.strip() else []
    if scope == "received":
        # Inbox copies can carry SENT too. Archived sent-labelled copies cannot
        # be reliably classified as incoming with Gmail's search labels alone.
        parts.extend(("{-in:sent in:inbox}", "-in:drafts"))
    elif scope == "sent":
        parts.extend(("in:sent", "-in:drafts"))
    if unread:
        parts.append("is:unread")
    if sender:
        parts.append(f'from:"{sender}"')
    context = {"scope": scope, "now_ist": spoken_ist(now)}
    if bounds:
        start, end = bounds
        # Gmail epoch query bounds are inclusive at whole-second precision.
        # Use the final second of the day to keep the next midnight out.
        parts.extend((f"after:{int(start.timestamp())}", f"before:{int(end.timestamp()) - 1}"))
        context["range_ist"] = {"from": start.date().isoformat(),
                                "through": (end.date() - timedelta(days=1)).isoformat()}
    return " ".join(parts), context


def utc_now():
    return datetime.now(timezone.utc)
