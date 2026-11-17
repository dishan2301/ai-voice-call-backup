"""Personal Microsoft account delegated authorization; no password or ID-token decoding."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import urlencode
import httpx

AUTHORITY='https://login.microsoftonline.com/consumers/oauth2/v2.0'
USERINFO='https://graph.microsoft.com/oidc/userinfo'
SCOPES=('openid','profile','email','offline_access','https://graph.microsoft.com/Mail.Read')


class MicrosoftError(RuntimeError):
    pass


def checked_scopes(value):
    scopes={s.rsplit('/',1)[-1].casefold() for s in str(value).split()}
    if 'mail.read' not in scopes or scopes-{'mail.read','openid','profile','email','offline_access'}:
        raise MicrosoftError('Only delegated read-only pilot scopes are permitted')


def authorization_url(settings,state,verifier):
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return AUTHORITY+'/authorize?'+urlencode(dict(client_id=settings.microsoft_client_id,
        redirect_uri=settings.microsoft_redirect_uri,response_type='code',response_mode='query',
        scope=' '.join(SCOPES),state=state,code_challenge=challenge,code_challenge_method='S256',prompt='select_account'))


def identity(client,access_token):
    response=client.get(USERINFO,headers={'Authorization':'Bearer '+access_token})
    if response.status_code!=200:
        raise MicrosoftError('Microsoft account identity could not be verified')
    profile=response.json()
    subject,email=profile.get('sub'),profile.get('email')
    if not isinstance(subject,str) or not subject or not isinstance(email,str) or '@' not in email or len(email)>320:
        raise MicrosoftError('Microsoft did not return a usable sign-in identity')
    return subject,email.casefold()


def token_request(settings,data,*,old=None,client=None):
    def run(http):
        response=http.post(AUTHORITY+'/token',data={**data,'client_id':settings.microsoft_client_id,
            'client_secret':settings.microsoft_client_secret,'scope':' '.join(SCOPES)})
        if response.status_code!=200:
            raise MicrosoftError('Microsoft authorization failed; reconnect if consent was revoked')
        token=response.json();checked_scopes(token.get('scope',''))
        access=token.get('access_token');refresh=token.get('refresh_token') or (old or {}).get('refresh_token')
        if not isinstance(access,str) or not access or not isinstance(refresh,str) or not refresh or token.get('token_type','').casefold()!='bearer':
            raise MicrosoftError('Microsoft offline read authorization is incomplete')
        subject,email=identity(http,access)
        if old and (subject,email)!=(old.get('subject'),old.get('email')):
            raise MicrosoftError('Refreshed account identity changed')
        if email not in {e.casefold() for e in settings.microsoft_allowed_emails}:
            raise MicrosoftError('This account is not enabled for the pilot')
        return {'access_token':access,'refresh_token':refresh,'scope':token['scope'],
            'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=min(86400,max(1,int(token.get('expires_in',0)))))).isoformat(),
            'subject':subject,'email':email}
    if client is not None:return run(client)
    with httpx.Client(timeout=15,follow_redirects=False) as http:return run(http)


def exchange_code(settings,code,verifier,*,client=None):
    return token_request(settings,{'grant_type':'authorization_code','code':code,
        'redirect_uri':settings.microsoft_redirect_uri,'code_verifier':verifier},client=client)


def refresh_grant(settings,old,*,client=None):
    checked_scopes(old.get('scope',''))
    return token_request(settings,{'grant_type':'refresh_token','refresh_token':old['refresh_token']},old=old,client=client)
