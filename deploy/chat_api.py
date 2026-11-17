"""Owner-authenticated typed Outlook chat. No public-token mailbox elevation."""
import asyncio
import json
import time
from urllib.parse import urlsplit
from starlette.requests import Request
from starlette.responses import JSONResponse
from app.core.auth import require_mailbox_context
from app.core.config import get_settings
from app.database.db import SessionLocal
from app.voice.mail_first import mail_agent_factory
from app.agents.requests import handle_mail_request

locks={}

async def status(request):
    settings=get_settings()
    connected=False
    try:
        with SessionLocal() as db:
            context=await require_mailbox_context(request,db)
            connected=context.provider=='microsoft' and not context.legacy_local
    except Exception:pass
    return JSONResponse({'connected':connected,'provider':'Outlook',
        'sign_in_ready':bool(settings.microsoft_enabled and settings.microsoft_client_id and settings.microsoft_client_secret),
        'agent_ready':bool(settings.gemini_api_key),'read_only':True},headers={'Cache-Control':'no-store'})

async def message(request):
    origin=urlsplit(request.headers.get('origin',''))
    if origin.scheme!='https' or origin.netloc!=request.headers.get('host'):
        return JSONResponse({'error':'Open this chat on its own HTTPS website.'},status_code=403)
    if not request.headers.get('content-type','').startswith('application/json'):
        return JSONResponse({'error':'JSON required'},status_code=415)
    try:
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>10000:raise ValueError()
            raw.extend(chunk)
        data=json.loads(raw)
        text=data.get('message','');session=data.get('session_key','')
        if not isinstance(text,str) or not 1<=len(text.strip())<=2000:raise ValueError()
        if not isinstance(session,str) or not 24<=len(session)<=100:raise ValueError()
    except Exception:return JSONResponse({'error':'Enter a question of up to 2,000 characters.'},status_code=400)
    try:
        with SessionLocal() as db:context=await require_mailbox_context(request,db)
    except Exception:return JSONResponse({'error':'Sign in to your Outlook account first.','sign_in':True},status_code=401)
    if context.provider!='microsoft' or context.legacy_local:
        return JSONResponse({'error':'An owned Outlook session is required.'},status_code=403)
    if not get_settings().gemini_api_key:
        return JSONResponse({'error':'The mail agent is not configured on this server yet.'},status_code=503)
    key=(context.mailbox_id,session)
    now=time.monotonic()
    for old,(lock,used) in list(locks.items()):
        if not lock.locked() and now-used>3600:locks.pop(old,None)
    if len(locks)>=100 and key not in locks:return JSONResponse({'error':'Chat is busy. Please try shortly.'},status_code=429)
    lock,_=locks.setdefault(key,(asyncio.Lock(),now))
    if lock.locked():return JSONResponse({'error':'Your previous question is still being checked.'},status_code=409)
    async with lock:
        try:
            result=await asyncio.to_thread(handle_mail_request,mail_agent_factory(context),text.strip(),session,
                                          input_mode='text',preserve_literals=True,
                                          cancelled=lambda:False)
            # Raw transcripts, audit internals and model traces stay off the public wire.
            safe={k:result.get(k) for k in ['answer','spoken_answer','state','evidence','pending_question']}
            return JSONResponse(safe,headers={'Cache-Control':'no-store'})
        except Exception:
            return JSONResponse({'error':'The Outlook agent could not complete that request. Nothing was sent.'},status_code=502)
        finally:locks[key]=(lock,time.monotonic())

async def route(scope,receive,send):
    request=Request(scope,receive)
    if request.method=='GET' and request.url.path=='/chat/status':response=await status(request)
    elif request.method=='POST' and request.url.path=='/chat/message':response=await message(request)
    else:response=JSONResponse({'error':'Method not allowed'},status_code=405)
    await response(scope,receive,send)
