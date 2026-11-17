"""Replay-resistant server-side OAuth transaction state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit
import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database.models import OAuthTransaction
from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore


class OAuthTransactionError(RuntimeError):
    """An OAuth attempt is invalid, expired, mismatched, or already consumed."""


@dataclass(frozen=True)
class OAuthAttempt:
    state: str
    code_verifier: str
    transaction_id: str


@dataclass(frozen=True)
class ClaimedOAuthAttempt:
    code_verifier: str
    transaction_id: str


class OAuthTransactions:
    """Create and atomically consume short-lived, browser-bound OAuth state."""

    def __init__(self, credentials: MailboxCredentialStore, lifetime_seconds: int = 600):
        if not 60 <= lifetime_seconds <= 1800:
            raise ValueError("OAuth transaction lifetime must be 60–1800 seconds")
        self.credentials = credentials
        self.lifetime_seconds = lifetime_seconds

    @staticmethod
    def _associated_data(transaction_id: str, provider: str) -> bytes:
        return f"oauth-pkce-v1:{transaction_id}:{provider}".encode("ascii")

    @staticmethod
    def _validate(provider: str, redirect_uri: str, browser_binding: str) -> None:
        parsed = urlsplit(redirect_uri)
        if (provider not in {"gmail", "microsoft", "calendar"} or parsed.scheme != "https" or not parsed.netloc
                or parsed.username or parsed.password or parsed.fragment or len(redirect_uri) > 2048):
            raise OAuthTransactionError("OAuth transaction parameters are invalid")
        if not browser_binding or len(browser_binding) > 256:
            raise OAuthTransactionError("OAuth browser binding is invalid")

    def begin(self, db: Session, provider: str, redirect_uri: str,
              browser_binding: str) -> OAuthAttempt:
        self._validate(provider, redirect_uri, browser_binding)
        transaction_id = uuid.uuid4().hex
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        aad = self._associated_data(transaction_id, provider)
        try:
            encrypted_verifier = self.credentials.seal(verifier.encode("ascii"), aad)
        except MailboxCredentialError as exc:
            raise OAuthTransactionError("OAuth transaction could not be secured") from exc
        db.add(OAuthTransaction(
            id=transaction_id,
            state_hash=hashlib.sha256(state.encode("ascii")).hexdigest(),
            browser_binding_hash=hashlib.sha256(browser_binding.encode("utf-8")).hexdigest(),
            provider=provider,
            redirect_uri=redirect_uri,
            code_verifier_ciphertext=encrypted_verifier,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=self.lifetime_seconds),
        ))
        db.commit()
        return OAuthAttempt(state, verifier, transaction_id)

    def claim(self, db: Session, state: str, provider: str, redirect_uri: str,
              browser_binding: str) -> ClaimedOAuthAttempt:
        self._validate(provider, redirect_uri, browser_binding)
        if not state or len(state) > 256:
            raise OAuthTransactionError("OAuth transaction is invalid or already used")
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        row = db.query(OAuthTransaction).filter_by(state_hash=state_hash).one_or_none()
        binding_hash = hashlib.sha256(browser_binding.encode("utf-8")).hexdigest()
        now = datetime.utcnow()
        if (not row or row.provider != provider or row.redirect_uri != redirect_uri
                or not hmac.compare_digest(row.browser_binding_hash, binding_hash)
                or row.consumed_at is not None or row.expires_at <= now):
            raise OAuthTransactionError("OAuth transaction is invalid or already used")
        claimed = db.execute(update(OAuthTransaction).where(
            OAuthTransaction.id == row.id,
            OAuthTransaction.state_hash == state_hash,
            OAuthTransaction.browser_binding_hash == binding_hash,
            OAuthTransaction.provider == provider,
            OAuthTransaction.redirect_uri == redirect_uri,
            OAuthTransaction.consumed_at.is_(None),
            OAuthTransaction.expires_at > now,
        ).values(consumed_at=now), execution_options={"synchronize_session": False})
        if claimed.rowcount != 1:
            db.rollback()
            raise OAuthTransactionError("OAuth transaction is invalid or already used")
        verifier_ciphertext = row.code_verifier_ciphertext
        transaction_id = row.id
        db.commit()
        try:
            verifier = self.credentials.unseal(
                verifier_ciphertext, self._associated_data(transaction_id, provider)).decode("ascii")
        except (MailboxCredentialError, UnicodeDecodeError) as exc:
            raise OAuthTransactionError("OAuth transaction is invalid or already used") from exc
        if len(verifier) < 43 or len(verifier) > 128:
            raise OAuthTransactionError("OAuth transaction is invalid or already used")
        return ClaimedOAuthAttempt(verifier, transaction_id)
