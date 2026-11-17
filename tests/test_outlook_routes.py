"""Actual OAuth transaction, encrypted login/session and Outlook mail-tool routes, synthetic grants."""
import asyncio
from datetime import datetime,timedelta,timezone
from urllib.parse import parse_qs,urlsplit
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select
from app.api import microsoft_oauth as routes
from app.api import dependencies
from app.core import auth
from app.core.config import Settings
from app.database.models import MailboxConnection,AuthSession
from app.main import app
from tests.test_hosted_auth import hosted
from tests.test_outlook import grant


@pytest.fixture
def outlook_app(hosted,monkeypatch,tmp_path):
    directory=tmp_path/'microsoft-keys';directory.mkdir(mode=0o700)
    key=directory/'1.key';key.write_bytes(b'x'*32);key.chmod(0o600)
    settings=Settings(gemini_api_key='synthetic-key',deployment_mode='hosted',allowed_hosts=['test'],microsoft_enabled=True,
        microsoft_client_id='synthetic-client',microsoft_client_secret='synthetic-secret',
        microsoft_redirect_uri='https://test/auth/microsoft/callback',microsoft_allowed_emails=['owner@example.test'],
        provider_credential_key_directory=str(directory),provider_credential_key_version=1)
    for target in ['app.api.microsoft_oauth.get_settings','app.core.auth.get_settings','app.core.security.get_settings',
                   'app.api.dependencies.get_settings','app.api.dependencies.get_runtime_settings']:
        monkeypatch.setattr(target,lambda:settings)
    monkeypatch.setattr(dependencies,'SessionLocal',hosted)
    async def inline(fn,*a,**kw):return fn(*a,**kw)
    monkeypatch.setattr(routes.asyncio,'to_thread',inline)
    monkeypatch.setattr(routes,'exchange_code',lambda *a:grant())
    return hosted,settings


async def login(client):
    started=await client.get('/auth/microsoft/start');assert started.status_code==307
    state=parse_qs(urlsplit(started.headers['location']).query)['state'][0]
    connected=await client.get('/auth/microsoft/callback',params={'state':state,'code':'synthetic-code'})
    assert connected.status_code==200,connected.text
    return state,connected


def test_pkce_login_replay_encryption_disconnect(outlook_app):
    sessions,settings=outlook_app
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            state,result=await login(client)
            assert result.json()['email']=='owner@example.test' and 'access_token' not in result.text
            assert 'HttpOnly' in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
            replay=await client.get('/auth/microsoft/callback',params={'state':state,'code':'synthetic-code'})
            assert replay.status_code==400
            status=await client.get('/auth/microsoft/status');assert status.status_code==200
            with sessions() as db:
                row=db.scalar(select(MailboxConnection).where(MailboxConnection.provider=='microsoft'))
                assert row and row.credential_ciphertext and 'synthetic-access' not in row.credential_ciphertext
                assert row.verified_sender=='owner@example.test'
            disconnected=await client.delete('/auth/microsoft/connection');assert disconnected.status_code==200
            assert (await client.get('/auth/microsoft/status')).status_code==401
            with sessions() as db:
                row=db.scalar(select(MailboxConnection).where(MailboxConnection.provider=='microsoft'))
                assert row.status=='REVOKED' and not row.credential_ciphertext
    asyncio.run(exercise())


def test_state_and_browser_binding_fail_before_exchange(outlook_app,monkeypatch):
    monkeypatch.setattr(routes,'exchange_code',lambda *a:pytest.fail('Invalid state must not exchange a token'))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            start=await client.get('/auth/microsoft/start')
            state=parse_qs(urlsplit(start.headers['location']).query)['state'][0]
            client.cookies.clear()
            response=await client.get('/auth/microsoft/callback',params={'state':state,'code':'synthetic-code'})
            assert response.status_code==400
            assert (await client.post('/auth/microsoft/search',json={'text':'Invoice'})).status_code==401
    asyncio.run(exercise())


def test_first_retrieval_is_owner_scoped_and_never_constructs_llm(outlook_app,monkeypatch):
    seen=[]
    def provider(context):
        assert context.provider=='microsoft' and not context.legacy_local
        seen.append(context)
        return SimpleNamespace(search_spec=lambda spec:{'messages':[{'message_id':'synthetic-m1'}],'completeness':{'complete':False}})
    monkeypatch.setattr(dependencies,'create_outlook_provider',provider)
    monkeypatch.setattr('app.llm.gemini.GeminiProvider.generate_json',lambda *a:pytest.fail('First read must not call Gemini'))
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            await login(client)
            result=await client.post('/auth/microsoft/search',json={'text':'statement','limit':3})
            assert result.status_code==200 and result.json()['messages'][0]['message_id']=='synthetic-m1'
            assert result.headers['cache-control']=='no-store'
            assert (await client.post('/auth/microsoft/search',json={'mailbox_id':'foreign'})).status_code==422
    asyncio.run(exercise());assert len(seen)==1


def test_foreign_provider_session_cannot_search_outlook(outlook_app):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            result=await client.post('/auth/microsoft/search',json={'text':'secret'},headers={'Authorization':'Bearer alice-token'})
            assert result.status_code==403
    asyncio.run(exercise())


def test_microsoft_provider_factory_loads_owned_encrypted_grant(outlook_app):
    sessions,settings=outlook_app
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
            await login(client)
    asyncio.run(exercise())
    with sessions() as db:
        connection=db.scalar(select(MailboxConnection).where(MailboxConnection.provider=='microsoft'))
        session=db.scalar(select(AuthSession).where(AuthSession.mailbox_id==connection.id))
        context=auth.MailboxContext(connection.owner_user_id,connection.workspace_id,connection.id,'microsoft',session.id)
    provider=dependencies.create_outlook_provider(context)
    assert provider._token()=='synthetic-access'
    agent=dependencies.create_mail_agent(context)
    assert agent.settings.email_provider=='microsoft'
    assert agent.run('done','outlook-readonly')['state']=='READ_ONLY'
    with sessions() as db:
        db.get(AuthSession,context.session_id).revoked_at=datetime.utcnow();db.commit()
    with pytest.raises(Exception):provider._token()
