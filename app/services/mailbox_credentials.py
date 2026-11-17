"""Encrypted, ownership-bound provider credentials for mailbox connections."""
from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.auth import MailboxContext
from app.core.config import PROJECT_ROOT
from app.database.models import AuthSession, MailboxConnection, User, WorkspaceMember


class MailboxCredentialError(RuntimeError):
    """A credential cannot safely be read or changed."""


class MailboxCredentialStore:
    """Use versioned 256-bit keys from a private directory; never create or print keys."""

    def __init__(self, key_directory: str | Path, active_version: int):
        self.key_directory = Path(key_directory)
        if not self.key_directory.is_absolute():
            self.key_directory = PROJECT_ROOT / self.key_directory
        if not 1 <= int(active_version) <= 999999:
            raise ValueError("Invalid credential key version")
        self.active_version = int(active_version)

    def _key(self, version: int) -> bytes:
        path = self.key_directory / (str(version) + ".key")
        try:
            directory_stat = self.key_directory.stat()
            if (not self.key_directory.is_dir() or directory_stat.st_mode & 0o077
                    or (hasattr(os, "geteuid") and directory_stat.st_uid != os.geteuid())):
                raise MailboxCredentialError("Credential key directory permissions are unsafe")
            if path.is_symlink():
                raise MailboxCredentialError("Credential key file is unavailable")
            stat = path.stat()
            if stat.st_mode & 0o077 or (hasattr(os, "geteuid") and stat.st_uid != os.geteuid()):
                raise MailboxCredentialError("Credential key file permissions are unsafe")
            key = path.read_bytes()
        except OSError as exc:
            raise MailboxCredentialError("Credential key file is unavailable") from exc
        if len(key) != 32:
            raise MailboxCredentialError("Credential key must contain 32 bytes")
        return key

    def seal(self, value: bytes, associated_data: bytes) -> str:
        """Encrypt short-lived secret material with the same versioned keyring."""
        if not isinstance(value, bytes) or len(value) > 65536 or not associated_data:
            raise MailboxCredentialError("Secret material is invalid")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key(self.active_version)).encrypt(nonce, value, associated_data)
        return "v%d.%s" % (self.active_version,
                            base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii"))

    def unseal(self, encoded: str, associated_data: bytes) -> bytes:
        try:
            prefix, value = encoded.split(".", 1)
            version = int(prefix[1:]) if prefix.startswith("v") else 0
            if not 1 <= version <= 999999 or not value:
                raise ValueError
            packed = base64.b64decode(value, altchars=b"-_", validate=True)
            if len(packed) < 29:
                raise ValueError
            return AESGCM(self._key(version)).decrypt(packed[:12], packed[12:], associated_data)
        except MailboxCredentialError:
            raise
        except Exception as exc:
            raise MailboxCredentialError("Secret material is unavailable") from exc

    @staticmethod
    def _aad(connection: MailboxConnection) -> bytes:
        identity = [connection.id, connection.workspace_id, connection.owner_user_id,
                    connection.provider, connection.provider_account_id, connection.verified_sender]
        return json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def _encode(credentials: Mapping[str, Mapping]) -> bytes:
        if not isinstance(credentials, Mapping) or not credentials or set(credentials) - {"read", "send"}:
            raise MailboxCredentialError("Credentials must contain read and/or send grants")
        if "read" not in credentials or any(not isinstance(value, Mapping) for value in credentials.values()):
            raise MailboxCredentialError("A read grant and valid credential objects are required")
        try:
            raw = json.dumps(credentials, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError) as exc:
            raise MailboxCredentialError("Credential bundle is invalid") from exc
        if len(raw) > 65536:
            raise MailboxCredentialError("Credential bundle is too large")
        return raw

    @staticmethod
    def _decode(raw: bytes) -> dict:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise MailboxCredentialError("Credential bundle is invalid") from exc
        if (not isinstance(value, dict) or "read" not in value or set(value) - {"read", "send"}
                or any(not isinstance(item, dict) for item in value.values())):
            raise MailboxCredentialError("Credential bundle is invalid")
        return value

    @staticmethod
    def _owned(db: Session, context: MailboxContext, connection_id: str) -> MailboxConnection:
        if context.legacy_local or not context.session_id:
            raise MailboxCredentialError("Per-mailbox credentials require a hosted session")
        now = datetime.utcnow()
        session = db.scalar(select(AuthSession).where(
            AuthSession.id == context.session_id, AuthSession.user_id == context.user_id,
            AuthSession.workspace_id == context.workspace_id,
            AuthSession.mailbox_id == context.mailbox_id, AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        ))
        member = db.scalar(select(WorkspaceMember).where(
            WorkspaceMember.user_id == context.user_id,
            WorkspaceMember.workspace_id == context.workspace_id,
            WorkspaceMember.status == "ACTIVE",
        ))
        user = db.get(User, context.user_id)
        connection = db.scalar(select(MailboxConnection).where(
            MailboxConnection.id == connection_id, MailboxConnection.id == context.mailbox_id,
            MailboxConnection.workspace_id == context.workspace_id,
            MailboxConnection.owner_user_id == context.user_id,
            MailboxConnection.provider == context.provider, MailboxConnection.status == "ACTIVE",
            MailboxConnection.revoked_at.is_(None),
        ))
        if not session or not member or not user or user.status != "ACTIVE" or not connection:
            raise MailboxCredentialError("Mailbox credential is unavailable")
        return connection

    def put(self, db: Session, context: MailboxContext, connection_id: str,
            credentials: Mapping[str, Mapping], *, expected_ciphertext: str | None = None) -> None:
        connection = self._owned(db, context, connection_id)
        old_ciphertext, old_version = connection.credential_ciphertext, connection.credential_key_version
        if expected_ciphertext is not None and old_ciphertext != expected_ciphertext:
            raise MailboxCredentialError("Mailbox credential changed; retry the operation")
        encoded = self.protect(connection, credentials)
        changed = db.execute(update(MailboxConnection).where(
            MailboxConnection.id == connection.id,
            MailboxConnection.owner_user_id == context.user_id,
            MailboxConnection.workspace_id == context.workspace_id,
            MailboxConnection.status == "ACTIVE", MailboxConnection.revoked_at.is_(None),
            MailboxConnection.credential_ciphertext == old_ciphertext,
            MailboxConnection.credential_key_version == old_version,
        ).values(credential_ciphertext=encoded, credential_key_version=self.active_version),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            db.rollback()
            raise MailboxCredentialError("Mailbox credential changed; retry the operation")
        db.commit()

    def protect(self, connection: MailboxConnection, credentials: Mapping[str, Mapping]) -> str:
        """Seal a verified connection's grant for transactional onboarding."""
        return self.seal(self._encode(credentials), self._aad(connection))

    def get(self, db: Session, context: MailboxContext, connection_id: str) -> dict:
        connection = self._owned(db, context, connection_id)
        prefix = connection.credential_ciphertext.partition(".")[0]
        try:
            version = int(prefix[1:]) if prefix.startswith("v") else 0
        except ValueError:
            version = 0
        if version != connection.credential_key_version:
            raise MailboxCredentialError("Mailbox credential is unavailable")
        raw = self.unseal(connection.credential_ciphertext, self._aad(connection))
        return self._decode(raw)

    def rotate(self, db: Session, context: MailboxContext, connection_id: str) -> None:
        self.put(db, context, connection_id, self.get(db, context, connection_id))

    def disconnect(self, db: Session, context: MailboxContext, connection_id: str) -> None:
        connection = self._owned(db, context, connection_id)
        db.execute(update(MailboxConnection).where(
            MailboxConnection.id == connection.id,
            MailboxConnection.owner_user_id == context.user_id,
            MailboxConnection.status == "ACTIVE",
        ).values(status="REVOKED", revoked_at=datetime.utcnow(), credential_ciphertext=""))
        db.execute(update(AuthSession).where(
            AuthSession.mailbox_id == connection_id, AuthSession.workspace_id == context.workspace_id,
            AuthSession.user_id == context.user_id, AuthSession.revoked_at.is_(None),
        ).values(revoked_at=datetime.utcnow()))
        db.commit()
