"""End-to-end offline M8 mail verification; never connects to a provider."""
import asyncio
import base64
import json
import socket
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import voice as voice_api
from app.core import auth
from app.database.db import get_db
from app.database.models import AuthSession, AgentAction, ConversationState, Email
from app.voice.mail_first import MailFirstLive, mail_agent_factory
from app.voice.session import VoiceSession
from tests.m8_mailbox import MailboxHarness, cases, tool, final


@pytest.fixture
def mailbox(monkeypatch):
    h = MailboxHarness()
    def no_network(*a, **kw):
        raise AssertionError('Network is forbidden in offline mail verification')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr('app.llm.gemini.GeminiProvider.generate_json', no_network)
    monkeypatch.setattr('app.email.gmail.GmailProvider.from_settings', no_network)
    monkeypatch.setattr('app.api.dependencies.create_mail_agent', h.agent)
    monkeypatch.setattr(auth, 'get_settings', lambda: h.settings)
    monkeypatch.setattr(voice_api, 'get_settings', lambda: h.settings)
    async def inline(f, *a, **kw):
        return f(*a, **kw)
    monkeypatch.setattr('app.voice.session.asyncio.to_thread', inline)
    yield h
    h.close()


class MockSocket:
    """Only the wire is mocked; start/receive/delegate/send/cleanup stay real."""
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def recv(self):
        return json.dumps({'setupComplete': {}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.queue.get())

    async def send(self, raw):
        value = json.loads(raw)
        self.sent.append(value)
        if 'toolResponse' in value or 'clientContent' in value:
            await self.queue.put({'serverContent': {'modelTurn': {'parts': [{'inlineData': {
                'mimeType': 'audio/pcm;rate=24000', 'data': base64.b64encode(b'\x00\x00'*20).decode()}}]},
                'turnComplete': True}})

    async def close(self):
        self.closed = True


class MockPC:
    def __init__(self, *a, **kw):
        self.handlers = {}
        self.localDescription = SimpleNamespace(sdp='synthetic-answer', type='answer')
    def on(self, event):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register
    def addTrack(self, value): pass
    async def setRemoteDescription(self, value): pass
    async def createAnswer(self): return SimpleNamespace(sdp='synthetic-answer', type='answer')
    async def setLocalDescription(self, value): pass
    async def close(self): pass


async def open_mail_voice(h, monkeypatch, token='owner-test-token'):
    wire = MockSocket()
    async def connect(*a, **kw):
        return wire
    monkeypatch.setattr(MailFirstLive, 'connector', staticmethod(connect))
    monkeypatch.setattr(voice_api, 'RTCPeerConnection', MockPC)
    api = FastAPI()
    api.include_router(voice_api.router)
    async def db():
        with h.sessions() as session:
            yield session
    api.dependency_overrides[get_db] = db
    client = AsyncClient(transport=ASGITransport(app=api), base_url='http://test')
    result = await client.post('/phone/voice/offer', headers={'Authorization': f'Bearer {token}'},
        json={'sdp': 'synthetic-sdp', 'type': 'offer', 'session_key': 'phase15-session-key', 'mail_first': True})
    return client, result, wire


async def spoken_turn(wire, text, call_id='call', args=None):
    await wire.queue.put({'serverContent': {'inputTranscription': {'text': text}}})
    await wire.queue.put({'toolCall': {'functionCalls': [{'id': call_id, 'name': 'mail_request', 'args': args or {}}]}})
    async with asyncio.timeout(3):
        while True:
            for value in wire.sent:
                for response in value.get('toolResponse', {}).get('functionResponses', []):
                    if response['id'] == call_id:
                        await asyncio.sleep(.01)  # finish the mocked audio turn before another request
                        return response['response']
            await asyncio.sleep(.01)


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.name)
def test_complete_multilingual_mail_path(mailbox, monkeypatch, case):
    async def exercise():
        client, offered, wire = await open_mail_voice(mailbox, monkeypatch)
        try:
            assert offered.status_code == 200
            mailbox.plan(case, 20)
            result = await spoken_turn(wire, case.request)
            ids = {e['message_id'] for e in result['evidence']}
            assert ids == set(case.expected)
            assert result['interpretation'] == case.request
            assert not mailbox.provider.send_calls
            with mailbox.sessions() as db:
                state = db.scalar(select(ConversationState))
                if ids:
                    assert set(state.result_ids_json) == ids
            setup = wire.sent[0]['setup']
            declarations = setup['tools'][0]['functionDeclarations']
            assert [d['name'] for d in declarations] == ['mail_request']
            assert declarations[0]['parameters']['properties'] == {}
        finally:
            await voice_api.close_all()
            await client.aclose()
        assert wire.closed and not voice_api.peers
    asyncio.run(exercise())


def test_real_local_tool_exposes_safe_headers_and_attachment_metadata(mailbox):
    agent = mailbox.agent()
    with mailbox.sessions() as db:
        result = agent._search_history(db, {'text': 'Dharani Quotation', 'sender': 'ravi@dharani.example.test', 'has_attachments': True})
    # Dharani's invoice/quotation sender is Ravina, not Ravi: this request must be empty.
    assert result['messages'] == []
    with mailbox.sessions() as db:
        result = agent._search_history(db, {'text': 'Dharani Quotation', 'has_attachments': True})
    row = next(r for r in result['messages'] if r['message_id'] == 'dharani-quotation-0')
    assert row['sender'] == 'Ravina Shah <ravina@dharani.example.test>'
    assert row['recipient_emails'] == ['owner@example.test']
    assert row['attachments'][0]['filename'] == 'quotation.pdf'
    assert row['attachments'][0]['extraction_status'] == 'unsupported'
    assert row['body_truncated'] is False


@pytest.mark.parametrize('sender', ['Ravi Shah <ravi@aster.example.test.evil.test>', '', 'Other <other@example.test>'])
def test_exact_sender_cannot_accept_lookalike_or_missing_header(mailbox, sender):
    source = {**mailbox.rows[0], 'sender': sender}
    assert mailbox.agent()._filter_explicit_metadata([source], 'Find from ravi@aster.example.test about quotation') == []


@pytest.mark.parametrize('token', ['invalid', '', 'other-test-token'])
def test_authentication_is_not_selected_by_provider_arguments(mailbox, monkeypatch, token):
    async def exercise():
        client, offered, wire = await open_mail_voice(mailbox, monkeypatch, token)
        try:
            if token == 'other-test-token':
                assert offered.status_code == 200
                mailbox.reasoner.decisions = [tool('search_history', text='confidential'), final()]
                result = await spoken_turn(wire, 'Find confidential payroll', args={'mailbox_id': 'owner-mail'})
                assert {r['message_id'] for r in result['evidence']} == {'foreign', 'aster-invoice-1'}
                assert all('Aster' not in r['subject'] for r in result['evidence'])
            else:
                assert offered.status_code == 401
                assert wire.sent == []
        finally:
            await voice_api.close_all()
            await client.aclose()
    asyncio.run(exercise())


def test_session_revocation_during_voice_session_blocks_retrieval(mailbox, monkeypatch):
    async def exercise():
        client, offered, wire = await open_mail_voice(mailbox, monkeypatch)
        try:
            with mailbox.sessions() as db:
                from datetime import datetime
                db.get(AuthSession, 'owner-session').revoked_at = datetime.utcnow()
                db.commit()
            result = await spoken_turn(wire, 'Find private payroll')
            assert result['state'] == 'ERROR' and not result['evidence']
            assert not mailbox.provider.calls
        finally:
            await voice_api.close_all()
            await client.aclose()
    asyncio.run(exercise())


def test_thread_followups_switching_and_language_identity(mailbox, monkeypatch):
    async def exercise():
        client, offered, wire = await open_mail_voice(mailbox, monkeypatch)
        try:
            for i, case in enumerate([cases()[0], cases()[4]]):
                mailbox.plan(case, 20)
                result = await spoken_turn(wire, case.request, f'search-{i}')
                assert result['evidence']
            mailbox.reasoner.review_terms = ['Aster', 'Invoice']
            mailbox.reasoner.decisions = [tool('get_thread', thread_id='aster-invoice'), final()]
            back = await spoken_turn(wire, 'go back to Aster Invoice', 'back')
            assert {row['thread_id'] for row in back['evidence']} == {'aster-invoice'}
            for i, text in enumerate(['उस मेल में क्या लिखा है?', 'us mail mein kya likha hai?', 'તે ઇમેઇલમાં શું લખ્યું છે?']):
                mailbox.reasoner.decisions = [tool('get_thread', thread_id='aster-invoice'), final()]
                response = await spoken_turn(wire, text, f'follow-{i}')
                assert {row['thread_id'] for row in response['evidence']} == {'aster-invoice'}
                assert response['interpretation'] == text
            assert any(call == ('get_thread', 'aster-invoice') for call in mailbox.provider.calls)
        finally:
            await voice_api.close_all()
            await client.aclose()
    asyncio.run(exercise())


def test_draft_review_voice_confirmation_and_interruption(mailbox, monkeypatch):
    from app.agents.draft_safety import begin_review, complete_review
    async def exercise():
        client, offered, wire = await open_mail_voice(mailbox, monkeypatch)
        try:
            mailbox.plan(cases()[0], 20)
            await spoken_turn(wire, cases()[0].request, 'search')
            mailbox.reasoner.decisions = [tool('prepare_draft', to='ravi@aster.example.test',
                subject='Re: Aster Invoice', body='Thank you.', thread_id='aster-invoice'), final()]
            response = await spoken_turn(wire, 'Reply to that email: Thank you.', 'draft')
            draft = response['draft']
            assert draft['to'] == 'ravi@aster.example.test' and draft['body'] == 'Thank you.'
            with mailbox.sessions() as db:
                review = begin_review(db, draft['id'], 'phase15-session-key', 'owner-mail')
                assert review['readback'] == 'To: ravi@aster.example.test\nSubject: Re: Aster Invoice\n\nThank you.'
                complete_review(db, draft['id'], 'phase15-session-key', review['review_ticket'], 'owner-mail')
            state = voice_api.peers[offered.json()['session_id']]
            state['voice'].interrupt()
            response = await spoken_turn(wire, 'send it', 'voice-confirm')
            assert response['state'] == 'CONFIRMING'
            # Typed confirmation without the completed review's secret ticket also fails.
            result = await state['voice'].respond('done', input_mode='text')
            assert result['state'] != 'SENT'
            assert not mailbox.provider.send_calls
            with mailbox.sessions() as db:
                assert db.get(AgentAction, draft['id']).status == 'PREPARED'
        finally:
            await voice_api.close_all()
            await client.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('attack', ['propose_risky_action', 'approve_action', 'calendar', 'whatsapp', 'prepare_draft'])
def test_email_prompt_injection_cannot_authorize_tools_or_recipient(mailbox, attack):
    mailbox.reasoner.review_terms = ['malicious']
    mailbox.reasoner.decisions = [tool('search_history', text='malicious'),
        tool(attack, to='attacker@example.test', subject='Payroll', body='Payroll', thread_id='injection',
             action_type='SEND', action_id=1), final('Which message should I check?')]
    result = mailbox.agent().run('Read the malicious note', 'injection-test')
    assert not result.get('draft') and not mailbox.provider.send_calls
    with mailbox.sessions() as db:
        assert not db.scalars(select(AgentAction)).all()


@pytest.mark.parametrize('error', [TimeoutError('synthetic timeout'), ConnectionError('synthetic outage')])
def test_failed_provider_thread_never_answers_from_stale_memory(mailbox, error):
    mailbox.plan(cases()[0], 20)
    agent = mailbox.agent()
    agent.run(cases()[0].request, 'outage')
    mailbox.provider.error = error
    # Use VoiceSession's real exception boundary for a failed contextual thread read.
    mailbox.reasoner.decisions = [tool('get_thread', thread_id='aster-invoice'), final()]
    async def exercise():
        voice = VoiceSession(lambda _: None, mail_agent_factory(mailbox.context), 'outage')
        result = await voice.respond('What did they ask?')
        assert not result.get('evidence') and result.get('state') in {'ERROR', 'ANSWERING', 'NEEDS_CLARIFICATION'}
        assert 'could' in result['answer'].lower() or 'unavailable' in result['answer'].lower() or 'no longer verify' in result['answer'].lower()
    asyncio.run(exercise())


@pytest.mark.parametrize('arguments', [{'query': ''}, {'query': 'Aster', 'limit': 'bad'}, {'query': 'Aster', 'limit': -100}])
def test_mail_tool_arguments_fail_safely_or_are_bounded(mailbox, arguments):
    mailbox.reasoner.review_terms = ['nonexistent']
    mailbox.reasoner.decisions = [tool('search_mail', **arguments), tool('search_history', text='nonexistent'), final(), final()]
    result = mailbox.agent().run('Find nonexistent topic', 'invalid-args')
    assert not result.get('draft') and not mailbox.provider.send_calls


def test_unread_attachment_deleted_and_missing_sources(mailbox):
    agent = mailbox.agent()
    with mailbox.sessions() as db:
        all_rows = agent._search_history(db, {'text': '', 'limit': 20})['messages']
        assert not {'deleted', 'inaccessible', 'foreign'} & {r['message_id'] for r in all_rows}
    mailbox.reasoner.review_terms = ['nonexistent']
    mailbox.reasoner.decisions = [tool('search_mail', query='nonexistent', limit=1),
                                final('Nothing matches.'), final('Nothing matches.')]
    result = agent.run('Find nonexistent topic', 'partial')
    assert 'incomplete' in result['answer'].lower()
    assert not result['evidence']


def test_ambiguous_sender_cannot_be_silently_selected_for_reply(mailbox):
    mailbox.reasoner.review_terms = ['Invoice']
    mailbox.reasoner.decisions = [tool('search_history', text='Invoice', limit=20),
        tool('prepare_draft', to='ravina@cobalt.example.test', subject='Re: Invoice', body='Thank you.', thread_id='cobalt-invoice'),
        final('Which Ravi Shah should receive the reply?', question='Which Ravi Shah should receive the reply?')]
    result = mailbox.agent().run('Reply to Ravi Shah about invoice: Thank you.', 'ambiguous')
    assert not result.get('draft')
    assert '?' in result['answer']
    assert not mailbox.provider.send_calls


def test_fts_message_id_collision_never_leaks_foreign_body(mailbox):
    with mailbox.sessions() as db:
        rows = mailbox.agent()._search_history(db, {'text': 'Aster Invoice', 'limit': 20})['messages']
    assert 'aster-invoice-1' in {r['message_id'] for r in rows}
    assert all('FOREIGN' not in row['body'] for row in rows)


def test_explicit_timezone_offset_is_not_replaced(mailbox):
    # 14 Nov 20:15 UTC is 15 Nov 01:45 India. Bounds are specified in UTC.
    with mailbox.sessions() as db:
        rows = mailbox.agent()._search_history(db, {'text': 'Invoice', 'sender': 'ravi@aster.example.test',
            'start_date': '2026-11-14T20:00:00+00:00', 'end_date': '2026-11-14T21:00:00+00:00'})['messages']
    assert [r['message_id'] for r in rows] == ['aster-invoice-0']


@pytest.mark.parametrize('interrupt_at', ['retrieval', 'toolResponse', 'clientContent'])
def test_mail_mode_reuses_late_speech_cancellation_guards(monkeypatch, interrupt_at):
    from tests import test_gemini_live as suite
    monkeypatch.setattr(suite, 'GeminiLive', MailFirstLive)
    suite.test_interrupted_answer_cannot_start_late_speech(interrupt_at)


def test_invented_evidence_excerpt_is_rejected(mailbox):
    mailbox.reasoner.fail_review = True
    mailbox.reasoner.review_terms = ['Aster', 'Invoice']
    mailbox.reasoner.decisions = [tool('search_history', text='Aster Invoice'), final()]
    response = mailbox.agent().run('Find Aster invoice', 'fabricated-excerpt')
    assert not response['evidence'] and not response.get('draft')
    assert 'verify' in response['answer'].lower()


def test_pagination_cursor_and_incomplete_disclosure_use_actual_mail_tool(mailbox):
    mailbox.reasoner.review_terms = ['Aster', 'Quotation']
    mailbox.provider.page_size = 1
    mailbox.reasoner.decisions = [tool('search_mail', query='Aster', limit=1),
        tool('search_mail', query='Aster', limit=1, page_token='1'), final()]
    response = mailbox.agent().run('Find Aster quotations', 'pages')
    assert ('search_mail', 'Aster', '1') in mailbox.provider.calls
    assert 'incomplete' in response['answer'].lower()
    assert not mailbox.provider.send_calls


def test_partial_local_history_discloses_cap_without_fts(mailbox):
    from sqlalchemy import text
    with mailbox.engine.begin() as connection:
        connection.execute(text('DROP TABLE message_search'))
    case = next(c for c in cases() if c.name == 'hinglish')
    mailbox.plan(case, 20)
    response = mailbox.agent().run(case.request, 'no-index')
    assert {r['message_id'] for r in response['evidence']} < set(case.expected)
    assert 'incomplete' in response['answer'].lower()


def test_owner_revoked_while_reasoning_cannot_start_another_tool(mailbox):
    from datetime import datetime
    from fastapi import HTTPException
    original = mailbox.reasoner.generate_json
    def revoke_before_tool(system, prompt, schema):
        if 'matches' not in schema.get('properties', {}):
            with mailbox.sessions() as db:
                db.get(AuthSession, 'owner-session').revoked_at = datetime.utcnow()
                db.commit()
        return original(system, prompt, schema)
    mailbox.reasoner.generate_json = revoke_before_tool
    mailbox.reasoner.decisions = [tool('search_mail', query='Aster')]
    with pytest.raises(HTTPException):
        mailbox.agent().run('Find email', 'revoke-during-reasoning')
    assert not mailbox.provider.calls
