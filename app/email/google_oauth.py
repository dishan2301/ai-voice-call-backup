"""Hosted Google web OAuth, separate from the personal installed-app token files."""
from __future__ import annotations

from dataclasses import dataclass
import json

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.id_token import verify_oauth2_token
from google_auth_oauthlib.flow import Flow

from app.core.config import Settings
from app.email.gmail import GMAIL_SCOPES


GOOGLE_SCOPES = ["openid", "email", *GMAIL_SCOPES]


class GoogleOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleMailboxGrant:
    subject: str
    email: str
    credentials: dict


def _flow(settings: Settings, *, state: str | None = None,
          code_verifier: str | None = None) -> Flow:
    config = {"web": {
        "client_id": settings.google_web_client_id,
        "client_secret": settings.google_web_client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [settings.google_web_redirect_uri],
    }}
    return Flow.from_client_config(
        config, scopes=GOOGLE_SCOPES, state=state,
        code_verifier=code_verifier, autogenerate_code_verifier=False,
        redirect_uri=settings.google_web_redirect_uri,
    )


def authorization_url(settings: Settings, state: str, code_verifier: str) -> str:
    flow = _flow(settings, state=state, code_verifier=code_verifier)
    url, returned_state = flow.authorization_url(access_type="offline", prompt="consent")
    if returned_state != state:
        raise GoogleOAuthError("Google authorization state changed")
    return url


def exchange_code(settings: Settings, code: str, code_verifier: str) -> GoogleMailboxGrant:
    flow = _flow(settings, code_verifier=code_verifier)
    flow.fetch_token(code=code, timeout=15)
    token = flow.oauth2session.token
    granted = set(str(token.get("scope", "")).split())
    if not set(GMAIL_SCOPES).issubset(granted):
        raise GoogleOAuthError("Required Google scopes were not granted")
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise GoogleOAuthError("Google offline access was not granted")
    id_token = token.get("id_token")
    if not id_token:
        raise GoogleOAuthError("Google identity token is missing")
    claims = verify_oauth2_token(id_token, Request(), settings.google_web_client_id)
    subject = claims.get("sub")
    email = claims.get("email")
    if (not isinstance(subject, str) or not subject or len(subject) > 320
            or not isinstance(email, str) or not email or len(email) > 320
            or claims.get("email_verified") is not True):
        raise GoogleOAuthError("Google account identity is not verified")
    session = AuthorizedSession(credentials)
    try:
        response = session.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=15)
        response.raise_for_status()
        profile_email = response.json().get("emailAddress", "")
    finally:
        session.close()
    if not isinstance(profile_email, str) or profile_email.casefold() != email.casefold():
        raise GoogleOAuthError("Gmail account does not match the verified Google identity")
    return GoogleMailboxGrant(subject, email.casefold(), {"read": json.loads(credentials.to_json())})
