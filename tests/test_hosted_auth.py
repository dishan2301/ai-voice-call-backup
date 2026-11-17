"""Hosted ownership must not fall back to a configured/default mailbox."""
import asyncio
from datetime import datetime, timedelta
import pytest

from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import auth
from app.core.auth import MailboxContext, token_digest
from app.core.config import Settings
from app.api import phone
from app.database.db import Base, get_db
from app.database.models import AuthSession, Commitment, Email, MailboxConnection, User, Workspace, WorkspaceMember
from app.main import app


def _owned_mailbox(db, *, user_id, workspace_id, mailbox_id, token):
    db.add_all([
        User(id=user_id, email=f"{user_id}@example.test"),
        Workspace(id=workspace_id, name=workspace_id),
        WorkspaceMember(user_id=user_id, workspace_id=workspace_id),
        MailboxConnection(id=mailbox_id, workspace_id=workspace_id, owner_user_id=user_id,
                          provider="demo", provider_account_id=f"provider-{user_id}",
                          verified_sender=f"{user_id}@example.test"),
        AuthSession(id=f"session-{user_id}", user_id=user_id, workspace_id=workspace_id,
                    mailbox_id=mailbox_id, token_hash=token_digest(token),
                    expires_at=datetime.utcnow() + timedelta(hours=1)),
    ])


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    # Hosted refresh executes sync work in a worker while auth holds its request
    # session. A single-connection in-memory StaticPool deadlocks that test
    # shape; file-backed WAL permits the real concurrent-session behavior.
    engine = create_engine(f"sqlite:///{tmp_path / 'hosted.db'}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        _owned_mailbox(db, user_id="alice", workspace_id="acme", mailbox_id="alice-mail", token="alice-token")
        _owned_mailbox(db, user_id="bob", workspace_id="beta", mailbox_id="bob-mail", token="bob-token")
        db.add_all([
            Email(provider="demo", mailbox_id="alice-mail", provider_message_id="alice-1",
                  thread_id="alice-thread", subject="Alice private", received_at=datetime.utcnow(), body_hash="a"),
            Email(provider="demo", mailbox_id="bob-mail", provider_message_id="bob-1",
                  thread_id="bob-thread", subject="Bob private", received_at=datetime.utcnow(), body_hash="b"),
        ])
        db.commit()
        for row in db.query(Email).all():
            db.add(Commitment(email_id=row.id, direction="YOU_OWE", source_text=row.subject))
        db.commit()

    async def override_db():
        with sessions() as db:
            yield db

    # Global provider deliberately differs from each owned connection.
    settings = Settings(app_env="production", deployment_mode="hosted", email_provider="gmail",
                        allowed_hosts=["test", "hosted.example.test"])
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(phone, "get_settings", lambda: settings)
    monkeypatch.setattr("app.core.security.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.dependencies.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        yield sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


@pytest.mark.parametrize("base_url", ["http://test", "https://hosted.example.test"])
@pytest.mark.parametrize("endpoint,key", [("/phone/recent", "emails"), ("/phone/commitments", "commitments")])
def test_hosted_reads_require_owned_server_session(hosted, base_url, endpoint, key):
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
            missing = await client.get(endpoint)
            alice = await client.get(endpoint, params={"mailbox_id": "bob-mail"},
                                     headers={"Authorization": "Bearer alice-token"})
            bob = await client.get(endpoint, headers={"Authorization": "Bearer bob-token"})
        return missing, alice, bob

    missing, alice, bob = asyncio.run(exercise())
    assert missing.status_code == 401
    assert alice.status_code == bob.status_code == 200
    assert [row["subject"] for row in alice.json()[key]] == ["Alice private"]
    assert [row["subject"] for row in bob.json()[key]] == ["Bob private"]


@pytest.mark.parametrize("change", ["expired", "revoked", "disabled_user", "removed_member",
                                    "foreign_workspace", "foreign_mailbox", "disconnected", "revoked_connection"])
def test_hosted_session_rechecks_current_ownership(hosted, change):
    with hosted() as db:
        session = db.get(AuthSession, "session-alice")
        if change == "expired":
            session.expires_at = datetime.utcnow() - timedelta(seconds=1)
        elif change == "revoked":
            session.revoked_at = datetime.utcnow()
        elif change == "disabled_user":
            db.get(User, "alice").status = "DISABLED"
        elif change == "removed_member":
            db.query(WorkspaceMember).filter_by(user_id="alice").delete()
        elif change == "foreign_workspace":
            session.workspace_id = "beta"
        elif change == "foreign_mailbox":
            session.mailbox_id = "bob-mail"
        elif change == "disconnected":
            db.get(MailboxConnection, "alice-mail").status = "DISCONNECTED"
        else:
            db.get(MailboxConnection, "alice-mail").revoked_at = datetime.utcnow()
        db.commit()
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hosted.example.test") as client:
            return await client.get("/phone/recent", headers={"Authorization": "Bearer alice-token"})
    result = asyncio.run(exercise())
    assert result.status_code in {401, 403}
    assert "private" not in result.text


def test_unmigrated_hosted_routes_never_reach_global_mailbox(hosted):
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for method, path in [("GET", "/briefings"), ("POST", "/phone/refresh"),
                                 ("GET", "/settings"), ("POST", "/phone/agent"),
                                 ("POST", "/phone/drafts/1/send")]:
                response = await client.request(method, path, headers={"Authorization": "Bearer alice-token"})
                assert response.status_code == 503
            # M4 migrates voice signaling to the authenticated mailbox context.
            # A valid session reaches body validation; it never falls back to globals.
            response = await client.post("/phone/voice/offer",
                                         headers={"Authorization": "Bearer alice-token"})
            assert response.status_code == 422
            for token in ["alice-token", "Basic alice-token", "Bearer invalid"]:
                response = await client.get("/phone/recent", headers={"Authorization": token})
                assert response.status_code == 401
    asyncio.run(exercise())


def test_hosted_startup_does_not_run_personal_mail_jobs(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "settings", Settings(deployment_mode="hosted", scheduler_enabled=True))
    monkeypatch.setattr(main, "create_tables", lambda: None)
    def forbidden():
        pytest.fail("Hosted startup touched the personal scheduler")
    monkeypatch.setattr(main, "start_scheduler", forbidden)
    monkeypatch.setattr(main, "stop_scheduler", forbidden)
    async def exercise():
        async with main.lifespan(main.app):
            pass
    asyncio.run(exercise())


def test_logout_revokes_only_the_authenticated_session(hosted):
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hosted.example.test") as client:
            headers = {"Authorization": "Bearer alice-token"}
            missing = await client.delete("/auth/session")
            assert missing.status_code == 401
            # Client-provided target identifiers never select the revoked session.
            signed_out = await client.delete("/auth/session", headers=headers,
                                             params={"session_id": "session-bob"})
            assert signed_out.status_code == 200
            refused = await client.get("/phone/recent", headers=headers)
            assert refused.status_code == 401
            bob = await client.get("/phone/recent", headers={"Authorization": "Bearer bob-token"})
            assert bob.status_code == 200
    asyncio.run(exercise())


def _credential_key(directory, version, byte):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (str(version) + ".key")
    path.write_bytes(bytes([byte]) * 32)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("owner,other", [("alice", "bob"), ("bob", "alice")])
def test_credential_store_encrypts_and_binds_credentials_to_mailbox(hosted, tmp_path, owner, other):
    from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 17)
    store = MailboxCredentialStore(key_dir, 1)
    grant = {"read": {"token": "synthetic access", "refresh_token": "synthetic refresh",
                       "scopes": ["mail.read"]}}
    owner_context = MailboxContext(owner, "acme" if owner == "alice" else "beta",
                                   owner + "-mail", "demo", "session-" + owner)
    other_context = MailboxContext(other, "acme" if other == "alice" else "beta",
                                   other + "-mail", "demo", "session-" + other)
    with hosted() as db:
        store.put(db, owner_context, owner + "-mail", grant)
        connection = db.get(MailboxConnection, owner + "-mail")
        assert "synthetic access" not in connection.credential_ciphertext
        assert "synthetic refresh" not in connection.credential_ciphertext
        assert connection.credential_key_version == 1
        assert store.get(db, owner_context, owner + "-mail") == grant
        with pytest.raises(MailboxCredentialError, match="unavailable"):
            store.get(db, other_context, owner + "-mail")
        with pytest.raises(MailboxCredentialError, match="unavailable"):
            store.put(db, owner_context, other + "-mail", grant)


def test_credential_store_authenticates_mailbox_metadata_and_rotates_keys(hosted, tmp_path):
    from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 21)
    _credential_key(key_dir, 2, 22)
    context = MailboxContext("alice", "acme", "alice-mail", "demo", "session-alice")
    grant = {"read": {"token": "synthetic token", "refresh_token": "synthetic refresh"},
             "send": {"token": "separate synthetic send token"}}
    first = MailboxCredentialStore(key_dir, 1)
    second = MailboxCredentialStore(key_dir, 2)
    with hosted() as db:
        first.put(db, context, "alice-mail", grant)
        first_ciphertext = db.get(MailboxConnection, "alice-mail").credential_ciphertext
        second.rotate(db, context, "alice-mail")
        connection = db.get(MailboxConnection, "alice-mail")
        assert connection.credential_key_version == 2
        assert connection.credential_ciphertext != first_ciphertext
        assert second.get(db, context, "alice-mail") == grant
        assert first.get(db, context, "alice-mail") == grant
        connection.provider_account_id = "changed-provider-account"
        db.commit()
        with pytest.raises(MailboxCredentialError, match="unavailable"):
            second.get(db, context, "alice-mail")


def test_credential_disconnect_erases_ciphertext_and_revokes_sessions(hosted, tmp_path):
    from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 23)
    store = MailboxCredentialStore(key_dir, 1)
    context = MailboxContext("alice", "acme", "alice-mail", "demo", "session-alice")
    with hosted() as db:
        store.put(db, context, "alice-mail", {"read": {"token": "temporary"}})
        store.disconnect(db, context, "alice-mail")
        connection = db.get(MailboxConnection, "alice-mail")
        assert connection.status == "REVOKED"
        assert connection.credential_ciphertext == ""
        assert db.get(AuthSession, "session-alice").revoked_at is not None
        assert db.get(AuthSession, "session-bob").revoked_at is None
        with pytest.raises(MailboxCredentialError, match="unavailable"):
            store.get(db, context, "alice-mail")


def test_oauth_transaction_is_encrypted_browser_bound_and_single_use(hosted, tmp_path):
    from app.database.models import OAuthTransaction
    from app.services.oauth_transactions import OAuthTransactionError, OAuthTransactions
    from app.services.mailbox_credentials import MailboxCredentialStore
    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 31)
    transactions = OAuthTransactions(MailboxCredentialStore(key_dir, 1))
    redirect = "https://mail.example.test/auth/google/callback"
    with hosted() as db:
        attempt = transactions.begin(db, "gmail", redirect, "browser-binding-secret")
        row = db.get(OAuthTransaction, attempt.transaction_id)
        assert attempt.state not in row.state_hash
        assert attempt.code_verifier not in row.code_verifier_ciphertext
        assert row.code_verifier_ciphertext.startswith("v1.")

        for args in [
            ("gmail", redirect, "different-browser"),
            ("microsoft", redirect, "browser-binding-secret"),
            ("gmail", "https://mail.example.test/other", "browser-binding-secret"),
        ]:
            with pytest.raises(OAuthTransactionError):
                transactions.claim(db, attempt.state, *args)

        claimed = transactions.claim(db, attempt.state, "gmail", redirect, "browser-binding-secret")
        assert claimed.code_verifier == attempt.code_verifier
        db.refresh(row)
        assert row.consumed_at is not None
        with pytest.raises(OAuthTransactionError, match="already used"):
            transactions.claim(db, attempt.state, "gmail", redirect, "browser-binding-secret")


def test_oauth_transaction_rejects_expired_or_unsafe_redirect(hosted, tmp_path):
    from app.database.models import OAuthTransaction
    from app.services.oauth_transactions import OAuthTransactionError, OAuthTransactions
    from app.services.mailbox_credentials import MailboxCredentialStore

    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 32)
    transactions = OAuthTransactions(MailboxCredentialStore(key_dir, 1), lifetime_seconds=60)
    with hosted() as db:
        with pytest.raises(OAuthTransactionError):
            transactions.begin(db, "gmail", "http://mail.example.test/callback", "browser")
        attempt = transactions.begin(db, "gmail", "https://mail.example.test/callback", "browser")
        row = db.get(OAuthTransaction, attempt.transaction_id)
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
        with pytest.raises(OAuthTransactionError, match="already used"):
            transactions.claim(db, attempt.state, "gmail", row.redirect_uri, "browser")


def test_hosted_google_callback_issues_only_browser_bound_owned_session(hosted, tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from app.email.google_oauth import GoogleMailboxGrant

    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 44)
    settings = Settings(
        app_env="production", deployment_mode="hosted", allowed_hosts=["hosted.example.test"],
        google_web_client_id="synthetic-client", google_web_client_secret="synthetic-secret",
        google_web_redirect_uri="https://hosted.example.test/auth/google/callback",
        hosted_google_allowed_emails=["pilot@example.test"],
        provider_credential_key_directory=str(key_dir), provider_credential_key_version=1,
    )
    monkeypatch.setattr("app.api.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.auth.exchange_code", lambda _settings, _code, _verifier:
                        GoogleMailboxGrant("provider-subject", "pilot@example.test",
                                           {"read": {"token": "synthetic-token",
                                                     "refresh_token": "synthetic-refresh"}}))
    async def inline_exchange(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr("app.api.auth.asyncio.to_thread", inline_exchange)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="https://hosted.example.test") as owner:
            start = await owner.get("/auth/google/start")
            assert start.status_code == 307
            assert urlsplit(start.headers["location"]).hostname == "accounts.google.com"
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            assert "HttpOnly" in start.headers["set-cookie"]
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="https://hosted.example.test") as other_browser:
                stolen = await other_browser.get("/auth/google/callback",
                                                 params={"state": state, "code": "synthetic-code"})
                assert stolen.status_code == 400
            callback = await owner.get("/auth/google/callback",
                                       params={"state": state, "code": "synthetic-code"})
            assert callback.status_code == 200
            assert "synthetic-token" not in callback.text
            assert "HttpOnly" in callback.headers["set-cookie"]
            replay = await owner.get("/auth/google/callback",
                                     params={"state": state, "code": "synthetic-code"})
            assert replay.status_code == 400
            recent = await owner.get("/phone/recent")
            assert recent.status_code == 200 and recent.json()["emails"] == []
            signed_out = await owner.delete("/auth/session")
            assert signed_out.status_code == 200
            refused = await owner.get("/phone/recent")
            assert refused.status_code == 401
            monkeypatch.setattr("app.api.auth.exchange_code", lambda _settings, _code, _verifier:
                                GoogleMailboxGrant("different-subject", "pilot@example.test",
                                                   {"read": {"token": "other-token"}}))
            second_start = await owner.get("/auth/google/start")
            second_state = parse_qs(urlsplit(second_start.headers["location"]).query)["state"][0]
            swapped = await owner.get("/auth/google/callback",
                                      params={"state": second_state, "code": "synthetic-code"})
            assert swapped.status_code == 409

    asyncio.run(exercise())
    with hosted() as db:
        connection = db.query(MailboxConnection).filter_by(provider_account_id="provider-subject").one()
        assert connection.provider == "gmail"
        assert connection.verified_sender == "pilot@example.test"
        assert "synthetic-token" not in connection.credential_ciphertext
        session = db.query(AuthSession).filter_by(mailbox_id=connection.id).one()
        assert session.revoked_at is not None
        assert db.query(MailboxConnection).filter_by(provider_account_id="different-subject").count() == 0


def test_hosted_google_callback_refuses_nonallowlisted_account(hosted, tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from app.email.google_oauth import GoogleMailboxGrant

    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 45)
    settings = Settings(
        app_env="production", deployment_mode="hosted", allowed_hosts=["hosted.example.test"],
        google_web_client_id="synthetic-client", google_web_client_secret="synthetic-secret",
        google_web_redirect_uri="https://hosted.example.test/auth/google/callback",
        hosted_google_allowed_emails=["pilot@example.test"],
        provider_credential_key_directory=str(key_dir), provider_credential_key_version=1,
    )
    monkeypatch.setattr("app.api.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.auth.exchange_code", lambda _settings, _code, _verifier:
                        GoogleMailboxGrant("other-subject", "not-invited@example.test",
                                           {"read": {"token": "synthetic-token"}}))
    async def inline_exchange(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr("app.api.auth.asyncio.to_thread", inline_exchange)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="https://hosted.example.test") as client:
            start = await client.get("/auth/google/start")
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            refused = await client.get("/auth/google/callback",
                                       params={"state": state, "code": "synthetic-code"})
            assert refused.status_code == 403
            assert (await client.get("/phone/recent")).status_code == 401

    asyncio.run(exercise())
    with hosted() as db:
        assert db.query(MailboxConnection).filter_by(provider_account_id="other-subject").count() == 0


@pytest.mark.parametrize("owner,other", [("alice", "bob"), ("bob", "alice")])
def test_owned_recovery_and_review_reject_foreign_ids_and_tickets(hosted, owner, other):
    from tests.test_mailbox_isolation import action, SESSION
    from app.database.models import AgentAction, AgentAudit, DraftReview
    with hosted() as db:
        drafts = {name: action(db, name + "-mail") for name in (owner, other)}
        pending = {name: action(db, name + "-mail", "ARCHIVE", "PENDING_APPROVAL")
                   for name in (owner, other)}
        for name, draft in drafts.items():
            db.add(AgentAudit(conversation_id=draft.conversation_id, step=1, event="TOOL",
                              summary=name + " private details", details_json={"private": name}))
        db.commit()

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hosted.example.test") as client:
            headers = {"Authorization": "Bearer " + owner + "-token"}
            foreign_headers = {"Authorization": "Bearer " + other + "-token"}
            params = {"session_key": SESSION, "mailbox_id": other + "-mail"}
            result = await client.get("/phone/resume", params=params, headers=headers)
            assert result.status_code == 200
            assert result.json()["draft"]["id"] == drafts[owner].id
            assert result.json()["answer"] == owner + "-mail"
            result = await client.get("/phone/pending-actions", headers=headers)
            assert [row["id"] for row in result.json()["actions"]] == [pending[owner].id]
            for suffix in ("audit", "incident"):
                own = await client.get(f"/phone/agent/{drafts[owner].conversation_id}/{suffix}",
                                       params=params, headers=headers)
                assert own.status_code == 200
                if suffix == "incident":
                    assert "private details" not in own.text
                foreign = await client.get(f"/phone/agent/{drafts[other].conversation_id}/{suffix}",
                                           params=params, headers=headers)
                assert foreign.status_code == 404
            foreign_review = await client.post(f"/phone/drafts/{drafts[other].id}/review",
                                              headers=foreign_headers, json=params)
            assert foreign_review.status_code == 200
            stolen = {**params, "review_ticket": foreign_review.json()["review_ticket"]}
            for suffix in ("review", "review-complete"):
                refused = await client.post(f"/phone/drafts/{drafts[other].id}/{suffix}",
                                            headers=headers, json=stolen)
                assert refused.status_code == 409
            review = await client.post(f"/phone/drafts/{drafts[owner].id}/review", headers=headers, json=params)
            assert review.status_code == 200
            assert review.json()["readback"] == f"To: customer@example.test\nSubject: {owner}-mail\n\nThank you."
            body = {**params, "review_ticket": review.json()["review_ticket"]}
            complete = await client.post(f"/phone/drafts/{drafts[owner].id}/review-complete",
                                         headers=headers, json=body)
            assert complete.status_code == 200 and complete.json()["state"] == "CONFIRMING"
            with hosted() as db:
                assert db.get(DraftReview, drafts[other].id).completed_at is None
                assert db.get(AgentAction, drafts[owner].id).status == "PREPARED"
                db.get(AuthSession, "session-" + owner).revoked_at = datetime.utcnow()
                db.commit()
            revoked = await client.post(f"/phone/drafts/{drafts[owner].id}/review-complete",
                                        headers=headers, json=body)
            assert revoked.status_code == 401
            missing = await client.get("/phone/resume", params=params)
            assert missing.status_code == 401
    asyncio.run(exercise())
