import pytest
from app.voice.outlook_conversation import OutlookTools

class Provider:
    def __init__(self): self.reads=0; self.allowed=True; self.params=None
    def authorize(self):
        if not self.allowed: raise PermissionError()
    def _get(self,url,params):
        self.reads+=1;self.params=params
        return {'value':[{'id':'m1','subject':'Test','bodyPreview':'Preview'}]}
    def get_message(self,mid):
        self.reads+=1
        return {'message_id':mid,'body':'Full body'}

def test_metadata_cache_and_expiry():
    p=Provider();now=[0];tools=OutlookTools(p,lambda:now[0])
    assert not tools.execute('search_mail',{})['full_bodies_loaded']
    assert 'body,' not in p.params['$select']
    assert tools.execute('search_mail',{})['cached'] and p.reads==1
    now[0]=61
    assert not tools.execute('search_mail',{})['cached'] and p.reads==2
    tools.execute('search_mail',{'refresh':True});assert p.reads==3

def test_read_reference_and_revocation():
    p=Provider();tools=OutlookTools(p)
    with pytest.raises(ValueError):tools.execute('read_mail',{'id':'foreign'})
    tools.execute('search_mail',{})
    assert tools.execute('read_mail',{'id':'m1'})['message']['body']=='Full body'
    p.allowed=False
    with pytest.raises(PermissionError):tools.execute('read_mail',{'id':'m1'})
    tools.clear();assert not tools.cache and not tools.references

def test_mutations_are_not_tools():
    with pytest.raises(ValueError):OutlookTools(Provider()).execute('send_mail',{})

import asyncio
import json
from types import SimpleNamespace
from app.voice.outlook_conversation import OutlookConversationLive, ProviderFailure, ConversationLedger

class Voice:
    closed=False
    epoch=0
    def __init__(self): self.events=[];self.interrupts=0
    def event(self,*args,**kwargs): self.events.append((args,kwargs))
    def interrupt(self): self.interrupts+=1;self.epoch+=1

def live_instance():
    settings=SimpleNamespace(gemini_live_model='primary',gemini_live_fallback_models=['backup'])
    audio=[]
    live=OutlookConversationLive(settings,Voice(),audio.append,lambda:audio.clear(),Provider())
    return live,audio

def test_quota_switches_model_and_auth_failure_does_not():
    async def run():
        live,_=live_instance();attempts=[]
        async def connect(model,resume=False):
            attempts.append(model)
            if model=='primary':raise ProviderFailure(429)
        live._connect_once=connect
        await live.connect()
        assert attempts==['primary','backup'] and live.model_index==1
        live,_=live_instance();attempts=[]
        async def forbidden(model,resume=False):
            attempts.append(model);raise ProviderFailure(403)
        live._connect_once=forbidden
        with pytest.raises(ProviderFailure):await live.connect()
        assert attempts==['primary']
    asyncio.run(run())

def test_reconnect_retains_history_and_close_erases_private_state():
    async def run():
        live,_=live_instance();attempts=[]
        live.ledger.add('user','a private question')
        live.ledger.add('model','a source-grounded answer')
        live.resume_handle='private-handle'
        async def connect(model,resume=False):attempts.append((model,resume))
        live._connect_once=connect
        await live.connect(recovery=True)
        assert attempts==[('primary',True)] and len(live.ledger.replay())==2
        await live.close()
        assert not live.ledger.replay() and live.resume_handle is None
        assert live.mail_tools.closed and not live.mail_tools.cache
    asyncio.run(run())

def test_late_transcription_does_not_cancel_tool_or_advance_epoch():
    async def run():
        live,_=live_instance()
        class Socket:
            def __aiter__(self):return self.iterate()
            async def iterate(self):
                yield json.dumps({'serverContent':{'inputTranscription':{'text':'Who'}}})
                yield json.dumps({'serverContent':{'inputTranscription':{'text':'sent it?','finished':True}}})
                live.closed=True
        live.socket=Socket()
        await live.receive()
        assert live.voice.interrupts==0
        assert 'Who sent it?' in live.ledger.replay()[0]['parts'][0]['text']
    asyncio.run(run())

def test_interruption_discards_a_late_tool_result():
    async def run():
        live,_=live_instance();sent=[]
        async def send(payload):sent.append(payload)
        live.send=send
        original=live.mail_tools.execute
        def execute(name,args):
            result=original(name,args)
            live.generation+=1
            return result
        live.mail_tools.execute=execute
        await live.delegate_after_transcription({'id':'call-1','name':'search_mail','args':{}})
        assert sent==[]
    asyncio.run(run())

def test_background_refresh_is_async_and_stops_on_close():
    async def run():
        live,_=live_instance()
        async def connect():pass
        live.connect=connect
        await live.start()
        for _ in range(100):
            if live.mail_tools.provider.reads:break
            await asyncio.sleep(.001)
        assert live.mail_tools.provider.reads==1
        await live.close()
        assert live.background.done() and not live.mail_tools.cache
    asyncio.run(run())

def test_history_bound_and_clear():
    ledger=ConversationLedger()
    for i in range(120):ledger.add('user',str(i))
    assert len(ledger.turns)==40 and len(ledger.summary)==20
    ledger.clear();assert ledger.replay()==[]

def test_read_error_invalidates_previous_cached_evidence():
    p=Provider();tools=OutlookTools(p)
    tools.execute('search_mail',{})
    def unavailable(mid):raise RuntimeError('deleted')
    p.get_message=unavailable
    with pytest.raises(RuntimeError):tools.execute('read_mail',{'id':'m1'})
    assert not tools.cache and not tools.references

def test_warmed_inbox_serves_smaller_search_without_another_request():
    p=Provider();now=[0];tools=OutlookTools(p,lambda:now[0])
    tools.execute('search_mail',{'max_results':20,'refresh':True})
    now[0]=30
    result=tools.execute('search_mail',{'max_results':1})
    assert p.reads==1 and result['cached']
    assert result['expires_in_seconds']==30

def test_provider_error_notice_is_bundled_pcm_not_a_network_dependency():
    import wave
    from pathlib import Path
    with wave.open(str(Path('app/voice/service-unavailable.wav')),'rb') as audio:
        assert audio.getframerate()==24000 and audio.getnchannels()==1
        assert audio.getsampwidth()==2 and audio.getnframes()>24000

def test_revoked_owner_cannot_receive_audio_from_history():
    async def run():
        live,audio=live_instance();live.mail_tools.provider.allowed=False
        class Socket:
            def __aiter__(self):return self.iterate()
            async def iterate(self):
                yield json.dumps({'serverContent':{'modelTurn':{'parts':[{'inlineData':{'mimeType':'audio/pcm','data':'AQABAA=='}}]}}})
        live.socket=Socket()
        async def notice():pass
        live.unavailable_notice=notice
        with pytest.raises(ProviderFailure) as error:await live.receive()
        assert error.value.code==403 and not audio
    asyncio.run(run())

def test_native_turns_retain_context_without_per_phrase_routes():
    async def run():
        live,audio=live_instance()
        class Socket:
            def __aiter__(self):return self.iterate()
            async def iterate(self):
                for i in range(20):
                    yield json.dumps({'serverContent':{'inputTranscription':{'text':'question '+str(i),'finished':True}}})
                    yield json.dumps({'serverContent':{'outputTranscription':{'text':'answer '+str(i)},
                        'modelTurn':{'parts':[{'inlineData':{'mimeType':'audio/pcm','data':'AQABAA=='}}]},'turnComplete':True}})
                live.closed=True
        live.socket=Socket();await live.receive()
        assert len(live.ledger.turns)==40 and len(audio)==20
        assert live.voice.interrupts==0
    asyncio.run(run())

def test_quota_close_reason_classified_without_exposing_reason():
    exc=SimpleNamespace(rcvd=SimpleNamespace(reason='RESOURCE_EXHAUSTED private provider details'))
    assert OutlookConversationLive.failure_code(exc)==429


def test_exhausted_models_are_bounded():
    async def run():
        live,_=live_instance();attempts=[]
        async def connect(model,resume=False):
            attempts.append(model);raise ProviderFailure(429)
        live._connect_once=connect
        with pytest.raises(ProviderFailure):await live.connect()
        assert attempts==['primary','backup'] and not live.recovering
    asyncio.run(run())
