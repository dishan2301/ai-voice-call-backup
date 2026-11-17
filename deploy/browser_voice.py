"""Same-origin WebSocket audio; mailbox ownership or a private general-voice token."""
import asyncio
import audioop
import hmac
import json
import os
import secrets
from urllib.parse import urlsplit
from starlette.websockets import WebSocket,WebSocketDisconnect
from app.core.auth import require_mailbox_context
from app.core.config import get_settings
from app.database.db import SessionLocal
from app.voice.outlook_conversation import OutlookConversationLive
from app.voice.session import VoiceSession

class NoMailbox:
    def authorize(self):pass
    def _get(self,*args,**kwargs):raise RuntimeError('Outlook is not connected')
    def get_message(self,*args):raise RuntimeError('Outlook is not connected')

active=set()

async def call(scope,receive,send):
    ws=WebSocket(scope,receive,send)
    settings=get_settings()
    origin=urlsplit(ws.headers.get('origin',''))
    if ws.url.hostname not in settings.allowed_hosts or origin.scheme!='https' or origin.netloc!=ws.headers.get('host'):
        await ws.close(code=1008);return
    await ws.accept()
    live=None;tasks=[];identifier=secrets.token_hex(8);queue=asyncio.Queue(maxsize=120)
    try:
        hello=await asyncio.wait_for(ws.receive_json(),10)
        if not isinstance(hello,dict):raise ValueError()
        rate=hello.get('sample_rate')
        if not isinstance(rate,int) or rate<8000 or rate>96000:raise ValueError()
        provider=None
        if ws.cookies.get('morning_mail_session'):
            with SessionLocal() as db:
                context=await require_mailbox_context(ws,db)
            if context.provider!='microsoft':raise ValueError()
            from app.api.dependencies import create_outlook_provider
            provider=create_outlook_provider(context)
        else:
            expected=os.environ.get('VOICE_PREVIEW_TOKEN','')
            supplied=hello.get('token','')
            if not expected or not isinstance(supplied,str) or not hmac.compare_digest(expected,supplied):
                await ws.send_json({'type':'error','message':'Enter the private voice access code, or sign in to Outlook.'})
                return
        if active:
            await ws.send_json({'type':'error','message':'Another call is active. Try again after it ends.'});return
        active.add(identifier)
        def emit(event):
            if queue.full():raise RuntimeError('Voice output queue full')
            queue.put_nowait(event)
        voice=VoiceSession(emit,lambda:None)
        live=OutlookConversationLive(settings,voice,emit,lambda:emit({'type':'clear_audio'}),provider or NoMailbox())
        if provider is None:
            live.tools=[]
            live.system_instruction=('You are a friendly conversational voice assistant. Speak short answers in the user language. '
                'This is a general voice connection test. Outlook is not connected; say so if asked about email. '
                'Do not invent mailbox facts, claim tool access or ask for passwords.')
        async def output():
            while True:
                value=await queue.get()
                if isinstance(value,bytes):await ws.send_bytes(value)
                elif value.get('type') in {'clear_audio','session.error','voice.provider.status'}:
                    await ws.send_json(value)
        tasks.append(asyncio.create_task(output()))
        await live.start()
        await ws.send_json({'type':'ready','mail_connected':provider is not None,'max_seconds':300})
        async def input_audio():
            state=None;total=0;began=asyncio.get_running_loop().time()
            while True:
                message=await ws.receive()
                if message['type']=='websocket.disconnect':return
                pcm=message.get('bytes')
                if pcm is None:return
                if len(pcm)>32768 or len(pcm)%2:raise ValueError('Invalid audio frame')
                total+=len(pcm)
                if total>rate*2*(asyncio.get_running_loop().time()-began+2):raise ValueError('Audio rate exceeded')
                pcm,state=audioop.ratecv(pcm,2,1,rate,16000,state)
                await live.audio(pcm)
        tasks.extend([asyncio.create_task(live.receive()),asyncio.create_task(input_audio())])
        done,_=await asyncio.wait(tasks,timeout=300,return_when=asyncio.FIRST_COMPLETED)
        for task in done:task.result()
    except (WebSocketDisconnect,asyncio.CancelledError):pass
    except Exception:
        try:await ws.send_json({'type':'error','message':'The voice connection could not continue. Check server configuration and retry.'})
        except Exception:pass
    finally:
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        if live:
            try:await asyncio.wait_for(live.close(),5)
            except Exception:pass
        active.discard(identifier)
        try:await ws.close()
        except Exception:pass
