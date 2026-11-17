from starlette.testclient import TestClient
from deploy.cloud_app import app
from deploy import chat_api
from app.core.auth import MailboxContext


def test_chat_page_and_public_status_do_not_claim_mail_access():
    client=TestClient(app)
    assert client.get('/chat').status_code==200
    assert client.get('/chat.js').status_code==200
    status=client.get('/chat/status').json()
    assert status['connected'] is False and status['read_only'] is True
    assert not any('secret' in key or 'token' in key for key in status)


def test_chat_requires_owner_and_same_origin():
    client=TestClient(app)
    data={'message':'Who sent it?','session_key':'x'*32}
    assert client.post('/chat/message',json=data,headers={'origin':'https://evil.example'}).status_code==403
    assert client.post('/chat/message',json=data,headers={'origin':'https://testserver'}).status_code==401


def test_authorized_chat_delegates_to_mail_agent_and_preserves_session(monkeypatch):
    async def owner(*args):return MailboxContext('u','w','m','microsoft','s')
    monkeypatch.setattr(chat_api,'require_mailbox_context',owner)
    monkeypatch.setattr(chat_api,'mail_agent_factory',lambda ctx:object())
    calls=[]
    def handle(factory,text,session,**kwargs):
        calls.append((text,session));return {'answer':'Verified answer','evidence':[{'message_id':'a'}],'raw_transcript':'private debug'}
    monkeypatch.setattr(chat_api,'handle_mail_request',handle)
    monkeypatch.setattr(chat_api,'get_settings',lambda:type('S',(),{'gemini_api_key':'synthetic'})())
    with TestClient(app) as client:
        for question in ['Find a message','Who sent it?']:
            result=client.post('/chat/message',json={'message':question,'session_key':'x'*32},headers={'origin':'https://testserver'})
            assert result.status_code==200 and 'raw_transcript' not in result.json()
    assert len(calls)==2 and calls[0][1]==calls[1][1]
