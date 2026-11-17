"""Request ownership resolution for hosted mailboxes.

Personal mode preserves the existing paired-phone and loopback deployment.
Hosted mode always resolves an individual server-side session, even on loopback.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import LOOPBACK_NAMES, phone_session_token
from app.database.db import get_db
from app.database.models import AuthSession, MailboxConnection, WorkspaceMember, User


@dataclass(frozen=True)
class MailboxContext:
    user_id: str
    workspace_id: str
    mailbox_id: str
    provider: str = "demo"
    session_id: Optional[str] = None
    legacy_local: bool = False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session_token() -> str:
    """Return an opaque token suitable for a one-time authenticated login response."""
    return secrets.token_urlsafe(32)


def validate_mailbox_context(db: Session, context: MailboxContext) -> None:
    """Recheck a captured mailbox owner before delayed work."""
    if context.legacy_local:
        return
    session = (db.scalar(select(AuthSession).where(AuthSession.id == context.session_id)
                         .execution_options(populate_existing=True))
               if context.session_id else None)
    member = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == context.workspace_id,
        WorkspaceMember.user_id == context.user_id,
        WorkspaceMember.status == "ACTIVE",
    ).execution_options(populate_existing=True))
    connection = db.scalar(select(MailboxConnection).where(
        MailboxConnection.id == context.mailbox_id
    ).execution_options(populate_existing=True))
    user = db.scalar(select(User).where(
        User.id == context.user_id
    ).execution_options(populate_existing=True))
    if context.session_id and (not session or session.user_id != context.user_id
                               or session.workspace_id != context.workspace_id
                               or session.mailbox_id != context.mailbox_id
                               or session.revoked_at or session.expires_at <= datetime.utcnow()):
        raise HTTPException(status_code=401, detail="Mailbox session is invalid or expired")
    if (not user or user.status != "ACTIVE" or not member or not connection
            or connection.status != "ACTIVE" or connection.revoked_at
            or connection.workspace_id != context.workspace_id
            or connection.owner_user_id != context.user_id
            or connection.provider != context.provider):
        raise HTTPException(status_code=403, detail="Mailbox ownership is no longer valid")


def _personal_loopback(request: Request) -> bool:
    settings = get_settings()
    host = request.url.hostname or ""
    client = request.client.host if request.client else ""
    return settings.deployment_mode == "personal" and host in LOOPBACK_NAMES and client in LOOPBACK_NAMES


async def require_mailbox_context(request: Request, db: Session = Depends(get_db)) -> MailboxContext:
    """Resolve a server-side session to its single, owned mailbox; fail closed otherwise."""
    settings = get_settings()
    if settings.deployment_mode == "personal":
        supplied = request.headers.get("authorization", "")
        paired = bool(settings.phone_install_code) and hmac.compare_digest(
            request.cookies.get("morning_mail_phone", ""), phone_session_token(settings.phone_install_code))
        bearer = bool(settings.phone_api_token) and hmac.compare_digest(
            supplied, "Bearer " + settings.phone_api_token)
        if not (_personal_loopback(request) or paired or bearer):
            raise HTTPException(status_code=401, detail="Phone pairing is required")
        return MailboxContext("local-development", "local-development",
                              settings.mailbox_id, settings.email_provider, legacy_local=True)
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if not authorization:
        scheme, token = "bearer", request.cookies.get("morning_mail_session", "")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise HTTPException(status_code=401, detail="An authenticated mailbox session is required")
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_digest(token)))
    now = datetime.utcnow()
    if not session or session.revoked_at or session.expires_at <= now:
        raise HTTPException(status_code=401, detail="Mailbox session is invalid or expired")
    member = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == session.workspace_id,
        WorkspaceMember.user_id == session.user_id,
        WorkspaceMember.status == "ACTIVE",
    ))
    connection = db.get(MailboxConnection, session.mailbox_id)
    user = db.get(User, session.user_id)
    if (not user or user.status != "ACTIVE" or not member or not connection
            or connection.status != "ACTIVE" or connection.revoked_at
            or connection.workspace_id != session.workspace_id
            or connection.owner_user_id != session.user_id):
        raise HTTPException(status_code=403, detail="Mailbox ownership is no longer valid")
    return MailboxContext(session.user_id, session.workspace_id, session.mailbox_id,
                          connection.provider, session.id)
