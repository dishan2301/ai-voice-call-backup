"""Bounded provider pages; checkpoint and changed records commit together."""
from dataclasses import dataclass
import time


class CursorExpired(RuntimeError):
    pass


@dataclass
class SyncPage:
    messages: list
    deleted: list
    cursor: str
    next_page: str = ''


class IndexSynchronizer:
    def __init__(self, index, source, *, attempts=3, sleep=time.sleep):
        self.index,self.source,self.attempts,self.sleep=index,source,min(3,max(1,attempts)),sleep

    def run(self, *, max_pages=2, page_size=50):
        if not 1<=max_pages<=20 or not 1<=page_size<=100:
            raise ValueError('Sync bounds exceeded')
        cp=self.index.checkpoint()
        for _ in range(max_pages):
            self.index.authorize()
            try:
                for attempt in range(self.attempts):
                    try:
                        page=(self.source.initial(cp['page_token'],page_size,cp['cursor']) if cp['mode']=='initial'
                              else self.source.changes(cp['cursor'],cp['page_token'],page_size))
                        break
                    except Exception as exc:
                        retryable=isinstance(exc,(TimeoutError,ConnectionError)) or getattr(getattr(exc,'resp',None),'status',None) in {429,500,502,503,504}
                        if not retryable or attempt+1==self.attempts:
                            raise
                        self.sleep(min(.25*2**attempt,1))
            except CursorExpired:
                self.index.apply_batch([],cursor='',complete=False,expected_generation=cp['generation'],mode='initial',reconcile_required=True,reconcile_after='')
                return {'status':'reconciliation_required','complete':False,'pages':0}
            # A failed page or killed worker leaves the last committed checkpoint intact.
            self.index.apply_batch(page.messages,page.deleted,cursor=page.cursor,
                page_token=page.next_page,complete=not bool(page.next_page),expected_generation=cp['generation'],
                mode='initial' if cp['mode']=='initial' and page.next_page else 'history')
            cp=self.index.checkpoint()
            if not page.next_page:
                return {'status':'ok' if not cp['reconcile_required'] else 'reconciliation_required',
                        'complete':not cp['reconcile_required'],'generation':cp['generation']}
        return {'status':'bounded','complete':False,'generation':cp['generation']}

    def reconcile(self, *, limit=100):
        """No unseen ID is deleted merely because a bounded scan missed it."""
        from sqlalchemy import text
        if not 1<=limit<=100:
            raise ValueError('Reconciliation bounds exceeded')
        cp=self.index.checkpoint()
        with self.index.transaction() as conn:
            ids=conn.execute(text('SELECT message_id FROM m8r_messages WHERE mailbox_id=:m AND message_id>:after ORDER BY message_id LIMIT :n'),
                {'m':self.index.context.mailbox_id,'n':limit,'after':cp['reconcile_after']}).scalars().all()
        removed=[]
        for mid in ids:
            self.index.authorize()
            try:
                self.source.provider.get_message(mid)
            except Exception as exc:
                if getattr(getattr(exc,'resp',None),'status',None) in {404,410}:
                    removed.append(mid)
                else:
                    raise
        self.index.apply_batch([],removed,cursor=cp['cursor'],page_token=cp['page_token'],
            complete=cp['complete'],expected_generation=cp['generation'],
            reconcile_required=len(ids)==limit,reconcile_after=ids[-1] if len(ids)==limit else '')
        return {'checked':len(ids),'removed':len(removed),'complete':len(ids)<limit}


class GmailIndexSource:
    """Reuse the authenticated Gmail client; bounded history pages, no send access."""
    def __init__(self, provider):
        self.provider=provider

    def initial(self, token, limit, cursor=''):
        self.provider._ensure_fresh_token()
        # Capture the history boundary before the first page; subsequent changes replay safely.
        cursor=cursor or self.provider.current_history_id()
        page=self.provider.service.users().messages().list(userId='me',maxResults=limit,pageToken=token or None).execute()
        rows=[];deleted=[]
        for row in page.get('messages',[]):
            try:
                rows.append(self.provider.get_message(str(row['id'])))
            except Exception as exc:
                if getattr(getattr(exc,'resp',None),'status',None)==404:
                    deleted.append(str(row['id']))
                else:
                    raise
        return SyncPage(rows,deleted,cursor,page.get('nextPageToken',''))

    def changes(self, cursor, token, limit):
        self.provider._ensure_fresh_token()
        try:
            page=self.provider.service.users().history().list(userId='me',startHistoryId=cursor,
                pageToken=token or None,maxResults=limit).execute()
        except Exception as exc:
            if getattr(getattr(exc,'resp',None),'status',None)==404:
                raise CursorExpired('History checkpoint expired') from None
            raise
        changed=set(); deleted=set()
        for event in page.get('history',[]):
            for key in ['messagesAdded','labelsAdded','labelsRemoved']:
                for item in event.get(key,[]):
                    changed.add(str(item['message']['id']))
            for item in event.get('messagesDeleted',[]):
                deleted.add(str(item['message']['id']))
        if len(changed|deleted)>100:
            # Preserve checkpoint for operator-controlled smaller/reconciliation scans.
            raise ValueError('History page exceeds the indexing record bound')
        rows=[]
        for mid in sorted(changed-deleted):
            try:
                rows.append(self.provider.get_message(mid))
            except Exception as exc:
                if getattr(getattr(exc,'resp',None),'status',None)==404:
                    deleted.add(mid)
                else:
                    raise
        next_page=page.get('nextPageToken','')
        # Keep startHistoryId stable while paginating the same history request.
        return SyncPage(rows,sorted(deleted),cursor if next_page else str(page.get('historyId',cursor)),next_page)
