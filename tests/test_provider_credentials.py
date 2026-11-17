import pytest
from datetime import datetime, timedelta
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import MailboxContext
from app.core.config import Settings
from app.core.provider_credentials import (
    CredentialError, disconnect_mailbox, load_credential, rotate_credential_key, store_credential,
)
from app.database.db import Base
from app.database.models import AuthSession, MailboxConnection, ProviderCredential, User, Workspace, WorkspaceMember


@pytest.fixture
def credential_store(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add_all([
            User(id="alice", email="alice@example.test"),
            User(id="bob", email="bob@example.test"),
            Workspace(id="workspace", name="Workspace"),
            WorkspaceMember(user_id="alice", workspace_id="workspace"),
            WorkspaceMember(user_id="bob", workspace_id="workspace"),
            MailboxConnection(id="alice-mail", workspace_id="workspace", owner_user_id="alice",
                              provider="gmail", provider_account_id="alice-provider",
                              verified_sender="alice@example.test"),
            MailboxConnection(id="bob-mail", workspace_id="workspace", owner_user_id="bob",
                              provider="gmail", provider_account_id="bob-provider",
                              verified_sender="bob@example.test"),
            AuthSession(id="alice-session", user_id="alice", workspace_id="workspace",
                        mailbox_id="alice-mail", token_hash="a" * 64,
                        expires_at=datetime.utcnow() + timedelta(hours=1)),
        ])
        db.commit()
    (tmp_path / "v1.key").write_bytes(b"1" * 32)
    (tmp_path / "v2.key").write_bytes(b"2" * 32)
    settings = Settings(credential_key_directory=str(tmp_path), credential_active_key_version="v1")
    return engine, sessions, settings


def test_credentials_are_encrypted_scoped_by_mailbox_and_separated_by_capability(credential_store):
    engine, sessions, settings = credential_store
    alice = MailboxContext("alice", "workspace", "alice-mail", "gmail")
    bob = MailboxContext("bob", "workspace", "bob-mail", "gmail")
    with sessions() as db:
        row = store_credential(db, alice, "READ", {"refresh_token": "alice-secret"},
                               ["gmail.readonly"], settings=settings)
        assert "alice-secret" not in row.ciphertext
        store_credential(db, alice, "SEND", {"refresh_token": "send-secret"},
                         ["gmail.send"], settings=settings)
        assert load_credential(db, alice, "READ", settings) == (
            {"refresh_token": "alice-secret"}, ["gmail.readonly"])
        assert load_credential(db, alice, "SEND", settings) == (
            {"refresh_token": "send-secret"}, ["gmail.send"])
        with pytest.raises(HTTPException) as error:
            load_credential(db, bob, "READ", settings)
        assert error.value.status_code == 409
        foreign = MailboxContext("bob", "workspace", "alice-mail", "gmail")
        with pytest.raises(HTTPException) as error:
            load_credential(db, foreign, "READ", settings)
        assert error.value.status_code == 403
    engine.dispose()


def test_credential_key_rotation_reencrypts_without_changing_payload(credential_store):
    _, sessions, settings = credential_store
    owner = MailboxContext("alice", "workspace", "alice-mail", "gmail")
    with sessions() as db:
        original = store_credential(db, owner, "READ", {"refresh_token": "rotate-me"},
                                    ["gmail.readonly"], settings=settings)
        old_ciphertext = original.ciphertext
        settings = settings.model_copy(update={"credential_active_key_version": "v2"})
        rotate_credential_key(db, owner, "READ", settings)
        rotated = load_credential(db, owner, "READ", settings)
        row = db.get(type(original), original.id)
        assert rotated == ({"refresh_token": "rotate-me"}, ["gmail.readonly"])
        assert row.key_version == "v2"
        assert row.ciphertext != old_ciphertext


def test_credential_scope_metadata_is_authenticated(credential_store):
    _, sessions, settings = credential_store
    owner = MailboxContext("alice", "workspace", "alice-mail", "gmail")
    with sessions() as db:
        row = store_credential(db, owner, "READ", {"refresh_token": "integrity"},
                               ["gmail.readonly"], settings=settings)
        row.scopes_json = ["gmail.send"]
        db.commit()
        with pytest.raises(CredentialError, match="invalid"):
            load_credential(db, owner, "READ", settings)


def test_disconnect_deletes_provider_grants_and_revokes_mailbox_sessions(credential_store):
    _, sessions, settings = credential_store
    owner = MailboxContext("alice", "workspace", "alice-mail", "gmail")
    with sessions() as db:
        store_credential(db, owner, "READ", {"refresh_token": "revoke-me"},
                         ["gmail.readonly"], settings=settings)
        disconnect_mailbox(db, owner)
        assert db.query(ProviderCredential).filter_by(mailbox_id="alice-mail").count() == 0
        assert db.get(AuthSession, "alice-session").revoked_at is not None
        with pytest.raises(HTTPException) as error:
            load_credential(db, owner, "READ", settings)
        assert error.value.status_code == 403


def test_hosted_gmail_client_uses_only_mailbox_grants(credential_store, monkeypatch):
    from app.api.dependencies import _hosted_gmail_provider, create_mail_agent
    from app.api import dependencies
    from app.email import gmail

    _, sessions, settings = credential_store
    settings = settings.model_copy(update={"gemini_api_key": "test-key"})
    monkeypatch.setattr(dependencies, "get_runtime_settings", lambda: settings)
    owner = MailboxContext("alice", "workspace", "alice-mail", "gmail")
    read_info = {"client_id": "client", "client_secret": "secret", "refresh_token": "read",
                 "token": "read-access", "token_uri": "https://oauth2.googleapis.com/token"}
    send_info = {**read_info, "refresh_token": "send", "token": "send-access"}
    bob_info = {**read_info, "refresh_token": "bob-read", "token": "bob-access"}
    with sessions() as db:
        store_credential(db, owner, "READ", read_info, [gmail.GMAIL_SCOPES[0]], settings=settings)
        store_credential(db, owner, "SEND", send_info, [gmail.GMAIL_SEND_SCOPE], settings=settings)
        store_credential(db, MailboxContext("bob", "workspace", "bob-mail", "gmail"),
                         "READ", bob_info, [gmail.GMAIL_SCOPES[0]], settings=settings)
        builds = []

        class Service:
            def __init__(self, http):
                self._http = http

        def build(*args, **kwargs):
            builds.append((args, kwargs))
            return Service(kwargs["http"])

        monkeypatch.setattr(gmail, "build", build)
        provider = _hosted_gmail_provider(owner, settings, db)
        alice_agent = create_mail_agent(owner, db)
        bob_agent = create_mail_agent(MailboxContext("bob", "workspace", "bob-mail", "gmail"), db)

    assert provider.service._http.credentials.has_scopes(gmail.GMAIL_SCOPES)
    assert not provider.service._http.credentials.has_scopes([gmail.GMAIL_SEND_SCOPE])
    assert provider.send_service._http.credentials.has_scopes([gmail.GMAIL_SEND_SCOPE])
    assert alice_agent.settings.mailbox_id == "alice-mail"
    assert bob_agent.settings.mailbox_id == "bob-mail"
    assert alice_agent.gmail.service._http.credentials.token == "read-access"
    assert bob_agent.gmail.service._http.credentials.token == "bob-access"
    assert len(builds) == 5
    assert all(call[1]["cache_discovery"] is False for call in builds)


def test_hosted_gmail_client_rejects_unrequested_scopes():
    from app.core.config import Settings
    from app.email.gmail import GmailConfigurationError, GmailProvider, GMAIL_SCOPES, GMAIL_SEND_SCOPE

    settings = Settings()
    with pytest.raises(GmailConfigurationError, match="READ authorization"):
        GmailProvider.from_authorized_user_info({}, [*GMAIL_SCOPES, "https://www.googleapis.com/auth/gmail.modify"],
                                                settings)
    read_info = {"client_id": "client", "client_secret": "secret", "refresh_token": "read",
                 "token": "access", "token_uri": "https://oauth2.googleapis.com/token"}
    with pytest.raises(GmailConfigurationError, match="SEND authorization"):
        GmailProvider.from_authorized_user_info(read_info, GMAIL_SCOPES, settings,
                                                send_info=read_info,
                                                send_scopes=[GMAIL_SEND_SCOPE,
                                                             "https://www.googleapis.com/auth/gmail.modify"])
