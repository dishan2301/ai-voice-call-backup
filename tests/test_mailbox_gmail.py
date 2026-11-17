from datetime import datetime, timedelta
import json

import httplib2
import pytest

from app.api import dependencies
from app.core.auth import MailboxContext
from app.core.config import Settings
from app.database.models import AuthSession, MailboxConnection
from app.email.gmail import GMAIL_SCOPES, GmailConfigurationError, GmailProvider, load_gmail_credentials
from app.services.mailbox_credentials import MailboxCredentialError, MailboxCredentialStore
from tests.test_hosted_auth import hosted, _credential_key


@pytest.fixture
def connected(hosted, tmp_path, monkeypatch):
    key_dir = tmp_path / "keys"
    _credential_key(key_dir, 1, 61)
    settings = Settings(deployment_mode="hosted", gemini_api_key="synthetic",
                        provider_credential_key_directory=str(key_dir), vip_senders=["private-local"])
    store = MailboxCredentialStore(key_dir, 1)
    contexts = {name: MailboxContext(name, workspace, name + "-mail", "gmail", "session-" + name)
                for name, workspace in [("alice", "acme"), ("bob", "beta")]}
    with hosted() as db:
        for name in contexts:
            db.get(MailboxConnection, name + "-mail").provider = "gmail"
        db.commit()
        for name, context in contexts.items():
            store.put(db, context, context.mailbox_id, {"read": {
                "token": name + "-access", "refresh_token": name + "-refresh",
                "client_id": "synthetic", "client_secret": "synthetic",
                "scopes": GMAIL_SCOPES,
                "expiry": (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z",
            }})
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    def forbidden_settings():
        pytest.fail("Hosted construction read personal runtime preferences")
    monkeypatch.setattr(dependencies, "get_runtime_settings", forbidden_settings)
    return hosted, settings, store, contexts


def test_owned_factory_uses_distinct_grants_and_rechecks_revocation(connected, monkeypatch):
    sessions, settings, store, contexts = connected
    calls = []
    class Http:
        def __init__(self, **kwargs):
            pass
        def request(self, uri, method, **kwargs):
            calls.append(kwargs["headers"]["authorization"])
            return httplib2.Response({"status": "200"}), b'{"messages":[]}'
        def close(self):
            pass
    monkeypatch.setattr("app.email.mailbox_gmail.httplib2.Http", Http)
    services = {name: dependencies.create_generation_service(context, sessions)
                for name, context in contexts.items()}
    for name, service in services.items():
        assert service.settings.mailbox_id == name + "-mail"
        assert service.settings.vip_senders == []
        assert service.session_factory is sessions
        assert service.provider.search("subject:test") == []
    assert calls == ["Bearer alice-access", "Bearer bob-access"]
    with sessions() as db:
        db.get(AuthSession, "session-alice").revoked_at = datetime.utcnow()
        db.commit()
    with pytest.raises(MailboxCredentialError):
        services["alice"].provider.search("subject:test")
    assert len(calls) == 2
    assert services["bob"].provider.search("subject:test") == []
    with pytest.raises(ValueError, match="authenticated"):
        dependencies.create_generation_service()
    for constructor in (GmailProvider.from_settings, load_gmail_credentials):
        with pytest.raises(GmailConfigurationError, match="owned"):
            constructor(settings)


@pytest.mark.parametrize("interruption", [None, "replace", "disconnect", "during_read"])
def test_refresh_persists_only_current_owned_grant(connected, monkeypatch, interruption):
    sessions, settings, store, contexts = connected
    context = contexts["alice"]
    with sessions() as db:
        bundle = store.get(db, context, context.mailbox_id)
        bundle["read"]["expiry"] = "2020-01-01T00:00:00Z"
        store.put(db, context, context.mailbox_id, bundle)
    reads = []
    class Http:
        def __init__(self, **kwargs):
            pass
        def request(self, uri, method="GET", **kwargs):
            if uri == "https://oauth2.googleapis.com/token":
                if interruption in {"replace", "disconnect"}:
                    with sessions() as db:
                        if interruption == "disconnect":
                            store.disconnect(db, context, context.mailbox_id)
                        else:
                            replacement = {"read": {**bundle["read"], "token": "replacement"}}
                            store.put(db, context, context.mailbox_id, replacement)
                return httplib2.Response({"status": "200"}), json.dumps({
                    "access_token": "renewed-access", "expires_in": 3600, "token_type": "Bearer",
                }).encode()
            reads.append(kwargs["headers"]["authorization"])
            if interruption == "during_read":
                with sessions() as db:
                    store.disconnect(db, context, context.mailbox_id)
            return httplib2.Response({"status": "200"}), b'{"messages":[]}'
        def close(self):
            pass
    monkeypatch.setattr("app.email.mailbox_gmail.httplib2.Http", Http)
    provider = GmailProvider.from_mailbox(settings, context, sessions)
    if interruption:
        with pytest.raises(MailboxCredentialError):
            provider.search("subject:test")
        assert reads == (["Bearer renewed-access"] if interruption == "during_read" else [])
    else:
        assert provider.search("subject:test") == []
        assert reads == ["Bearer renewed-access"]
    with sessions() as db:
        if interruption not in {"disconnect", "during_read"}:
            assert store.get(db, context, context.mailbox_id)["read"]["token"] == (
                "replacement" if interruption == "replace" else "renewed-access")
        assert store.get(db, contexts["bob"], "bob-mail")["read"]["token"] == "bob-access"


def test_hosted_refresh_syncs_two_accounts_through_owned_transport(connected, monkeypatch):
    import asyncio
    from urllib.parse import urlsplit
    from fastapi import Depends
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select, text
    from app.core.auth import require_mailbox_context
    from app.api import phone
    from app.database.models import Email, SyncCheckpoint
    from app.main import app

    sessions, settings, store, contexts = connected
    with sessions() as db:
        db.execute(text("""CREATE VIRTUAL TABLE message_search USING fts5(
            provider UNINDEXED, mailbox_id UNINDEXED, provider_message_id UNINDEXED,
            sender_name, sender_email, subject, body_text, summary, keywords, organization)"""))
        db.commit()
    class Http:
        def __init__(self, **kwargs):
            pass
        def request(self, uri, method="GET", **kwargs):
            owner = kwargs["headers"]["authorization"].split()[1].split("-")[0]
            path = urlsplit(uri).path
            if path.endswith("/profile"):
                data = {"historyId": "100"}
            elif path.endswith("/messages"):
                data = {"messages": [{"id": "shared"}]}
            else:
                assert path.endswith("/messages/shared")
                # Full sync counts archived mail outside the briefing time window too.
                data = {"id": "shared", "threadId": "thread", "internalDate": "946684800000",
                        "payload": {"headers": [{"name": "Subject", "value": owner + " private"}],
                                    "mimeType": "text/plain", "body": {"data": "aGVsbG8="}}}
            return httplib2.Response({"status": "200"}), json.dumps(data).encode()
        def close(self):
            pass
    monkeypatch.setattr("app.email.mailbox_gmail.httplib2.Http", Http)
    async def inline_pool(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(phone, "run_in_threadpool", inline_pool)
    async def service(context=Depends(require_mailbox_context)):
        return dependencies.create_generation_service(context, sessions)
    app.dependency_overrides[dependencies.get_generation_service] = service
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://hosted.example.test") as client:
            unauthenticated = await asyncio.wait_for(client.post("/phone/refresh"), timeout=10)
            assert unauthenticated.status_code == 401
            for owner in ("alice", "bob"):
                headers = {"Authorization": "Bearer " + owner + "-token"}
                result = await asyncio.wait_for(client.post(
                    "/phone/refresh", headers=headers, params={"mailbox_id": "foreign"}), timeout=10)
                assert result.status_code == 200, result.text
                assert result.json() == {"status": "synced", "synced_messages": 1, "analysis_status": "pending"}
                recent = await asyncio.wait_for(client.get("/phone/recent", headers=headers), timeout=10)
                assert [row["subject"] for row in recent.json()["emails"]] == [owner + " private"]
    try:
        asyncio.run(exercise())
    finally:
        app.dependency_overrides.pop(dependencies.get_generation_service, None)
    with sessions() as db:
        rows = db.scalars(select(Email).where(Email.provider == "gmail")).all()
        assert len(rows) == 2 and all(not row.is_deleted for row in rows)
        assert set(db.execute(text("SELECT mailbox_id, subject FROM message_search")).all()) == {
            ("alice-mail", "alice private"), ("bob-mail", "bob private")}
        for owner in ("alice", "bob"):
            assert db.get(SyncCheckpoint, "gmail:" + owner + "-mail").last_history_id == "100"


def test_hosted_sync_refuses_publication_after_session_revocation(connected, monkeypatch):
    from sqlalchemy import select
    from app.database.models import Email, SyncCheckpoint
    from tests.test_persistent_briefing import _important_mail
    from datetime import timezone
    sessions, settings, store, contexts = connected
    service = dependencies.create_generation_service(contexts["alice"], sessions)
    def fetched_then_revoked(start, end):
        with sessions() as db:
            db.get(AuthSession, "session-alice").revoked_at = datetime.utcnow()
            db.commit()
        return [_important_mail(datetime.now(timezone.utc))], [], ("gmail:alice-mail", "100"), False
    monkeypatch.setattr(service, "_sync_messages", fetched_then_revoked)
    with pytest.raises(MailboxCredentialError):
        service.sync_only()
    with sessions() as db:
        assert db.scalar(select(Email).where(Email.provider_message_id == "important-1")) is None
        assert db.get(SyncCheckpoint, "gmail:alice-mail") is None


def test_send_refuses_different_gmail_identity_before_submit():
    from unittest.mock import Mock
    from app.email.gmail import GmailProvider
    def service(identity):
        api = Mock()
        api.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": identity}
        return api
    reader, sender = service('reader@example.test'), service('other@example.test')
    provider = GmailProvider(reader, send_service=sender)
    with pytest.raises(GmailConfigurationError, match='different account'):
        provider.send_message({'to': 'recipient@example.test', 'subject': 'Hello', 'body': 'Hi'})
    sender.users.return_value.messages.return_value.send.assert_not_called()


def test_send_requires_separate_send_authorization():
    from app.email.gmail import GmailProvider
    provider = GmailProvider(object())
    with pytest.raises(GmailConfigurationError, match="Separate Gmail send authorization"):
        provider.send_message({"to": "recipient@example.test", "subject": "Hello", "body": "Hi"})


def test_send_identity_match_submits_once_without_automatic_retry():
    import base64
    from email import policy
    from email.parser import BytesParser
    from app.email.gmail import GmailProvider
    class Request:
        def __init__(self, value): self.value = value
        def execute(self, **kwargs): return self.value
    class Users:
        def __init__(self, identity): self.identity = identity; self.submits = 0; self.payload = None
        def getProfile(self, **kwargs): return Request({"emailAddress": self.identity})
        def messages(self): return self
        def send(self, **kwargs):
            self.submits += 1
            self.payload = kwargs["body"]
            return Request({'id': 'receipt-1'})
    class Service:
        def __init__(self, identity): self.user = Users(identity)
        def users(self): return self.user
    reader, sender = Service('same@example.test'), Service('same@example.test')
    provider = GmailProvider(reader, send_service=sender)
    result = provider.send_message({'to': 'recipient@example.test', 'subject': 'Re: Hello', 'body': 'Hi',
                                   'thread_id': 'thread-1', 'in_reply_to': '<message@example.test>',
                                   'references': '<root@example.test> <message@example.test>'})
    assert result['id'] == 'receipt-1'
    assert sender.user.submits == 1
    assert sender.user.payload["threadId"] == "thread-1"
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(sender.user.payload["raw"]))
    assert message["To"] == "recipient@example.test"
    assert message["Subject"] == "Re: Hello"
    assert message["In-Reply-To"] == "<message@example.test>"
    assert message["References"] == "<root@example.test> <message@example.test>"
    assert message.get_content().strip() == "Hi"
