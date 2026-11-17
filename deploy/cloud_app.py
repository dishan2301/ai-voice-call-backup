"""Public non-sensitive deployment status; all application routes keep their auth."""
from starlette.requests import Request
from starlette.responses import HTMLResponse,JSONResponse,FileResponse
from pathlib import Path
from app.main import app as application
from app.core.config import get_settings

async def app(scope,receive,send):
    path=scope.get('path')
    if scope['type']=='websocket' and path=='/talk/ws':
        from deploy.browser_voice import call
        return await call(scope,receive,send)
    if scope['type']=='http' and path in {'/chat/status','/chat/message'}:
        if Request(scope).url.hostname not in get_settings().allowed_hosts:
            return await JSONResponse({'detail':'Untrusted request host'},status_code=403)(scope,receive,send)
        from deploy.chat_api import route
        return await route(scope,receive,send)
    public={'/chat':'chat.html','/chat.css':'chat.css','/chat.js':'chat.js','/talk':'talk.html','/talk.js':'talk.js','/voice-capture.js':'voice-capture.js'}
    if scope['type']=='http' and path in public and scope['method'] in {'GET','HEAD'}:
        if Request(scope).url.hostname not in get_settings().allowed_hosts:
            return await JSONResponse({'detail':'Untrusted request host'},status_code=403)(scope,receive,send)
        return await FileResponse(Path(__file__).parent/public[path])(scope,receive,send)
    if scope['type']=='http' and scope.get('path')=='/' and scope['method'] in {'GET','HEAD'}:
        request=Request(scope)
        if request.url.hostname not in get_settings().allowed_hosts:
            return await JSONResponse({'detail':'Untrusted request host'},status_code=403)(scope,receive,send)
        return await HTMLResponse('''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Voice Call</title><style>body{font:18px system-ui;background:#111827;color:#f3f4f6;max-width:650px;margin:12vh auto;padding:24px}a{color:#93c5fd}</style>
<h1>Morning Mail</h1><p><a href="/chat">Open Outlook chat assistant</a></p><p>Cloud preview.</p>
<p>The backend is deployed. Private mail requires owner sign-in and server-side configuration.</p>
<p>This free preview has temporary storage. It is not a production mailbox service.</p>
<p><a href="/health">Check backend health</a></p>
<p><a href="/talk">Open browser voice call</a>. A private server-side access code and Gemini key must be configured. No credentials or mail are included in this deployment.</p></html>''')(scope,receive,send)
    await application(scope,receive,send)
