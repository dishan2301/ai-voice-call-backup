"""Explicit personal Outlook pilot onboarding and read-only first-test endpoint."""
import asyncio
import secrets
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import RedirectResponse,JSONResponse
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.auth import MailboxContext,require_mailbox_context
from app.database.db import get_db
from app.services.mailbox_credentials import MailboxCredentialStore
from app.services.oauth_transactions import OAuthTransactions
from app.services.hosted_login import finish_mailbox_login
from app.email.microsoft_oauth import authorization_url,exchange_code
from app.retrieval.query import SearchSpec

router=APIRouter(prefix='/auth/microsoft',tags=['Outlook pilot'])


def setup(request):
    settings=get_settings();redirect=urlsplit(settings.microsoft_redirect_uri)
    if (not settings.microsoft_enabled or settings.deployment_mode!='hosted' or not settings.microsoft_client_id
        or not settings.microsoft_client_secret or not settings.microsoft_allowed_emails
        or redirect.scheme!='https' or redirect.hostname not in settings.allowed_hosts
        or redirect.hostname!=request.url.hostname or redirect.path!='/auth/microsoft/callback'
        or redirect.username or redirect.password or redirect.query or redirect.fragment):
        raise HTTPException(503,'Personal Outlook pilot is not configured')
    store=MailboxCredentialStore(settings.provider_credential_key_directory,settings.provider_credential_key_version)
    return settings,store


@router.get('/start')
def start(request:Request,db:Session=Depends(get_db)):
    settings,store=setup(request)
    binding=secrets.token_urlsafe(32)
    try:
        attempt=OAuthTransactions(store).begin(db,'microsoft',settings.microsoft_redirect_uri,binding)
        url=authorization_url(settings,attempt.state,attempt.code_verifier)
    except Exception as exc:raise HTTPException(503,'Outlook sign-in could not start') from exc
    response=RedirectResponse(url)
    response.set_cookie('m8_microsoft_oauth',binding,max_age=600,secure=True,httponly=True,samesite='lax',path='/auth/microsoft')
    response.headers['Cache-Control']='no-store'
    return response


@router.get('/callback')
async def callback(request:Request,state:str='',code:str='',error:str='',db:Session=Depends(get_db)):
    # FastAPI has bound the callback values. Avoid retaining OAuth codes in ASGI access-log query strings.
    request.scope['query_string']=b''
    settings,store=setup(request)
    try:
        attempt=OAuthTransactions(store).claim(db,state,'microsoft',settings.microsoft_redirect_uri,
            request.cookies.get('m8_microsoft_oauth',''))
    except Exception as exc:raise HTTPException(400,'Outlook sign-in attempt is invalid or expired') from exc
    if error or not code or len(code)>4096:raise HTTPException(400,'Outlook consent was not completed')
    try:
        grant=await asyncio.to_thread(exchange_code,settings,code,attempt.code_verifier)
        token=finish_mailbox_login(db,store,provider='microsoft',subject=grant['subject'],email=grant['email'],credentials={'read':grant})
    except Exception as exc:raise HTTPException(403,'Outlook identity or read-only consent could not be verified') from exc
    response=JSONResponse({'status':'connected','email':grant['email'],'provider':'microsoft','read_only':True})
    response.set_cookie('morning_mail_session',token,max_age=43200,secure=True,httponly=True,samesite='strict',path='/')
    response.delete_cookie('m8_microsoft_oauth',path='/auth/microsoft',secure=True,httponly=True,samesite='lax')
    response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    return response


@router.get('/status')
def status(context:MailboxContext=Depends(require_mailbox_context),db:Session=Depends(get_db)):
    if context.provider!='microsoft' or context.legacy_local:raise HTTPException(403,'An owned Outlook session is required')
    from app.database.models import MailboxConnection
    connection=db.get(MailboxConnection,context.mailbox_id)
    return {'provider':'microsoft','email':connection.verified_sender,'read_only':True}


@router.post('/search')
async def first_search(spec:SearchSpec,context:MailboxContext=Depends(require_mailbox_context)):
    if context.provider!='microsoft' or context.legacy_local:raise HTTPException(403,'An owned Outlook session is required')
    from app.api.dependencies import create_outlook_provider
    try:
        result=await asyncio.to_thread(create_outlook_provider(context).search_spec,spec)
    except Exception as exc:raise HTTPException(502,'Outlook read could not be completed; no mailbox changes were made') from exc
    return JSONResponse(result,headers={'Cache-Control':'no-store'})


@router.delete('/connection')
def disconnect(request:Request,context:MailboxContext=Depends(require_mailbox_context),db:Session=Depends(get_db)):
    settings=get_settings()
    store=MailboxCredentialStore(settings.provider_credential_key_directory,settings.provider_credential_key_version)
    if context.provider!='microsoft' or context.legacy_local:raise HTTPException(403,'An owned Outlook session is required')
    store.disconnect(db,context,context.mailbox_id)
    response=JSONResponse({'status':'disconnected','local_sessions_revoked':True,'provider_revocation':
        'Personal Microsoft accounts do not support revokeSignInSessions. Remove app consent in your Microsoft account permissions.'})
    response.delete_cookie('morning_mail_session',path='/',secure=True,httponly=True,samesite='strict')
    response.headers['Cache-Control']='no-store'
    return response
