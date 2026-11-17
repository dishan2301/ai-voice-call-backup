import asyncio
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.api.dependencies import get_generation_service
from app.database.db import Base, get_db
from app.database.models import AgentAction, AgentAudit, AgentConversation, Briefing, BriefingItem, BriefingRun, Commitment, Email, EmailAnalysis
from app.main import app
from app.services.briefing_service import demo_briefing
from app.services.persistent_briefing_service import BriefingGenerationError


def _setup_database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    today = datetime.now(timezone.utc).astimezone(get_settings().timezone).date()
    now = datetime.utcnow()
    with sessions() as session:
        email = Email(
            provider="gmail",
            provider_message_id="dashboard-1",
            thread_id="dashboard-thread",
            sender_name="Customer",
            sender_email="customer@example.com",
            subject="Question",
            received_at=now,
            body_hash="hash",
        )
        session.add(email)
        session.flush()
        analysis = EmailAnalysis(
            email_id=email.id,
            importance="HIGH",
            category="CUSTOMER",
            needs_reply=True,
            needs_action=True,
            urgency="TODAY",
            sender="Customer",
            subject="Question",
            summary="The customer needs an answer.",
            why_important="The customer is waiting.",
            requested_action="Reply today.",
            deadline="Today",
            key_facts_json=[],
            risk_flags_json=[],
            confidence=0.9,
            model="test",
            prompt_version="v1",
        )
        session.add(analysis)
        session.flush()
        briefing = Briefing(
            briefing_date=today,
            generated_at=now,
            period_start=now - timedelta(days=1),
            period_end=now,
            total_emails=1,
            critical_count=0,
            high_count=1,
            reply_count=1,
            action_count=0,
            low_priority_count=0,
            detailed_brief="Daily detailed brief",
            spoken_brief="Good morning.",
        )
        session.add(briefing)
        session.flush()
        session.add(
            BriefingRun(
                provider="gmail",
                period_key=str(today),
                period_start=now - timedelta(days=1),
                period_end=now,
                status="COMPLETE",
                briefing_id=briefing.id,
            )
        )
        item = BriefingItem(
            briefing_id=briefing.id,
            email_analysis_id=analysis.id,
            rank=1,
            section="Replies Needed",
            user_status="NEEDS_REPLY",
        )
        session.add(item)
        session.add(Commitment(email_id=email.id, direction="THEY_OWE",
                               source_text="We will send the report by Friday.", due_text="by Friday"))
        conversation = AgentConversation(session_key="recover-session-1234", user_message="Draft a reply",
                                         assistant_message="I prepared the reply for review.")
        session.add(conversation)
        session.flush()
        session.add(AgentAction(conversation_id=conversation.id, action_type="PREPARE_DRAFT",
                                arguments_json={"to": "customer@example.com", "subject": "Re: Question",
                                                "body": "Thank you.", "thread_id": "dashboard-thread"},
                                rationale="User requested it", risk="LOW", status="PREPARED"))
        session.add(AgentAudit(conversation_id=conversation.id, step=1, event="TOOL",
                               tool_name="search_mail", summary="Found source",
                               details_json={"result": {"messages": [{"message_id": "dashboard-1",
                                   "thread_id": "dashboard-thread", "sender": "Customer", "subject": "Question"}]}}))
        session.commit()
        item_id = item.id
    return sessions, item_id, conversation.id


def test_dashboard_and_local_apis() -> None:
    sessions, item_id, conversation_id = _setup_database()

    async def override_db():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            dashboard = await client.get("/")
            saved = await client.get(f"/?briefing_id=1")
            today = await client.get("/briefings/today")
            important = await client.get("/emails/important")
            phone_brief = await client.get("/phone/briefing")
            phone_recent = await client.get("/phone/recent")
            retired_search = await client.get("/phone/search", params={"sender": "Customer"})
            retired_queue = await client.get("/phone/decisions")
            commitments = await client.get("/phone/commitments")
            resumed = await client.get("/phone/resume", params={"session_key": "recover-session-1234"})
            audit = await client.get(f"/phone/agent/{conversation_id}/audit",
                                     params={"session_key": "recover-session-1234"})
            denied_audit = await client.get(f"/phone/agent/{conversation_id}/audit",
                                            params={"session_key": "another-session-1234"})
            incident = await client.get(f"/phone/agent/{conversation_id}/incident",
                                        params={"session_key": "recover-session-1234"})
            done = await client.post(f"/items/{item_id}/done")
            track = await client.post(f"/items/{item_id}/track")
            settings = await client.patch(
                "/settings",
                json={"briefing_time": "08:45", "vip_domains": ["client.example"]},
            )
            saved_settings = await client.post(
                "/dashboard/settings",
                headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
                data={
                    "briefing_time": "09:30",
                    "app_timezone": "Asia/Kolkata",
                    "tts_enabled": "true",
                    "tts_rate": "165",
                    "tts_volume": "1",
                    "model": "gemini-3.8-flash",
                },
            )
            return (dashboard, saved, today, important, phone_brief, phone_recent, retired_search, retired_queue,
                    commitments, resumed, audit, denied_audit, incident, done, track,
                    settings, saved_settings)

    try:
        (dashboard, saved, today, important, phone_brief, phone_recent, retired_search, retired_queue,
         commitments, resumed, audit, denied_audit, incident, done, track,
         settings, saved_settings) = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert dashboard.status_code == 200
    assert "Daily detailed brief" in dashboard.text
    assert "Ask Gemini about your mail" in dashboard.text
    assert 'src="/static/dashboard.js?v=3"' in dashboard.text
    assert 'data-speech=' in dashboard.text
    assert saved.status_code == 200
    assert "Saved briefing" in saved.text
    assert "Verify original in Gmail" in saved.text
    assert "Important mail and actions" in saved.text
    assert today.json()["high_count"] == 1
    assert important.json()[0]["needs_reply"] is True
    assert retired_search.status_code == 404
    assert retired_queue.status_code == 404
    assert phone_brief.json()["items"][0]["subject"] == "Question"
    assert phone_brief.json()["degraded"] is False
    assert phone_recent.json()["count"] == 1
    assert phone_recent.json()["emails"][0]["subject"] == "Question"
    assert phone_recent.json()["emails"][0]["message_id"] == "dashboard-1"
    assert commitments.json()["they_owe"] == 1
    assert commitments.json()["commitments"][0]["message_id"] == "dashboard-1"
    assert resumed.json()["state"] == "RECOVERED"
    assert resumed.json()["draft"]["to"] == "customer@example.com"
    assert resumed.json()["evidence"][0]["message_id"] == "dashboard-1"
    assert audit.status_code == 200
    assert denied_audit.status_code == 404
    assert incident.json()["events"] == [{"step": 1, "event": "TOOL", "tool": "search_mail",
                                           "created_at": incident.json()["events"][0]["created_at"]}]
    assert "customer@example.com" not in incident.text
    assert "Question" not in incident.text
    assert "#" not in phone_brief.json()["items"][0]["summary"]
    assert done.json()["user_status"] == "RESOLVED"
    assert track.json()["user_status"] == "NEEDS_REPLY"
    assert settings.json()["briefing_time"] == "08:45"
    assert settings.json()["model"] == get_settings().gemini_model
    assert saved_settings.status_code == 303


def test_manual_generate_api_returns_a_complete_brief() -> None:
    class FakeGenerationService:
        def generate(self, now):
            return demo_briefing(now)

    async def override_generation():
        return FakeGenerationService()

    app.dependency_overrides[get_generation_service] = override_generation

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/briefings/generate")

    try:
        response = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["total_emails"] == 8


def test_manual_generate_api_reports_safe_incomplete_sync() -> None:
    class FailingGenerationService:
        def generate(self, now):
            raise BriefingGenerationError("EMAIL_SYNC")

    async def override_generation():
        return FailingGenerationService()

    app.dependency_overrides[get_generation_service] = override_generation

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/briefings/generate")

    try:
        response = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "Email sync failed. Today's briefing may be incomplete."


def test_phone_refresh_uses_the_generation_service(monkeypatch) -> None:
    class FakeGenerationService:
        def generate(self, now):
            return demo_briefing(now)

    async def override_generation():
        return FakeGenerationService()

    app.dependency_overrides[get_generation_service] = override_generation
    from app.api import phone

    async def inline_worker(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(phone, "run_in_threadpool", inline_worker)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/phone/refresh")

    try:
        response = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["total_emails"] == 8


def test_waiting_radar_is_read_only_and_evidence_linked(monkeypatch) -> None:
    class Provider:
        def unanswered(self, days, limit):
            assert (days, limit) == (3, 20)
            return [{"message_id": "sent-1", "thread_id": "thread-1",
                     "subject": "Quotation", "last_message_at": "2026-09-12T09:00:00Z"}]

    class Service:
        provider = Provider()

    async def override_generation():
        return Service()

    app.dependency_overrides[get_generation_service] = override_generation
    from app.api import phone

    async def inline_worker(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(phone, "run_in_threadpool", inline_worker)

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/phone/waiting", params={"days": 3})

    try:
        response = asyncio.run(exercise())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["threads"][0]["message_id"] == "sent-1"
    assert response.json()["threads"][0]["safe_actions"] == ["Review", "Draft follow-up", "Defer"]
