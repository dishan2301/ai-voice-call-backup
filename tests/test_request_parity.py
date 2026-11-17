"""Both user-facing transports must reach the same evidence and safety policy."""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.phone import AgentRequest, phone_agent
from app.agents.requests import handle_mail_request
from app.database.db import Base
from app.voice.session import VoiceSession
from tests.test_mail_agent import make_agent, decision


@pytest.fixture(autouse=True)
def inline_fake_agents(monkeypatch):
    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr("app.api.phone.run_in_threadpool", run_inline)
    monkeypatch.setattr("app.voice.session.asyncio.to_thread", run_inline)


def agent_for_transport(query):
    agent, _ = make_agent([
        decision(tool="search_mail", arguments={"query": query}),
        decision(kind="final", answer="The source asks you to confirm."),
        decision(tool="get_thread", arguments={"thread_id": "thread-1"}),
        decision(kind="final", answer="The meeting email asks about 3 pm."),
    ])
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    agent.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    search = agent.gmail.search
    get_thread = agent.gmail.get_thread
    def synthetic_search(query, *args):
        rows = search(query, *args)
        if "from:helix" in query:
            rows = [dict(row, sender="Helix Support <helix@example.test>") for row in rows]
        return [dict(row, received_at="2026-09-22T10:00:00Z") for row in rows]
    agent.gmail.search = synthetic_search
    agent.gmail.get_thread = lambda *args: [dict(row, received_at="2026-09-22T10:00:00Z") for row in get_thread(*args)]
    return agent


@pytest.mark.parametrize("question,query", [
    ("Find turbine repair mail from Helix", 'from:helix "turbine repair"'),
    ("What changed in the Atlas royalty agreement?", 'Atlas {royalty agreement}'),
    ("Any shipment delay mail for Cedar?", 'Cedar {shipment delay}'),
])
def test_http_and_live_transcript_return_same_search_and_followup_evidence(monkeypatch, question, query):
    async def exercise():
        typed = agent_for_transport(query)
        monkeypatch.setattr("app.api.phone.MailAgent", lambda: typed)
        text_result = await phone_agent(AgentRequest(message=question, session_key="typed-parity"))
        spoken = agent_for_transport(query)
        voice = VoiceSession(lambda event: None, lambda: spoken, "voice-parity")
        voice_result = await voice.respond(question)
        assert typed.gmail.queries == spoken.gmail.queries == [query]
        assert text_result["evidence"] == voice_result["evidence"]
        assert text_result["answer"] == voice_result["answer"]
        assert text_result["interpretation"] == voice_result["interpretation"] == question
        text_followup = await phone_agent(AgentRequest(message="Read that thread", session_key="typed-parity"))
        voice_followup = await voice.respond("Read that thread")
        assert text_followup["evidence"] == voice_followup["evidence"]
        assert text_followup["answer"] == voice_followup["answer"]
    asyncio.run(exercise())


@pytest.mark.parametrize("utterance", ["done", "send it", "bhej do", "sand it", "argent mail padho"])
def test_voice_safety_is_identical_over_http_and_streaming(monkeypatch, utterance):
    def forbidden():
        raise AssertionError("Unconfirmed voice must not construct a mail agent")

    async def exercise():
        monkeypatch.setattr("app.api.phone.MailAgent", forbidden)
        http = await phone_agent(AgentRequest(message=utterance, input_mode="voice"))
        live = await VoiceSession(lambda event: None, forbidden).respond(utterance)
        assert http == live
        assert http["state"] in {"CONFIRMING", "NEEDS_CLARIFICATION"}
    asyncio.run(exercise())


def test_high_confidence_voice_confirmation_still_cannot_send(monkeypatch):
    def forbidden():
        raise AssertionError("Spoken confirmation must not reach send")

    monkeypatch.setattr("app.api.phone.MailAgent", forbidden)
    result = asyncio.run(phone_agent(AgentRequest(message="done", input_mode="voice",
        alternatives=[{"text": "done", "confidence": 1.0}], review_ticket="test-review")))
    assert result["state"] == "CONFIRMING"


def test_low_confidence_repair_asks_before_searching(monkeypatch):
    def forbidden():
        raise AssertionError("Low confidence must not search or draft")

    monkeypatch.setattr("app.api.phone.MailAgent", forbidden)
    result = asyncio.run(phone_agent(AgentRequest(message="male from Helix", input_mode="voice",
        alternatives=[{"text": "male from Helix", "confidence": 0.5}])))
    assert result["state"] == "NEEDS_CLARIFICATION"
    assert result["pending_question"] and result["evidence"] == []


def test_missing_gemini_config_returns_json_instead_of_500():
    class MissingConfigAgent:
        def run(self, *args, **kwargs):
            raise ValueError("GEMINI_API_KEY is required for Gmail analysis")

    result = handle_mail_request(MissingConfigAgent, "Check mail", "android")
    assert result["state"] == "DEGRADED"
    assert result["evidence"] == []
    assert "GEMINI_API_KEY" in result["answer"]
