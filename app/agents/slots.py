"""Validate explicit mail date windows; search language is interpreted by the model."""
import re
from datetime import datetime, timedelta


def search_query(query, request, timezone, now=None):
    current = (now or datetime.now(timezone)).astimezone(timezone)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    text = request.lower()
    start = end = None
    if re.search(r"\b(today|aaj|aje)\b", text):
        start, end = today, today + timedelta(days=1)
    elif re.search(r"\byesterday\b", text):
        start, end = today - timedelta(days=1), today
    elif "this week" in text:
        start, end = today - timedelta(days=today.weekday()), current
    elif "last week" in text:
        end = today - timedelta(days=today.weekday())
        start = end - timedelta(days=7)
    else:
        date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
        if date:
            start = datetime.strptime(date.group(), "%Y-%m-%d").replace(tzinfo=timezone)
            end = start + timedelta(days=1)
    date_filter = r"\b(?:after|before|newer|older|newer_than|older_than):\S+"
    count_received = bool(re.search(r"\b(?:how\s+many|count|number\s+of)\b", text)
                          and re.search(r"\b(?:mail|mails|email|emails|received|arrived)\b", text)
                          and re.search(r"\b(?:today|aaj|aje|yesterday|this\s+week|last\s+week)\b", text))
    if not re.search(date_filter, query, re.I) and not count_received:
        # An event date mentioned in an email is not its received date. Do not
        # inject a received-date restriction the search planner did not request.
        return query.strip()
    if start is None:
        raise ValueError("Choose an exact day or time window before filtering by date.")
    query = re.sub(date_filter, "", query, flags=re.I).strip()
    if start:
        query += f" after:{int(start.timestamp())} before:{int(end.timestamp())}"
    return query
