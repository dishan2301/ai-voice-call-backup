"""Public non-sensitive deployment status; all application routes keep their auth."""
from starlette.requests import Request
from starlette.responses import HTMLResponse,JSONResponse
from app.main import app as application
from app.core.config import get_settings

async def app(scope,receive,send):
    if scope['type']=='http' and scope.get('path')=='/' and scope['method'] in {'GET','HEAD'}:
        request=Request(scope)
        if request.url.hostname not in get_settings().allowed_hosts:
            return await JSONResponse({'detail':'Untrusted request host'},status_code=403)(scope,receive,send)
        return await HTMLResponse('''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Voice Call</title><style>body{font:18px system-ui;background:#111827;color:#f3f4f6;max-width:650px;margin:12vh auto;padding:24px}a{color:#93c5fd}</style>
<h1>AI Voice Call</h1><p>Morning Mail PA cloud preview.</p>
<p>The backend is deployed. Private mail requires owner sign-in and server-side configuration.</p>
<p>This free preview has temporary storage. It is not a production mailbox service.</p>
<p><a href="/health">Check backend health</a></p>
<p>Remote voice still requires a configured media relay. No credentials or mail are included in this deployment.</p></html>''')(scope,receive,send)
    await application(scope,receive,send)
