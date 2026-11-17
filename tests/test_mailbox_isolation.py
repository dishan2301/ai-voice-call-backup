"""Mailbox scope regressions, not a substitute for authenticated-user isolation."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.agents.draft_safety import begin_review, complete_review, reviewed
from app.agents.mail_agent import MailAgent
from app.api import phone
from app.core.auth import MailboxContext
from app.database.models import (
    AgentAction, AgentAudit, AgentConversation, AgentMemory, AnalysisJob,
    Attachment, Commitment, DraftReview, Email,
)
from app.services.persistent_briefing_service import BriefingGenerationError, PersistentBriefingService
from app.agents.email_analyzer import DemoAnalyzer
from tests.test_mail_agent import FakeGmail, FakeLLM, make_agent
from tests.test_persistent_briefing import StaticProvider


SESSION = "same-browser-session-for-both"


@pytest.fixture
def mailboxes(monkeypatch):
    agent_a, factory = make_agent([])
    agent_a.settings = agent_a.settings.model_copy(update={"mailbox_id": "a"})
    agent_b = MailAgent(agent_a.settings.model_copy(update={"mailbox_id": "b"}),
                        factory, FakeLLM([]), FakeGmail())
    monkeypatch.setattr(phone, "get_settings", lambda: agent_a.settings)
    monkeypatch.setattr("app.core.auth.get_settings", lambda: agent_a.settings)
    # Review helpers must resolve the server's configured scope too.
    monkeypatch.setattr("app.agents.draft_safety.get_settings", lambda: agent_a.settings)
    return agent_a, agent_b, factory


def action(db, mailbox, kind="PREPARE_DRAFT", status="PREPARED"):
    conversation = AgentConversation(mailbox_id=mailbox, session_key=SESSION,
                                     user_message="synthetic request", assistant_message=mailbox)
    db.add(conversation)
    db.flush()
    row = AgentAction(conversation_id=conversation.id, action_type=kind, status=status,
                      arguments_json={"to": "customer@example.test", "subject": mailbox,
                                      "body": "Thank you.", "thread_id": "thread"},
                      rationale="synthetic", risk="HIGH")
    db.add(row)
    db.commit()
    return row


def email(db, mailbox, *, provider="gmail", deleted=False):
    row = Email(provider=provider, mailbox_id=mailbox,
                provider_message_id="deleted-id" if deleted else "shared-id",
                thread_id="shared-thread", received_at=datetime.utcnow(), body_hash="hash",
                sender_name=mailbox, subject=mailbox, is_deleted=deleted)
    db.add(row)
    db.flush()
    return row


def test_memory_read_and_update_stay_in_mailbox(mailboxes):
    a, b, factory = mailboxes
    with factory() as db:
        for agent, value in [(b, "B secret"), (a, "A preference")]:
            agent._remember(db, {"kind": "preference", "key": "tone", "value": value,
                                 "source": "user"})
        db.commit()
        assert [row["value"] for row in a._recall(db, {})["memories"]] == ["A preference"]
        assert [row["value"] for row in b._recall(db, {})["memories"]] == ["B secret"]
        assert len(db.scalars(select(AgentMemory)).all()) == 2


def test_foreign_draft_cannot_be_reviewed_completed_or_sent_even_with_ticket(mailboxes):
    a, b, factory = mailboxes
    with factory() as db:
        foreign = action(db, "b")
        review = begin_review(db, foreign.id, SESSION, "b")
        ticket = review["review_ticket"]
        complete_review(db, foreign.id, SESSION, ticket, "b")
        with pytest.raises(ValueError, match="not available"):
            begin_review(db, foreign.id, SESSION)
        with pytest.raises(ValueError, match="not available"):
            complete_review(db, foreign.id, SESSION, ticket)
        assert not reviewed(db, foreign, SESSION, ticket, "a")
    assert a.run("done", SESSION, review_ticket=ticket)["state"] == "READ_BACK"
    assert not a.gmail.sent
    with factory() as db:
        assert db.get(AgentAction, foreign.id).status == "PREPARED"
    # The owning mailbox still uses the exact-review send path.
    assert b.run("done", SESSION, review_ticket=ticket)["state"] == "SENT"
    assert len(b.gmail.sent) == 1


def test_question_only_invalidates_own_mailbox_review(mailboxes):
    a, _b, factory = mailboxes
    with factory() as db:
        drafts = [action(db, box) for box in ("a", "b")]
        for draft, box in zip(drafts, ("a", "b")):
            ticket = begin_review(db, draft.id, SESSION, box)["review_ticket"]
            complete_review(db, draft.id, SESSION, ticket, box)
    a.run("kal", SESSION)  # Deterministic clarification, without a model call.
    with factory() as db:
        assert db.get(DraftReview, drafts[0].id).completed_at is None
        assert db.get(DraftReview, drafts[1].id).completed_at is not None


def test_foreign_pending_action_cannot_be_listed_or_approved(mailboxes):
    a, b, factory = mailboxes
    with factory() as db:
        own = action(db, "a", "ARCHIVE", "PENDING_APPROVAL")
        foreign = action(db, "b", "ARCHIVE", "PENDING_APPROVAL")
        assert [r["action_id"] for r in a._pending_actions(db)["actions"]] == [own.id]
        assert "error" in a._approve_action(db, {"action_id": foreign.id}, foreign.id)
        assert foreign.status == "PENDING_APPROVAL"
        assert b._approve_action(db, {"action_id": foreign.id}, foreign.id)["status"] == "APPROVED"


def test_phone_recovery_audit_and_pending_actions_exclude_foreign_session(mailboxes):
    _a, _b, factory = mailboxes
    with factory() as db:
        own = action(db, "a")
        foreign = action(db, "b")  # Newest same-session row must not win.
        pending = action(db, "a", "ARCHIVE", "PENDING_APPROVAL")
        action(db, "b", "ARCHIVE", "PENDING_APPROVAL")
        db.add(AgentAudit(conversation_id=foreign.conversation_id, step=1, event="TOOL",
                          summary="B secret", details_json={"private": "B"}))
        db.commit()
        context = MailboxContext("test-user", "test-workspace", "a", "gmail", legacy_local=True)
        result = asyncio.run(phone.phone_resume(SESSION, db, context))
        assert result["answer"] == "a"
        assert result["draft"]["id"] == own.id
        assert [r["id"] for r in asyncio.run(phone.pending_actions(db, context))["actions"]] == [pending.id]
        for endpoint in (phone.agent_audit, phone.agent_incident):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(endpoint(foreign.conversation_id, SESSION, db, context))
            assert exc.value.status_code == 404
            assert asyncio.run(endpoint(own.conversation_id, SESSION, db, context))["conversation_id"] == own.conversation_id


def test_recent_mail_and_commitments_exclude_other_mailboxes_providers_and_deleted(mailboxes):
    a, _b, factory = mailboxes
    with factory() as db:
        for box, provider, deleted in [("a", "gmail", False), ("b", "gmail", False),
                                       ("a", "outlook", False), ("a", "gmail", True)]:
            row = email(db, box, provider=provider, deleted=deleted)
            db.add(Commitment(email_id=row.id, direction="YOU_OWE", source_text=box))
        db.commit()
        context = MailboxContext("test-user", "test-workspace", "a", "gmail", legacy_local=True)
        assert [r["subject"] for r in asyncio.run(phone.phone_recent_mail(50, db, context))["emails"]] == ["a"]
        assert [r["subject"] for r in asyncio.run(phone.phone_commitments(db, context))["commitments"]] == ["a"]
        assert [r["subject"] for r in a._commitments(db, {})["commitments"]] == ["a"]


def test_worker_cannot_claim_expire_or_finish_foreign_analysis_jobs(mailboxes):
    a, _b, factory = mailboxes
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(), a.settings, factory)
    with factory() as db:
        jobs = []
        for box, provider in [("a", "gmail"), ("b", "gmail"), ("a", "outlook")]:
            row = email(db, box, provider=provider)
            job = AnalysisJob(email_id=row.id, source_hash="hash", status="processing",
                              leased_until=datetime.utcnow() - timedelta(minutes=10))
            db.add(job)
            jobs.append(job)
        db.commit()
    assert service.claim_analysis_jobs() == [jobs[0].id]
    service.finish_analysis_job(jobs[1].id)
    service.finish_analysis_for_message("shared-id")
    with factory() as db:
        assert db.get(AnalysisJob, jobs[0].id).status == "complete"
        for job in jobs[1:]:
            row = db.get(AnalysisJob, job.id)
            assert row.status == "processing" and row.attempts == 0


def test_attachment_worker_cannot_use_own_credentials_for_foreign_attachment(mailboxes):
    a, _b, factory = mailboxes
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(), a.settings, factory)
    with factory() as db:
        for box, provider in [("b", "gmail"), ("a", "outlook")]:
            row = email(db, box, provider=provider)
            db.add(Attachment(email_id=row.id, filename="sample.txt", mime_type="text/plain",
                              attachment_id="shared-attachment", extraction_status="pending"))
        db.commit()
    class MustNotDownload:
        def __getattr__(self, name):
            pytest.fail("Foreign attachment reached provider: " + name)
    assert service.run_attachment_worker(MustNotDownload()) == {"claimed": 0, "completed": 0, "failed": 0}
    with factory() as db:
        assert all(r.extraction_status == "pending" for r in db.scalars(select(Attachment)))


def test_http_routes_refuse_foreign_ids_and_ignore_client_mailbox_selection(mailboxes):
    from httpx import ASGITransport, AsyncClient
    from app.database.db import get_db
    from app.main import app

    _a, _b, factory = mailboxes
    with factory() as db:
        foreign = action(db, "b")
        own = action(db, "a")
    async def database():
        with factory() as db:
            yield db
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for suffix in ("audit", "incident"):
                response = await client.get(f"/phone/agent/{foreign.conversation_id}/{suffix}",
                                            params={"session_key": SESSION, "mailbox_id": "b"})
                assert response.status_code == 404
            for suffix in ("review", "review-complete"):
                response = await client.post(f"/phone/drafts/{foreign.id}/{suffix}",
                                             json={"session_key": SESSION, "mailbox_id": "b"})
                assert response.status_code == 409
            response = await client.post(f"/phone/drafts/{own.id}/review", json={"session_key": SESSION})
            assert response.status_code == 200 and response.json()["draft_id"] == own.id
    app.dependency_overrides[get_db] = database
    try:
        asyncio.run(exercise())
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_legacy_memory_migration_preserves_data_in_default_scope(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app.core.config import Settings
    from app.database import db as database

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("""CREATE TABLE agent_memories (
            id INTEGER PRIMARY KEY, kind TEXT, key TEXT, value TEXT, source TEXT,
            confidence FLOAT, active BOOLEAN, created_at DATETIME, updated_at DATETIME)"""))
        connection.execute(text("""INSERT INTO agent_memories
            (id, kind, key, value, source, confidence, active) VALUES
            (1, 'preference', 'tone', 'synthetic preference', 'user', 1, 1)"""))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "settings", Settings(database_url=str(engine.url), database_key_file=""))
    database.create_tables()
    database.create_tables()  # Additive migration is repeatable.
    with Session(engine) as db:
        row = db.get(AgentMemory, 1)
        assert row.mailbox_id == "default" and row.value == "synthetic preference"


def test_sync_index_tombstones_and_checkpoints_stay_in_mailbox(mailboxes):
    from app.database.models import SyncCheckpoint
    from app.schemas.mail import MailMessage
    a, b, factory = mailboxes
    with factory() as db:
        db.execute(text("""CREATE VIRTUAL TABLE message_search USING fts5(
            provider UNINDEXED, mailbox_id UNINDEXED, provider_message_id UNINDEXED,
            sender_name, sender_email, subject, body_text, summary, keywords, organization)"""))
        db.commit()
    services = {box: PersistentBriefingService(StaticProvider([]), DemoAnalyzer(), agent.settings, factory)
                for box, agent in [("a", a), ("b", b)]}
    def mail(box, message_id):
        return MailMessage(message_id=message_id, thread_id=message_id,
                           sender_email=f"{box}@example.test", subject=box + " private",
                           body_text=box + " private body", received_at=datetime.now(timezone.utc))
    # The foreign mailbox is inserted first so an unscoped scalar query picks it.
    for box in ("b", "a"):
        services[box]._commit_sync([mail(box, "shared"), mail(box, box + "-only")], [],
                                    ("gmail:" + box, box + "1"), full_sync=True)
    with factory() as db:
        for box in ("a", "b"):
            subjects = db.execute(text("SELECT subject FROM message_search WHERE mailbox_id=:box"),
                                  {"box": box}).scalars().all()
            assert subjects == [box + " private", box + " private"]
    services["a"]._commit_sync([mail("a", "shared")], [], ("gmail:a", "a2"), full_sync=True)
    services["a"]._commit_sync([], ["shared"], ("gmail:a", "a3"))
    with factory() as db:
        rows = db.scalars(select(Email)).all()
        assert all(row.is_deleted == (row.mailbox_id == "a") for row in rows)
        assert db.execute(text("SELECT mailbox_id FROM message_search")).scalars().all() == ["b", "b"]
        assert db.get(SyncCheckpoint, "gmail:a").last_history_id == "a3"
        assert db.get(SyncCheckpoint, "gmail:b").last_history_id == "b1"
    with pytest.raises(BriefingGenerationError, match="CHECKPOINT_SCOPE_MISMATCH"):
        services["a"]._commit_sync([mail("a", "must-rollback")], [], ("gmail:b", "tampered"))
    with factory() as db:
        assert db.scalar(select(Email).where(Email.provider_message_id == "must-rollback")) is None
        assert db.get(SyncCheckpoint, "gmail:b").last_history_id == "b1"


def test_nondefault_sync_refuses_legacy_unscoped_search_index():
    from tests.test_persistent_briefing import _session_factory, _important_mail
    from app.core.config import Settings
    factory = _session_factory()
    service = PersistentBriefingService(StaticProvider([]), DemoAnalyzer(),
                                        Settings(email_provider="gmail", mailbox_id="another"), factory)
    with pytest.raises(BriefingGenerationError, match="SEARCH_SCOPE_MIGRATION_REQUIRED"):
        service._commit_sync([_important_mail(datetime.now(timezone.utc))], [], None)
    with factory() as db:
        assert db.scalar(select(Email)) is None
