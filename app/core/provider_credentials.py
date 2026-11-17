"""Encrypted per-mailbox provider credentials with versioned key files."""
import base64
import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext, validate_mailbox_context
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.database.models import AuthSession, MailboxConnection, ProviderCredential


class CredentialError(RuntimeError):
    pass


def _key_path(version: str, settings: Settings) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", version):
        raise CredentialError("Credential key version is invalid")
    directory = Path(settings.credential_key_directory)
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    return directory / f"{version}.key"


def _key(version: str, settings: Settings) -> bytes:
    path = _key_path(version, settings)
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CredentialError(f"Credential key {version} is unavailable") from exc
    if len(value) != 32:
        raise CredentialError(f"Credential key {version} must contain 32 bytes")
    return value


def _aad(mailbox_id: str, capability: str, scopes: list[str], expires_at: datetime | None) -> bytes:
    scope_data = json.dumps(sorted(set(scopes)), separators=(",", ":"))
    expiry_data = expires_at.isoformat() if expires_at else ""
    return f"morning-mail:{mailbox_id}:{capability}:{scope_data}:{expiry_data}:v1".encode()


def _encrypt(mailbox_id: str, capability: str, payload: dict, scopes: list[str],
             expires_at: datetime | None, settings: Settings) -> tuple[str, str]:
    version = settings.credential_active_key_version
    nonce = secrets.token_bytes(12)
    cleartext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    encrypted = AESGCM(_key(version, settings)).encrypt(
        nonce, cleartext, _aad(mailbox_id, capability, scopes, expires_at))
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii"), version


def _decrypt(row: ProviderCredential, settings: Settings) -> dict:
    try:
        packed = base64.urlsafe_b64decode(row.ciphertext.encode("ascii"))
        cleartext = AESGCM(_key(row.key_version, settings)).decrypt(
            packed[:12], packed[12:], _aad(row.mailbox_id, row.capability,
                                          row.scopes_json or [], row.expires_at))
        value = json.loads(cleartext)
    except (ValueError, TypeError, UnicodeError, InvalidTag) as exc:
        raise CredentialError("Stored provider credential is invalid") from exc
    if not isinstance(value, dict):
        raise CredentialError("Stored provider credential is invalid")
    return value


def _owned_connection(db: Session, context: MailboxContext) -> MailboxConnection:
    validate_mailbox_context(db, context)
    connection = db.scalar(select(MailboxConnection).where(
        MailboxConnection.id == context.mailbox_id,
        MailboxConnection.workspace_id == context.workspace_id,
        MailboxConnection.owner_user_id == context.user_id,
        MailboxConnection.status == "ACTIVE",
        MailboxConnection.revoked_at.is_(None),
    ))
    if not connection:
        raise HTTPException(status_code=403, detail="Mailbox ownership is no longer valid")
    return connection


def store_credential(db: Session, context: MailboxContext, capability: str,
                     payload: dict, scopes: list[str], expires_at: datetime | None = None,
                     settings: Settings | None = None) -> ProviderCredential:
    """Replace one READ or SEND credential for the authenticated mailbox."""
    if capability not in {"READ", "SEND", "CALENDAR"} or not isinstance(payload, dict) or not scopes:
        raise ValueError("A credential capability and granted scopes are required")
    settings = settings or get_settings()
    connection = _owned_connection(db, context)
    normalized_scopes = sorted(set(scopes))
    ciphertext, version = _encrypt(connection.id, capability, payload, normalized_scopes,
                                    expires_at, settings)
    row = db.scalar(select(ProviderCredential).where(
        ProviderCredential.mailbox_id == connection.id,
        ProviderCredential.capability == capability,
    ))
    if row is None:
        row = ProviderCredential(mailbox_id=connection.id, capability=capability)
    row.ciphertext = ciphertext
    row.key_version = version
    row.scopes_json = normalized_scopes
    row.expires_at = expires_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def load_credential(db: Session, context: MailboxContext, capability: str,
                    settings: Settings | None = None) -> tuple[dict, list[str]]:
    if capability not in {"READ", "SEND", "CALENDAR"}:
        raise ValueError("Credential capability is invalid")
    settings = settings or get_settings()
    connection = _owned_connection(db, context)
    row = db.scalar(select(ProviderCredential).where(
        ProviderCredential.mailbox_id == connection.id,
        ProviderCredential.capability == capability,
    ))
    if not row:
        raise HTTPException(status_code=409, detail=f"Mailbox {capability.lower()} authorization is missing")
    return _decrypt(row, settings), list(row.scopes_json or [])


def rotate_credential_key(db: Session, context: MailboxContext, capability: str,
                          settings: Settings | None = None) -> None:
    """Re-encrypt one mailbox capability with the configured active key version."""
    payload, scopes = load_credential(db, context, capability, settings)
    row = db.scalar(select(ProviderCredential).where(
        ProviderCredential.mailbox_id == context.mailbox_id,
        ProviderCredential.capability == capability,
    ))
    if row is None:
        raise HTTPException(status_code=409, detail="Mailbox authorization changed during key rotation")
    active = settings or get_settings()
    ciphertext, version = _encrypt(context.mailbox_id, capability, payload, scopes,
                                   row.expires_at, active)
    row.ciphertext, row.key_version = ciphertext, version
    row.scopes_json = scopes
    db.commit()


def disconnect_mailbox(db: Session, context: MailboxContext) -> None:
    """Revoke one mailbox and all its hosted sessions in the same transaction."""
    connection = _owned_connection(db, context)
    now = datetime.utcnow()
    db.execute(delete(ProviderCredential).where(ProviderCredential.mailbox_id == connection.id))
    db.execute(update(AuthSession).where(
        AuthSession.mailbox_id == connection.id,
        AuthSession.revoked_at.is_(None),
    ).values(revoked_at=now))
    connection.status = "DISCONNECTED"
    connection.revoked_at = now
    db.commit()
