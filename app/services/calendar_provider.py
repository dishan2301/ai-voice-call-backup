"""Separate Google Calendar capability. No Gmail mutation or automatic retries."""
import json
from pathlib import Path

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from app.core.auth import validate_mailbox_context
from app.core.config import PROJECT_ROOT
from app.core.provider_credentials import load_credential, store_credential
from app.database.models import MailboxConnection

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events",
                   "https://www.googleapis.com/auth/calendar.freebusy",
                   "https://www.googleapis.com/auth/calendar.calendars.readonly"]


class CalendarProvider:
    def __init__(self, service):
        self.service = service

    def read(self, start, end, query="", busy=False):
        if busy:
            result = self.service.freebusy().query(body={"timeMin": start, "timeMax": end,
                                                        "items": [{"id": "primary"}]}).execute(num_retries=0)
            if ("primary" not in result.get("calendars", {})
                    or result["calendars"]["primary"].get("errors")):
                raise ValueError("Calendar availability could not be verified")
            return result
        return self.service.events().list(calendarId="primary", timeMin=start, timeMax=end,
            q=query, singleEvents=True, orderBy="startTime", maxResults=50).execute(num_retries=0)

    def get(self, event_id):
        return self.service.events().get(calendarId="primary", eventId=event_id).execute(num_retries=0)

    def mutate(self, action_id, payload):
        events = self.service.events()
        operation = payload["operation"]
        body = {key: payload[key] for key in ("summary", "start", "end", "attendees", "location") if key in payload}
        if operation == "create_event":
            # Google accepts caller-supplied base32hex IDs; stable across reconnects.
            return events.insert(calendarId="primary", body={**body, "id": action_id},
                                 sendUpdates="none").execute(num_retries=0)
        current = self.get(payload["event_id"])
        if current.get("etag") != payload["etag"]:
            raise ValueError("Event changed; review the current details again")
        request = (events.delete(calendarId="primary", eventId=payload["event_id"], sendUpdates="none")
                   if operation == "cancel_event" else events.patch(
                       calendarId="primary", eventId=payload["event_id"], body=body, sendUpdates="none"))
        request.headers["If-Match"] = payload["etag"]
        result = request.execute(num_retries=0)
        return result or {"id": payload["event_id"], "status": "cancelled"}


def calendar_provider(db, context, settings):
    validate_mailbox_context(db, context)
    path = None
    if context.legacy_local:
        path = Path(settings.google_calendar_token_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.resolve() in {(PROJECT_ROOT / settings.google_token_file).resolve(),
                               (PROJECT_ROOT / settings.google_send_token_file).resolve()}:
            raise ValueError("Calendar requires a separate token file")
        info = json.loads(path.read_text())
        scopes = info.get("scopes", [])
    else:
        info, scopes = load_credential(db, context, "CALENDAR", settings)
    if not set(CALENDAR_SCOPES).issubset(set(scopes)):
        raise ValueError("Calendar authorization is missing required scopes")
    credentials = Credentials.from_authorized_user_info(info)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        validate_mailbox_context(db, context)
        if path:
            path.write_text(credentials.to_json()); path.chmod(0o600)
        else:
            store_credential(db, context, "CALENDAR", json.loads(credentials.to_json()), scopes, settings=settings)
    if not credentials.valid:
        raise ValueError("Calendar authorization is unavailable")
    service = build("calendar", "v3", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=15)),
                    cache_discovery=False)
    primary = service.calendars().get(calendarId="primary").execute(num_retries=0)
    if context.legacy_local:
        from app.email.gmail import create_gmail_service
        identity = create_gmail_service(settings).users().getProfile(userId="me").execute(num_retries=0)["emailAddress"]
    else:
        identity = db.get(MailboxConnection, context.mailbox_id).verified_sender
    if primary.get("id", "").casefold() != identity.casefold():
        raise ValueError("Calendar account does not match the mailbox")
    return CalendarProvider(service)
