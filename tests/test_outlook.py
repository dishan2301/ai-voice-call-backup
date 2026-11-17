"""Provider mocked, personal-account Outlook tests; never uses real consent or mail."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

import httpx
import pytest
from app.core.config import Settings
from app.email.microsoft_oauth import authorization_url,exchange_code,refresh_grant,checked_scopes,MicrosoftError,SCOPES
from app.email.outlook import OutlookProvider,BASE
from app.retrieval.query import SearchSpec


def settings():
    return Settings(microsoft_enabled=True,microsoft_client_id='synthetic-client',microsoft_client_secret='synthetic-secret',
        microsoft_redirect_uri='https://test/auth/microsoft/callback',microsoft_allowed_emails=['owner@example.test'])


def grant():
    return dict(access_token='synthetic-access',refresh_token='synthetic-refresh',scope=' '.join(SCOPES),subject='synthetic-sub',
                email='owner@example.test',expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())


def message(mid='m1',**updates):
    return dict(id=mid,conversationId='thread-1',internetMessageId='<m1@example.test>',subject='Maintenance statement',
        **{'from':{'emailAddress':{'name':'Support','address':'support@example.test'}}},
        toRecipients=[{'emailAddress':{'address':'owner@example.test'}}],ccRecipients=[],
        receivedDateTime='2026-11-17T05:00:00Z',isRead=False,hasAttachments=False,
        body={'contentType':'text','content':'Please review the maintenance statement.'},**updates)


def provider(handler,load=None,save=None,authorize=None):
    return OutlookProvider(settings(),load or (lambda:(grant(),'cipher')),save or (lambda *a:None),authorize or (lambda:None),
        'synthetic-sub','owner@example.test',transport=httpx.MockTransport(handler))


def test_authorization_is_personal_pkce_and_readonly():
    url=authorization_url(settings(),'synthetic-state','v'*64);query=parse_qs(urlsplit(url).query)
    assert '/consumers/' in url and query['response_type']==['code'] and query['code_challenge_method']==['S256']
    assert query['scope']==[' '.join(SCOPES)]
    assert not {'Mail.Send','Mail.ReadWrite','User.Read'} & set(query['scope'][0].split())
    assert 'v'*64 not in url


@pytest.mark.parametrize('scope',['Mail.ReadWrite','Mail.Read Mail.Send','Mail.Read User.Read','openid email'])
def test_overbroad_or_missing_read_permission_is_rejected(scope):
    with pytest.raises(MicrosoftError):checked_scopes(scope)


@pytest.mark.parametrize('identity',[{'sub':'subject','email':'owner@example.test'},{'sub':'subject','email':'other@example.test'},{'sub':'subject'}])
def test_exchange_uses_bound_code_verifier_and_verified_userinfo(identity):
    def handler(request):
        if str(request.url).endswith('/token'):
            data=parse_qs(request.content.decode())
            assert data['code_verifier']==['v'*64] and data['grant_type']==['authorization_code']
            return httpx.Response(200,json={'access_token':'access','refresh_token':'refresh','token_type':'Bearer','expires_in':3600,'scope':'Mail.Read openid profile email'})
        assert str(request.url)=='https://graph.microsoft.com/oidc/userinfo'
        return httpx.Response(200,json=identity)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if identity.get('email')=='owner@example.test':
            value=exchange_code(settings(),'code','v'*64,client=client)
            assert value['subject']=='subject' and value['email']=='owner@example.test'
        else:
            with pytest.raises(MicrosoftError):exchange_code(settings(),'code','v'*64,client=client)


def test_refresh_binds_identity_and_rotates_token():
    old=grant()
    def handler(request):
        if str(request.url).endswith('/token'):
            data=parse_qs(request.content.decode());assert data['grant_type']==['refresh_token']
            return httpx.Response(200,json={'access_token':'rotated','refresh_token':'new-refresh','token_type':'Bearer','expires_in':3600,'scope':'Mail.Read'})
        return httpx.Response(200,json={'sub':old['subject'],'email':old['email']})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        value=refresh_grant(settings(),old,client=client)
        assert value['access_token']=='rotated' and value['refresh_token']=='new-refresh'


def test_provider_refresh_is_persisted_with_compare_and_swap(monkeypatch):
    old={**grant(),'expires_at':datetime.now(timezone.utc).isoformat()};saved=[]
    monkeypatch.setattr('app.email.outlook.refresh_grant',lambda *a:{**grant(),'access_token':'fresh'})
    def handler(request):
        assert request.headers['authorization']=='Bearer fresh'
        return httpx.Response(200,json=message())
    p=provider(handler,load=lambda:(old,'expected-cipher'),save=lambda *a:saved.append(a))
    assert p.get_message('m1')['message_id']=='m1' and saved[0][1]=='expected-cipher'


def test_graph_reads_verify_originals_and_preserve_hard_filters():
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.method=='GET' and '/me/messages' in request.url.path
        assert 'ImmutableId' in request.headers['prefer']
        if request.url.path.endswith('/messages'):return httpx.Response(200,json={'value':[{'id':'m1'}]})
        return httpx.Response(200,json=message())
    p=provider(handler)
    response=p.search_spec(SearchSpec(sender='support@example.test',recipient='owner@example.test',text='maintenance',
        start=datetime(2026,11,17,tzinfo=timezone.utc),unread=True))
    assert response['messages'][0]['provider_verified'] and response['messages'][0]['provider']=='microsoft'
    assert not response['completeness']['complete']
    assert 'from/emailAddress/address' in requests[0].url.params['$filter']
    assert requests[1].url.path.endswith('/m1')


@pytest.mark.parametrize('token',['https://attacker.example.test/steal','https://graph.microsoft.com/v1.0/users/other/messages','https://graph.microsoft.com/v1.0/me/sendMail'])
def test_continuation_cannot_redirect_tokens_or_select_foreign_owner(token):
    p=provider(lambda r:pytest.fail('Untrusted URL must be rejected before a network request'))
    with pytest.raises(MicrosoftError):p.search_page('invoice',page_token=token)


def test_canonical_thread_rejects_wrong_sender_and_send_is_impossible():
    def handler(request):
        if request.url.path.endswith('/messages'):return httpx.Response(200,json={'value':[{'id':'m1'}]})
        return httpx.Response(200,json=message())
    p=provider(handler)
    assert not p.search_spec(SearchSpec(sender='other@example.test'))['messages']
    assert p.get_thread('thread-1')[0]['thread_id']=='thread-1'
    with pytest.raises(MicrosoftError):p.send_message({'to':'attacker@example.test'})


@pytest.mark.parametrize('status',[401,403,429,503])
def test_failures_do_not_turn_into_empty_success(status):
    p=provider(lambda r:httpx.Response(status,json={'error':{'message':'PRIVATE_ERROR'}}))
    with pytest.raises(MicrosoftError) as exc:p.get_message('m1')
    assert 'PRIVATE_ERROR' not in str(exc.value) and 'synthetic-access' not in str(exc.value)


def test_authorization_is_checked_before_and_after_each_provider_read():
    checks=[]
    p=provider(lambda r:httpx.Response(200,json=message()),authorize=lambda:checks.append(True))
    p.get_message('m1');assert len(checks)==2


def test_callback_log_filter_does_not_leak_authorization_code():
    import logging
    from app.core.logging import OAuthCallbackFilter
    record=logging.LogRecord('httpx',logging.INFO,'',1,'HTTP GET %s',('https://test/auth/microsoft/callback?code=PRIVATE_CODE&state=PRIVATE_STATE',),None)
    OAuthCallbackFilter().filter(record)
    assert 'PRIVATE_CODE' not in record.getMessage() and 'PRIVATE_STATE' not in record.getMessage()


def test_mail_agent_and_mock_voice_delegate_to_owned_outlook_provider(monkeypatch):
    from tests.test_mail_agent import make_agent,decision
    from app.voice.session import VoiceSession
    from app.voice.mail_first import MailFirstLive
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/messages'):return httpx.Response(200,json={'value':[{'id':'m1'}]})
        return httpx.Response(200,json=message())
    p=provider(handler)
    agent,_=make_agent([decision(tool='search_history',arguments={'text':'maintenance','sender':'support@example.test'}),
                       decision(kind='final',answer='Please review the maintenance statement.')])
    agent.gmail=p
    agent.settings=agent.settings.model_copy(update={'email_provider':'microsoft','retrieval_adaptive_enabled':True})
    agent.mail_first=True
    async def inline(fn,*a,**kw):return fn(*a,**kw)
    monkeypatch.setattr('app.voice.session.asyncio.to_thread',inline)
    async def exercise():
        voice=VoiceSession(lambda e:None,lambda:agent)
        voice.preserve_literals=True
        live=MailFirstLive(None,voice,lambda pcm:None,lambda:None)
        responses=[]
        class Socket:
            async def send(self,raw):
                responses.append(json.loads(raw));live.audio_chunks_received+=1;live.turn_complete.set()
        live.socket=Socket()
        await live.delegate({'name':'mail_request','id':'outlook-test','args':{'mailbox_id':'foreign'}},'Find the maintenance statement')
        result=responses[0]['toolResponse']['functionResponses'][0]['response']
        assert result['evidence'][0]['message_id']=='m1' and result['evidence'][0]['provider']=='microsoft'
        assert result['answer'].startswith('Please review the maintenance statement.')
        assert 'incomplete' in result['answer'].lower()
        assert (await voice.respond('send it'))['state']=='CONFIRMING'
        voice.close()
    asyncio.run(exercise())
    assert calls and all(r.method=='GET' and '/me/messages' in r.url.path for r in calls)


def test_graph_search_url_is_redacted_in_logs():
    import logging
    from app.core.logging import OAuthCallbackFilter
    record=logging.LogRecord('httpx',logging.INFO,'',1,'GET %s',('https://graph.microsoft.com/v1.0/me/messages?$search=PRIVATE_BODY_PHRASE',),None)
    OAuthCallbackFilter().filter(record)
    assert 'PRIVATE_BODY_PHRASE' not in record.getMessage()


def test_continuation_preserves_filters_and_bounded_id_projection():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'value':[]})
    p=provider(handler)
    p.search_page('from:support@example.test',limit=2,page_token=BASE+'?$skip=20&$top=999&$select=body')
    query=calls[0].url.params
    assert query['$skip']=='20' and query['$top']=='20' and query['$select']=='id'
    assert 'support@example.test' in query['$filter']
