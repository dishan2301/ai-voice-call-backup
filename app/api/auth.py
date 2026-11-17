"""Hosted Google mailbox sign-in and revocable application sessions."""
import asyncio
from datetime import datetime
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, require_mailbox_context
from app.core.config import Settings, get_settings
from app.database.db import get_db
from app.database.models import AuthSession
from app.email.google_oauth import GoogleOAuthError, authorization_url, exchange_code
from app.services.hosted_login import HostedLoginError, finish_mailbox_login
from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
from app.services.oauth_transactions import OAuthTransactionError, OAuthTransactions

router = APIRouter(prefix="/auth", tags=["authentication"])


def _google_web_settings(settings: Settings, request: Request) -> None:
    try:
        redirect = urlsplit(settings.google_web_redirect_uri)
    except ValueError as exc:
        raise HTTPException(503, "Hosted Google sign-in is not configured") from exc
    if (settings.deployment_mode != "hosted" or not settings.google_web_client_id
            or not settings.google_web_client_secret or not settings.hosted_google_allowed_emails
            or redirect.scheme != "https" or redirect.hostname not in settings.allowed_hosts
            or redirect.hostname != request.url.hostname
            or redirect.username or redirect.password
            or redirect.path != "/auth/google/callback" or redirect.query or redirect.fragment):
        raise HTTPException(503, "Hosted Google sign-in is not configured")


@router.get("/google/start")
async def start_google_login(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    _google_web_settings(settings, request)
    binding = secrets.token_urlsafe(32)
    store = MailboxCredentialStore(settings.provider_credential_key_directory,
                                   settings.provider_credential_key_version)
    try:
        attempt = OAuthTransactions(store).begin(db, "gmail", settings.google_web_redirect_uri, binding)
        url = authorization_url(settings, attempt.state, attempt.code_verifier)
    except (OAuthTransactionError, MailboxCredentialError, GoogleOAuthError, ValueError) as exc:
        raise HTTPException(503, "Hosted Google sign-in is unavailable") from exc
    response = RedirectResponse(url)
    response.set_cookie("morning_mail_oauth_browser", binding, max_age=600, secure=True,
                        httponly=True, samesite="lax", path="/auth/google")
    return response


@router.get("/google/callback")
async def finish_google_login(request: Request, state: str = "", code: str = "",
                              error: str = "", db: Session = Depends(get_db)):
    settings = get_settings()
    _google_web_settings(settings, request)
    store = MailboxCredentialStore(settings.provider_credential_key_directory,
                                   settings.provider_credential_key_version)
    try:
        attempt = OAuthTransactions(store).claim(
            db, state, "gmail", settings.google_web_redirect_uri,
            request.cookies.get("morning_mail_oauth_browser", ""))
    except OAuthTransactionError as exc:
        raise HTTPException(400, "Google sign-in attempt is invalid or expired") from exc
    if error or not code or len(code) > 4096:
        raise HTTPException(400, "Google sign-in was not completed")
    try:
        grant = await asyncio.to_thread(exchange_code, settings, code, attempt.code_verifier)
    except Exception as exc:
        raise HTTPException(502, "Google sign-in could not be verified") from exc
    if not any(grant.email.casefold() == item.casefold()
               for item in settings.hosted_google_allowed_emails):
        raise HTTPException(403, "This account is not enabled for the pilot")
    try:
        token = finish_mailbox_login(db, store, provider="gmail", subject=grant.subject,
                                     email=grant.email, credentials=grant.credentials)
    except (HostedLoginError, MailboxCredentialError) as exc:
        raise HTTPException(409, "Google mailbox could not be connected") from exc
    response = JSONResponse({"status": "connected", "email": grant.email})
    response.set_cookie("morning_mail_session", token, max_age=43200, secure=True,
                        httponly=True, samesite="strict", path="/")
    response.delete_cookie("morning_mail_oauth_browser", path="/auth/google", secure=True,
                           httponly=True, samesite="lax")
    return response


@router.delete("/session")
async def logout(context: MailboxContext = Depends(require_mailbox_context), db: Session = Depends(get_db)):
    if context.legacy_local:
        raise HTTPException(409, "Personal pairing does not use individual login sessions")
    db.execute(update(AuthSession).where(
        AuthSession.id == context.session_id,
        AuthSession.user_id == context.user_id,
    ).values(revoked_at=datetime.utcnow()))
    db.commit()
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie("morning_mail_session", path="/", secure=True,
                           httponly=True, samesite="strict")
    return response
