"""Read-only /me Graph adapter. No sender credentials, mutation methods or attachment bytes."""
from datetime import datetime, timedelta, timezone
from email.utils import formataddr,parseaddr
import json
from urllib.parse import quote,urlsplit,parse_qs
import httpx
from app.email.microsoft_oauth import MicrosoftError,checked_scopes,refresh_grant
from app.email.normalizer import html_to_text
from app.retrieval.query import SearchSpec,from_arguments

class OutlookReadError(MicrosoftError):
    def __init__(self, status, retry_after=0):
        self.status=status
        self.retry_after=retry_after
        super().__init__('Outlook read unavailable')


BASE='https://graph.microsoft.com/v1.0/me/messages'
SELECT='id,conversationId,internetMessageId,subject,from,toRecipients,ccRecipients,receivedDateTime,body,isRead,hasAttachments,webLink'


def literal(value):return "'"+str(value).replace("'","''")+"'"


class OutlookProvider:
    def __init__(self,settings,load,save,authorize,subject,email,*,transport=None):
        self.settings,self.load,self.save,self.authorize=settings,load,save,authorize
        self.subject,self.email,self.transport=subject,email.casefold(),transport

    def _token(self):
        self.authorize()
        grant,etag=self.load();checked_scopes(grant.get('scope',''))
        if (grant.get('subject'),grant.get('email','').casefold())!=(self.subject,self.email):
            raise MicrosoftError('Stored mailbox identity mismatch')
        expiry=datetime.fromisoformat(grant['expires_at'])
        if expiry.tzinfo is None:raise MicrosoftError('Stored token expiry is invalid')
        if expiry<=datetime.now(timezone.utc)+timedelta(seconds=60):
            grant=refresh_grant(self.settings,grant)
            self.save(grant,etag)
        return grant['access_token']

    def _get(self,url,params=None):
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.username or parsed.fragment
                or not (parsed.path=='/v1.0/me/messages' or parsed.path.startswith('/v1.0/me/messages/'))):
            raise MicrosoftError('Invalid Graph continuation or resource path')
        token=self._token()
        headers={'Authorization':'Bearer '+token,
                 'Prefer':'IdType="ImmutableId", outlook.body-content-type="text"'}
        pooled=getattr(self,'http_client',None)
        if pooled is not None:
            response=pooled.get(url,params=params,headers=headers)
        else:
            with httpx.Client(timeout=15,follow_redirects=False,transport=self.transport) as client:
                response=client.get(url,params=params,headers=headers)
        self.authorize()
        if response.status_code!=200:
            try: retry_after=min(3600,max(0,float(response.headers.get('Retry-After','0'))))
            except ValueError: retry_after=60 if response.status_code==429 else 0
            raise OutlookReadError(response.status_code,retry_after)
        return response.json()

    def get_message(self,message_id):
        if not message_id or message_id in {'.','..'}:
            raise MicrosoftError('Invalid Outlook message reference')
        raw=self._get(BASE+'/'+quote(message_id,safe=''),{'$select':SELECT,
            '$expand':'attachments($select=id,name,contentType,size,isInline)'})
        if raw.get('id')!=message_id:raise MicrosoftError('Outlook original message identity mismatch')
        sender=raw.get('from',{}).get('emailAddress',{})
        body=html_to_text(raw.get('body',{}).get('content',''))
        url=raw.get('webLink','');parts=urlsplit(url)
        if parts.scheme!='https' or parts.hostname not in {'outlook.live.com','outlook.office.com','outlook.office365.com'}:url=''
        headers={r.get('name','').casefold():r.get('value','') for r in raw.get('internetMessageHeaders',[])}
        return {'message_id':raw['id'],'thread_id':raw['conversationId'],
            'rfc_message_id':raw.get('internetMessageId',''),'in_reply_to':headers.get('in-reply-to',''),
            'sender':formataddr((sender.get('name',''),sender.get('address',''))),
            'recipient_emails':[r.get('emailAddress',{}).get('address','') for r in raw.get('toRecipients',[])],
            'cc':[r.get('emailAddress',{}).get('address','') for r in raw.get('ccRecipients',[])],
            'received_at':raw['receivedDateTime'],'subject':raw.get('subject',''),
            'body':body[:40000],'body_truncated':len(body)>40000,
            'attachments_truncated':bool(raw.get('attachments@odata.nextLink')),
            'labels':[] if raw.get('isRead') else ['UNREAD'],
            'attachments':[{'attachment_id':r['id'],'filename':r.get('name',''),
                'mime_type':r.get('contentType',''),'size_bytes':r.get('size',0)} for r in raw.get('attachments',[])],
            'has_attachments':bool(raw.get('hasAttachments')),'provider':'microsoft','source_url':url,'provider_verified':True}

    def search_spec(self,spec,*,page_token=''):
        if spec.company or spec.contact:
            question='Which exact sender email address should I search for this company or contact?'
            return {'messages':[],'error':question,'pending_question':question,'completeness':{'complete':False}}
        if spec.message_id:
            candidates=[self.get_message(spec.message_id)];more='';truncated=False
        else:
            clauses=[]
            for value,field in [(spec.sender,'from/emailAddress/address'),(spec.subject,'subject'),(spec.thread_id,'conversationId')]:
                if value:clauses.append(field+' eq '+literal(value))
            if spec.recipient:
                clauses.append('(toRecipients/any(r:r/emailAddress/address eq '+literal(spec.recipient)+') or ccRecipients/any(r:r/emailAddress/address eq '+literal(spec.recipient)+'))')
            if spec.start:clauses.append('receivedDateTime ge '+spec.start.astimezone(timezone.utc).isoformat())
            if spec.end:clauses.append('receivedDateTime lt '+spec.end.astimezone(timezone.utc).isoformat())
            if spec.unread is not None:clauses.append('isRead eq '+str(not spec.unread).lower())
            if spec.has_attachments is not None:clauses.append('hasAttachments eq '+str(spec.has_attachments).lower())
            params={'$select':'id','$top':min(50,max(spec.limit,20))}
            if clauses:params['$filter']=' and '.join(clauses)
            elif spec.text:params['$search']=json.dumps(spec.text,ensure_ascii=False)
            else:params['$orderby']='receivedDateTime desc'
            if page_token:
                parsed=urlsplit(page_token)
                if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.path!='/v1.0/me/messages' or parsed.fragment:
                    raise MicrosoftError('Invalid Graph continuation')
                cursor=parse_qs(parsed.query)
                paging={key:value[0] for key,value in cursor.items() if key in {'$skip','$skiptoken'} and len(value)==1}
                if not paging or any(len(value)>8192 for value in paging.values()):
                    raise MicrosoftError('Invalid Graph continuation cursor')
                # Rebuild with the original filters, bounded size and ID-only projection.
                params.update(paging)
            page=self._get(BASE,params)
            more=page.get('@odata.nextLink','')
            if more:
                # Never trust foreign nextLink URLs, even if supplied by a server or client.
                parsed=urlsplit(more)
                if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com' or parsed.path!='/v1.0/me/messages' or parsed.fragment:
                    raise MicrosoftError('Invalid Graph continuation')
            candidates=[self.get_message(r['id']) for r in page.get('value',[])[:50]]
            truncated=bool(spec.text)  # Graph search is capped; bounded filter scans also are not exhaustive text searches.
        from app.retrieval.adaptive import terms,source_text
        results=[]
        for row in candidates:
            stamp=datetime.fromisoformat(row['received_at'].replace('Z','+00:00'))
            sender=parseaddr(row['sender'])[1].casefold()
            if (spec.sender and sender!=spec.sender or spec.recipient and spec.recipient not in {r.casefold() for r in [*row['recipient_emails'],*row['cc']]}
                or spec.subject and spec.subject.casefold()!=row['subject'].casefold() or spec.thread_id and spec.thread_id!=row['thread_id']
                or spec.start and stamp<spec.start or spec.end and stamp>=spec.end
                or spec.unread is not None and ('UNREAD' in row['labels'])!=spec.unread
                or spec.has_attachments is not None and row['has_attachments']!=spec.has_attachments):continue
            if spec.text and not set(terms(spec.text))<=set(terms(source_text(row))):continue
            results.append(row)
        return {'messages':results[:spec.limit],'next_page_token':more,'retrieval_backend':'microsoft',
            'completeness':{'complete':not more and not truncated and len(results)<=spec.limit and not any(r.get('body_truncated') or r.get('attachments_truncated') for r in candidates),
                            'candidate_count':len(candidates),'bounded':bool(more or truncated)}}

    def search_page(self,query,limit=10,page_token=''):
        return self.search_spec(from_arguments({'query':query,'limit':limit},self.settings.timezone),page_token=page_token)

    def get_thread(self,thread_id,limit=20):
        result=self.search_spec(SearchSpec(thread_id=thread_id,limit=min(20,limit)))
        return [dict(row,thread_truncated=not result['completeness']['complete']) for row in
                sorted(result['messages'],key=lambda r:r['received_at'])]

    def send_message(self,*a,**kw):raise MicrosoftError('Outlook pilot is read-only; sending is disabled')
    def unanswered(self,*a,**kw):raise MicrosoftError('Outlook waiting-on scan is not implemented for this pilot')


class OutlookIndex:
    def __init__(self,provider):self.provider=provider
    def search(self,spec,provider):return self.provider.search_spec(spec)
