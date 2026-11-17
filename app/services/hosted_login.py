"""Persist a verified provider mailbox and issue its revocable application session."""
from datetime import datetime, timedelta
import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import issue_session_token, token_digest
from app.database.models import AuthSession, MailboxConnection, User, Workspace, WorkspaceMember
from app.services.mailbox_credentials import MailboxCredentialStore


class HostedLoginError(RuntimeError):
    pass


def finish_mailbox_login(db: Session, store: MailboxCredentialStore, *, provider: str,
                         subject: str, email: str, credentials: dict) -> str:
    """Only a provider callback with a verified subject may call this operation."""
    if (provider not in {"gmail", "microsoft"} or not subject or not email
            or len(subject) > 320 or len(email) > 320):
        raise HostedLoginError("Verified mailbox identity is invalid")
    email = email.casefold()
    digest = hashlib.sha256(f"{provider}:{subject}".encode("utf-8")).hexdigest()
    user_id, workspace_id, mailbox_id = "usr:" + digest[:60], "ws:" + digest[:61], "mb:" + digest
    user = db.get(User, user_id)
    conflicting_email = db.scalar(select(User).where(func.lower(User.email) == email, User.id != user_id))
    if conflicting_email or (user and user.status != "ACTIVE"):
        raise HostedLoginError("Mailbox account cannot be connected")
    if user is None:
        user = User(id=user_id, email=email)
        db.add(user)
    else:
        user.email = email
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        db.add(Workspace(id=workspace_id, name="Personal workspace"))
    member = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id))
    if member is None:
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, status="ACTIVE"))
    elif member.status != "ACTIVE":
        raise HostedLoginError("Mailbox account cannot be connected")
    connection = db.get(MailboxConnection, mailbox_id)
    if connection is None:
        connection = MailboxConnection(id=mailbox_id, workspace_id=workspace_id,
                                       owner_user_id=user_id, provider=provider,
                                       provider_account_id=subject, verified_sender=email)
        db.add(connection)
    elif (connection.workspace_id != workspace_id or connection.owner_user_id != user_id
          or connection.provider != provider or connection.provider_account_id != subject):
        raise HostedLoginError("Mailbox ownership does not match the verified identity")
    connection.verified_sender = email
    connection.credential_ciphertext = store.protect(connection, credentials)
    connection.credential_key_version = store.active_version
    connection.status = "ACTIVE"
    connection.revoked_at = None
    token = issue_session_token()
    db.add(AuthSession(id=uuid.uuid4().hex, user_id=user_id, workspace_id=workspace_id,
                       mailbox_id=mailbox_id, token_hash=token_digest(token),
                       expires_at=datetime.utcnow() + timedelta(hours=12)))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HostedLoginError("Mailbox account could not be connected") from exc
    return token
