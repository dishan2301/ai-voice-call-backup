"""Offline mail-first acceptance. Scripted models test plumbing, not ASR quality."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.auth import MailboxContext
from app.database.models import AgentConversation
from app.voice.mail_first import MailFirstLive, LIVE_POLICY, mail_agent_factory
from app.voice.session import VoiceSession
from tests.test_mail_agent import make_agent, decision, FakeLLM


def test_factory_requires_server_context_and_bypasses_assistant_router(monkeypatch):
    with pytest.raises(ValueError):
        mail_agent_factory(None)
    agent, _ = make_agent([])
    context = MailboxContext('u', 'w', 'm')
    seen = []
    monkeypatch.setattr('app.api.dependencies.create_mail_agent', lambda c: seen.append(c) or agent)
    assert mail_agent_factory(context)().mail_first
    assert seen == [context]


@pytest.mark.parametrize('question', [
    'Zydus તરફથી આવેલા ઇમેઇલ શોધો',
    'Zydus ના payment emails બતાવો',
    'Zydus ke payment mail dikhao',
    'Zydus का भुगतान ईमेल पढ़ो',
])
def test_multilingual_request_reaches_shared_verified_retrieval_without_raw_storage(question):
    agent, factory = make_agent([
        decision(tool='search_mail', arguments={'query': 'from:client@example.com'}),
        decision(kind='final', answer='Please confirm.'),
    ])
    agent.mail_first = True
    prompts = []
    scripted = agent.llm
    class Capture:
        def generate_json(self, system, prompt, schema):
            prompts.append((system, prompt))
            return scripted.generate_json(system, prompt, schema)
    agent.llm = Capture()
    result = agent.run(question, 'multilingual')
    assert result['evidence'][0]['message_id'] == 'm1'
    assert any(question in json.loads(p).get('request', '') for _, p in prompts)
    assert any('ગુજરાતી' in s and 'EMAIL ONLY' in s for s, _ in prompts)
    with factory() as db:
        assert db.scalar(select(AgentConversation)).user_message == ''
    assert not agent.gmail.sent


@pytest.mark.parametrize('question', ['કાલે આવેલા મેલ', 'कल वाला मेल', 'kal wala mail', 'Find mail on 03/04'])
def test_ambiguous_dates_never_call_model_or_mail(question):
    agent, _ = make_agent([])
    agent.mail_first = True
    result = agent.run(question, 'dates')
    assert result['state'] == 'NEEDS_CLARIFICATION'
    assert not agent.gmail.queries


@pytest.mark.parametrize('tool', ['remember', 'approve_action', 'propose_risky_action', 'calendar', 'whatsapp'])
def test_model_cannot_escape_mail_tool_allowlist(tool):
    agent, factory = make_agent([decision(tool=tool, arguments={'action_type': 'DELETE'}),
                                decision(kind='final', answer='Which mail?')])
    agent.mail_first = True
    result = agent.run('Help with mail', 'tools')
    assert not agent.gmail.sent
    from app.database.models import AgentAction
    with factory() as db:
        assert not db.scalars(select(AgentAction)).all()


def test_setup_is_multilingual_mail_only_and_uses_m8_transport():
    from app.voice.m8_lifecycle import SingleAttemptConnect
    async def exercise():
        live = MailFirstLive(None, None, None, None)
        live.socket = AsyncMock()
        await live.send({'setup': {'tools': [{'mail_request': {}}], 'inputAudioTranscription': {'languageCodes': ['en-IN']}}})
        setup = json.loads(live.socket.send.call_args.args[0])['setup']
        assert setup['systemInstruction']['parts'][0]['text'] == LIVE_POLICY
        assert setup['inputAudioTranscription'] == {}
        assert setup['outputAudioTranscription'] == {}
        assert live.connector is SingleAttemptConnect
        assert live.mail_only and live._vocabulary() == []
    asyncio.run(exercise())


def test_voice_confirmation_and_failed_tool_are_safe(monkeypatch):
    async def inline(f, *a, **kw):
        return f(*a, **kw)
    monkeypatch.setattr('app.voice.session.asyncio.to_thread', inline)
    async def exercise():
        def fail():
            raise RuntimeError('private provider detail')
        voice = VoiceSession(lambda _: None, fail)
        assert (await voice.respond('send it'))['state'] == 'CONFIRMING'
        result = await voice.respond('મારા મેઇલ શોધો')
        assert result['state'] == 'ERROR' and result['evidence'] == []
        assert 'private' not in str(result)
        voice.close()
    asyncio.run(exercise())


def test_sip_mail_blocks_drafts_and_confirmation_at_the_agent():
    from app.database.models import AgentAction

    agent, factory = make_agent([
        decision(tool="prepare_draft", arguments={"to": "client@example.com", "body": "Hello"}),
        decision(kind="final", answer="Use the website to draft."),
    ])
    agent.mail_first = agent.sip_read_only = True
    assert agent.run("done", "sip")["state"] == "READ_ONLY"
    agent.run("Please draft a new message", "sip")
    assert not agent.gmail.sent
    with factory() as db:
        assert not db.scalars(select(AgentAction)).all()


def test_sip_voice_passes_only_short_answer_back_to_live():
    from scripts.sip_gmail import SipMailLive

    async def exercise():
        live = SipMailLive(None, None, None, None)
        live.socket = AsyncMock()
        await live.send({"toolResponse": {"functionResponses": [{
            "id": "1", "name": "mail_request", "response": {
                "spoken_answer": "You have one match.", "evidence": ["private source"],
                "raw_transcript": "private speech",
            },
        }]}})
        sent = json.loads(live.socket.send.call_args.args[0])
        response = sent["toolResponse"]["functionResponses"][0]["response"]
        assert response == {"spoken_answer": "You have one match."}

    asyncio.run(exercise())

@pytest.mark.parametrize('case', [
    'test_m3_contextual_followup_revalidates_active_gmail_thread',
    'test_m3_draft_selection_is_thread_aware_after_topic_switch',
    'test_agent_flags_wrong_sender_group_count_even_when_total_is_correct',
    'test_model_invented_recipient_cannot_become_a_draft',
    'test_done_sends_the_reviewed_draft_once',
    'test_review_expiry_and_low_confidence_block_send',
    'test_send_now_returns_exact_readback_and_never_sends',
    'test_revision_cannot_change_recipient_outside_verified_thread',
    'test_hinglish_send_now_is_review_only',
])
def test_existing_mail_safety_in_mail_first_mode(monkeypatch, case):
    from tests import test_mail_agent as suite
    original = suite.make_agent
    def mail_first_agent(*args, **kwargs):
        agent, factory = original(*args, **kwargs)
        agent.mail_first = True
        return agent, factory
    monkeypatch.setattr(suite, 'make_agent', mail_first_agent)
    getattr(suite, case)()


def test_unidentified_or_revoked_owner_never_reaches_mail():
    from fastapi import HTTPException
    agent, _ = make_agent([])
    agent.mail_first = True
    agent.mailbox_context = MailboxContext('missing', 'workspace', 'default', 'gmail', 'revoked')
    with pytest.raises(HTTPException):
        agent.run('મારા ઇમેઇલ વાંચો', 'unauthorized')
    assert not agent.gmail.queries and not agent.gmail.sent


@pytest.mark.parametrize('followup', ['उसमें क्या लिखा है?', 'Us mail mein kya likha hai?', 'તેમાં શું લખ્યું છે?'])
def test_multilingual_followup_reloads_verified_active_topic(followup):
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'Zydus'}),
        decision(kind='final', answer='Found your payment email.'),
        decision(tool='get_thread', arguments={'thread_id': 'thread-1'}),
        decision(kind='final', answer='Can we meet at 3 pm?'),
    ])
    agent.mail_first = True
    agent.run('Zydus ના mail શોધો', 'followup')
    result = agent.run(followup, 'followup')
    assert result['evidence'][0]['message_id'] == 'm1'
    assert agent.gmail.queries == ['Zydus']


def test_mail_first_preserves_names_addresses_and_dates_in_final_transcript(monkeypatch):
    async def inline(f, *a, **kw):
        return f(*a, **kw)
    monkeypatch.setattr('app.voice.session.asyncio.to_thread', inline)
    seen = []
    class Agent:
        def run(self, message, *a, **kw):
            seen.append(message)
            return {'answer': 'Checked.', 'spoken_answer': 'Checked.', 'evidence': []}
    async def exercise():
        voice = VoiceSession(lambda _: None, Agent)
        voice.preserve_literals = True
        text = 'May Patel ને may.patel@example.test ના 2026-11-17 mail વિશે પૂછો'
        await voice.respond(text)
        assert seen == [text]
    asyncio.run(exercise())


@pytest.mark.parametrize('body', ['આભાર.', 'धन्यवाद।', 'Thank you.'])
def test_requested_language_draft_is_preserved_exactly_and_never_sent(body):
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'Zydus'}),
        decision(tool='prepare_draft', arguments={'to': 'client@example.com', 'subject': 'Re: Payment',
                   'body': body, 'thread_id': 'thread-1'}),
        decision(kind='final', answer='Draft ready.'),
    ])
    agent.mail_first = True
    result = agent.run('Reply to client@example.com: ' + body, 'draft-language')
    assert result['draft']['body'] == body
    assert not agent.gmail.sent


def test_mail_refresh_uses_captured_owner_context(monkeypatch):
    from types import SimpleNamespace
    context = MailboxContext('local', 'local', 'default', 'gmail', legacy_local=True)
    agent, _ = make_agent([decision(tool='sync_mail'), decision(kind='final', answer='Refreshed.')])
    agent.mail_first = True
    agent.mailbox_context = context
    seen = []
    def generation(c):
        seen.append(c)
        return SimpleNamespace(generate=lambda _: SimpleNamespace(total_emails=0))
    monkeypatch.setattr('app.api.dependencies.create_generation_service', generation)
    agent.run('Sync my mail', 'refresh')
    assert seen == [context]
