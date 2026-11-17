"""Additional Calendar consent, bound to the existing authenticated Google owner."""
import json
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel, Field
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, require_mailbox_context, validate_mailbox_context
from app.core.config import get_settings
from app.core.provider_credentials import store_credential
from app.database.db import get_db
from app.database.models import MailboxConnection
from app.services.calendar_provider import CALENDAR_SCOPES
from app.services.mailbox_credentials import MailboxCredentialStore
from app.services.oauth_transactions import OAuthTransactions

router = APIRouter(prefix="/auth/calendar", tags=["calendar consent"])


def setup(request, context):
    settings = get_settings()
    redirect = urlsplit(settings.google_calendar_redirect_uri)
    if (context.legacy_local or redirect.scheme != "https" or redirect.hostname != request.url.hostname
            or redirect.path != "/auth/calendar/callback" or redirect.query or redirect.fragment
            or redirect.username or not settings.google_web_client_id or not settings.google_web_client_secret):
        raise HTTPException(503, "Hosted Calendar consent is not configured; personal mode uses --enable-calendar")
    store = MailboxCredentialStore(settings.provider_credential_key_directory, settings.provider_credential_key_version)
    return settings, OAuthTransactions(store)


def flow(settings, state=None, verifier=None):
    return Flow.from_client_config({"web": {"client_id": settings.google_web_client_id,
        "client_secret": settings.google_web_client_secret, "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"}},
        scopes=["openid", "email", *CALENDAR_SCOPES], state=state, code_verifier=verifier,
        autogenerate_code_verifier=False, redirect_uri=settings.google_calendar_redirect_uri)


@router.get("/start")
def start(request: Request, context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    settings, transactions = setup(request, context)
    binding = secrets.token_urlsafe(32)
    attempt = transactions.begin(db, "calendar", settings.google_calendar_redirect_uri,
                                 binding + ":" + context.session_id)
    url, state = flow(settings, attempt.state, attempt.code_verifier).authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false")
    if state != attempt.state:
        raise HTTPException(503, "Calendar consent state mismatch")
    response = RedirectResponse(url)
    response.set_cookie("calendar_oauth", binding, max_age=600, secure=True, httponly=True,
                        samesite="lax", path="/auth/calendar")
    return response


@router.get("/callback")
def callback_landing():
    # First-party fetch allows the existing Strict login cookie to remain Strict.
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>Calendar consent</title>'
                        '<p id="status">Verifying Calendar consent…</p>'
                        '<script src="/static/calendar-consent.js"></script>')


class Finish(BaseModel):
    code: str = Field(default="", max_length=4096)
    state: str = Field(default="", max_length=256)
    error: str = Field(default="", max_length=256)


@router.post("/finish")
def finish(request: Request, payload: Finish,
           context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    return callback(request, payload.code, payload.state, payload.error, context, db)


def callback(request: Request, code: str = "", state: str = "", error: str = "",
             context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    settings, transactions = setup(request, context)
    try:
        attempt = transactions.claim(db, state, "calendar", settings.google_calendar_redirect_uri,
                                      request.cookies.get("calendar_oauth", "") + ":" + context.session_id)
        if error or not code or len(code) > 4096:
            raise ValueError("Consent incomplete")
        consent = flow(settings, verifier=attempt.code_verifier)
        consent.fetch_token(code=code, timeout=15)
        claims = verify_oauth2_token(consent.oauth2session.token.get("id_token", ""), GoogleRequest(),
                                     settings.google_web_client_id)
        validate_mailbox_context(db, context)
        connection = db.get(MailboxConnection, context.mailbox_id)
        if (claims.get("sub") != connection.provider_account_id or claims.get("email_verified") is not True
                or claims.get("email", "").casefold() != connection.verified_sender.casefold()):
            raise ValueError("Calendar identity differs from Gmail")
        scopes = consent.oauth2session.token.get("scope", "")
        scopes = scopes.split() if isinstance(scopes, str) else scopes
        if not set(CALENDAR_SCOPES).issubset(scopes) or not consent.credentials.refresh_token:
            raise ValueError("Calendar permission missing")
        store_credential(db, context, "CALENDAR", json.loads(consent.credentials.to_json()), scopes, settings=settings)
    except Exception as exc:
        raise HTTPException(409, "Calendar consent failed verification. Gmail credentials are unchanged.") from exc
    return {"status": "calendar_connected"}
