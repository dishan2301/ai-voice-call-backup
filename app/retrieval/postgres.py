"""Mailbox-bound PostgreSQL candidate retrieval, always verified at the provider."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import hashlib
import json
import os

from sqlalchemy import text
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.retrieval.query import SearchSpec, fuse, vector_literal


class IndexUnavailable(RuntimeError):
    pass


class CursorConflict(RuntimeError):
    pass


class PostgresIndex:
    def __init__(self, engine, context, authorize, *, content_key=None, semantic=False, embedder=None, stale_seconds=1800):
        if engine.dialect.name != 'postgresql':
            raise ValueError('PostgreSQL is required for the retrieval index')
        self.engine, self.context, self.authorize = engine, context, authorize
        self.content_key, self.semantic, self.embedder = content_key, semantic, embedder
        self.stale_seconds = stale_seconds

    @contextmanager
    def transaction(self, *, registered=True):
        self.authorize()
        with self.engine.begin() as conn:
            privileged = conn.execute(text('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user')).scalar()
            if privileged:
                raise IndexUnavailable('Retrieval runtime requires a non-superuser without BYPASSRLS')
            conn.execute(text("SELECT set_config('app.m8_mailbox', :m, true)"), {'m': self.context.mailbox_id})
            conn.execute(text("SELECT set_config('statement_timeout','3000',true)"))
            if registered:
                box = conn.execute(text('SELECT * FROM m8r_mailboxes WHERE mailbox_id=:m'), {'m':self.context.mailbox_id}).mappings().first()
                if not box or (box['owner_user_id'],box['workspace_id'],box['provider']) != (
                        self.context.user_id,self.context.workspace_id,self.context.provider):
                    raise PermissionError('Retrieval mailbox ownership mismatch')
            yield conn

    def register(self, *, content_enabled=False, retention_days=90):
        if content_enabled and (not self.content_key or len(self.content_key)!=32):
            raise ValueError('Encrypted content indexing requires an explicit 32-byte local key')
        if not 1 <= retention_days <= 3650:
            raise ValueError('Retention must be between 1 and 3650 days')
        with self.transaction(registered=False) as conn:
            conn.execute(text('''INSERT INTO m8r_mailboxes(mailbox_id,owner_user_id,workspace_id,provider,content_enabled,retention_days)
                VALUES (:m,:u,:w,:p,:c,:r) ON CONFLICT(mailbox_id) DO NOTHING'''),
                {'m':self.context.mailbox_id,'u':self.context.user_id,'w':self.context.workspace_id,
                 'p':self.context.provider,'c':content_enabled,'r':retention_days})
            row = conn.execute(text('SELECT * FROM m8r_mailboxes WHERE mailbox_id=:m'), {'m':self.context.mailbox_id}).mappings().one()
            if (row['owner_user_id'],row['workspace_id'],row['provider']) != (self.context.user_id,self.context.workspace_id,self.context.provider):
                raise PermissionError('Retrieval mailbox ownership mismatch')
            conn.execute(text("INSERT INTO m8r_checkpoints(mailbox_id) VALUES (:m) ON CONFLICT DO NOTHING"), {'m':self.context.mailbox_id})

    def checkpoint(self):
        with self.transaction() as conn:
            return dict(conn.execute(text('SELECT * FROM m8r_checkpoints WHERE mailbox_id=:m'), {'m':self.context.mailbox_id}).mappings().one())

    def apply_batch(self, rows, deleted=(), *, cursor='', page_token='', complete=False, expected_generation=0, mode=None, reconcile_required=None, reconcile_after=None):
        """Atomic data/checkpoint CAS; retries of a committed page never overwrite newer state."""
        with self.transaction() as conn:
            m = self.context.mailbox_id
            box = conn.execute(text('SELECT * FROM m8r_mailboxes WHERE mailbox_id=:m'), {'m':m}).mappings().one()
            generation = conn.execute(text('SELECT generation FROM m8r_checkpoints WHERE mailbox_id=:m FOR UPDATE'), {'m':m}).scalar_one()
            if generation != expected_generation:
                raise CursorConflict('Index checkpoint changed; reload before retrying')
            for row in rows:
                mid, tid = str(row['message_id']), str(row['thread_id'])
                stamp = datetime.fromisoformat(str(row['received_at']).replace('Z','+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('Provider dates must be timezone aware')
                sender = parseaddr(str(row['sender']))[1].casefold()
                if not sender or not mid or not tid:
                    raise ValueError('Provider message identity and sender are required')
                allow_content = box['content_enabled'] and bool(self.content_key)
                body = str(row.get('body','')) if allow_content else ''
                metadata = {k:row.get(k) for k in ['subject','sender','recipient_emails','received_at','labels','attachments']}
                digest = hashlib.sha256(json.dumps([metadata,body],sort_keys=True,default=str).encode()).hexdigest()
                args = {'m':m,'id':mid,'tid':tid,'subject':str(row.get('subject',''))[:998],
                    'sender':sender,'name':parseaddr(str(row['sender']))[0], 'date':stamp,
                    'unread':'UNREAD' in row.get('labels',[]),'attachments':bool(row.get('attachments')),'hash':digest}
                conn.execute(text('INSERT INTO m8r_threads VALUES (:m,:tid) ON CONFLICT DO NOTHING'), args)
                conn.execute(text('''INSERT INTO m8r_messages(mailbox_id,message_id,thread_id,subject,sender_email,sender_name,received_at,unread,has_attachments,source_hash)
                    VALUES (:m,:id,:tid,:subject,:sender,:name,:date,:unread,:attachments,:hash)
                    ON CONFLICT(mailbox_id,message_id) DO UPDATE SET thread_id=excluded.thread_id,subject=excluded.subject,
                    sender_email=excluded.sender_email,sender_name=excluded.sender_name,received_at=excluded.received_at,
                    unread=excluded.unread,has_attachments=excluded.has_attachments,source_hash=excluded.source_hash,indexed_at=now(),deleted=false'''), args)
                conn.execute(text('UPDATE m8r_messages SET rfc_message_id=:rfc,in_reply_to=:parent WHERE mailbox_id=:m AND message_id=:id'),
                    {**args,'rfc':str(row.get('rfc_message_id','')),'parent':str(row.get('in_reply_to',''))})
                for table in ['participants','attachments','content']:
                    conn.execute(text(f'DELETE FROM m8r_{table} WHERE mailbox_id=:m AND message_id=:id'), args)
                for role, addresses in [('from',[sender]),('to',row.get('recipient_emails',[])),('cc',row.get('cc',[]))]:
                    for address in addresses:
                        address = parseaddr(str(address))[1].casefold()
                        if address:
                            conn.execute(text('INSERT INTO m8r_participants VALUES (:m,:id,:role,:email) ON CONFLICT DO NOTHING'),
                                         {**args,'role':role,'email':address})
                for number, attachment in enumerate(row.get('attachments',[])):
                    conn.execute(text('INSERT INTO m8r_attachments VALUES (:m,:id,:aid,:filename,:mime,:size)'),
                        {**args,'aid':str(attachment.get('attachment_id') or number), 'filename':str(attachment.get('filename','')),
                         'mime':str(attachment.get('mime_type','application/octet-stream')),'size':int(attachment.get('size_bytes',0))})
                if allow_content:
                    if not self.content_key:
                        raise ValueError('Content indexing key is unavailable')
                    nonce = os.urandom(12)
                    ciphertext = nonce + AESGCM(self.content_key).encrypt(nonce,body.encode(),f'{m}:{mid}'.encode())
                    conn.execute(text("INSERT INTO m8r_content VALUES (:m,:id,:cipher,to_tsvector('simple',:body),:expiry)"),
                        {**args,'cipher':ciphertext,'body':body,'expiry':datetime.now(timezone.utc)+timedelta(days=box['retention_days'])})
                if self.semantic and self.embedder:
                    embedding=vector_literal(self.embedder.encode_passage(args['subject']+' '+args['name']+' '+body))
                    conn.execute(text("""INSERT INTO m8r_embeddings VALUES (:m,:id,:model,:hash,CAST(:vector AS vector),:expiry)
                        ON CONFLICT(mailbox_id,message_id,model) DO UPDATE SET source_hash=excluded.source_hash,
                        embedding=excluded.embedding,expires_at=excluded.expires_at"""),
                        {**args,'model':self.embedder.model_id,'vector':embedding,
                         'expiry':datetime.now(timezone.utc)+timedelta(days=box['retention_days'])})
            for mid in deleted:
                # FK cascade removes participants/content/vectors. No deleted content is retained.
                conn.execute(text('DELETE FROM m8r_messages WHERE mailbox_id=:m AND message_id=:id'), {'m':m,'id':str(mid)})
            conn.execute(text('''UPDATE m8r_checkpoints SET cursor=:cursor,page_token=:page,generation=generation+1,
                complete=:complete,mode=coalesce(:mode,mode),reconcile_required=coalesce(:reconcile,reconcile_required),
                reconcile_after=coalesce(:reconcile_after,reconcile_after),status=:status,updated_at=now() WHERE mailbox_id=:m'''),
                {'m':m,'cursor':cursor,'page':page_token,'complete':complete,'status':'ok' if complete else 'partial','mode':mode,'reconcile':reconcile_required,'reconcile_after':reconcile_after})
            return generation+1

    def register_contact(self, contact_id, name, addresses):
        with self.transaction() as conn:
            args={'m':self.context.mailbox_id,'id':contact_id,'name':name}
            conn.execute(text('INSERT INTO m8r_contacts VALUES (:m,:id,:name) ON CONFLICT(mailbox_id,contact_id) DO UPDATE SET name=excluded.name'),args)
            for address in addresses:
                conn.execute(text('INSERT INTO m8r_addresses VALUES (:m,:email,:id,true) ON CONFLICT(mailbox_id,email) DO UPDATE SET contact_id=excluded.contact_id,verified=true'),
                             {**args,'email':address.casefold()})

    def register_company(self, company_id, name, *, aliases=(), verified_domains=(), contacts=()):
        """Explicit owner-managed associations, never inferred from email bodies."""
        with self.transaction() as conn:
            args={'m':self.context.mailbox_id,'id':company_id,'name':name}
            conn.execute(text('INSERT INTO m8r_companies VALUES (:m,:id,:name) ON CONFLICT(mailbox_id,company_id) DO UPDATE SET name=excluded.name'),args)
            for alias in set([name,*aliases]):
                conn.execute(text('INSERT INTO m8r_aliases VALUES (:m,:id,:alias) ON CONFLICT DO NOTHING'),{**args,'alias':alias.casefold()})
            for domain in verified_domains:
                conn.execute(text('INSERT INTO m8r_domains VALUES (:m,:id,:domain,true) ON CONFLICT DO NOTHING'),{**args,'domain':domain.casefold()})
            for contact in contacts:
                conn.execute(text('INSERT INTO m8r_company_contacts VALUES (:m,:id,:contact) ON CONFLICT DO NOTHING'),{**args,'contact':contact})

    def candidates(self, spec: SearchSpec):
        """Every ranking channel shares the same hard-filtered eligible set."""
        with self.transaction() as conn:
            m=self.context.mailbox_id
            params={'m':m,'q':spec.text,'cap':max(50,spec.limit*5),'allow_content':bool(self.content_key)}
            clauses=['m.mailbox_id=:m','NOT m.deleted']
            for field,column in [('sender','sender_email'),('thread_id','thread_id'),('message_id','message_id')]:
                if getattr(spec,field):
                    clauses.append(f'm.{column}=:{field}'); params[field]=getattr(spec,field)
            for field,op in [('start','>='),('end','<')]:
                if getattr(spec,field):
                    clauses.append(f'm.received_at {op} :{field}'); params[field]=getattr(spec,field)
            for field in ['unread','has_attachments']:
                if getattr(spec,field) is not None:
                    clauses.append(f'm.{field}=:{field}'); params[field]=getattr(spec,field)
            if spec.subject:
                clauses.append('lower(m.subject)=lower(:subject)'); params['subject']=spec.subject
            if spec.recipient:
                clauses.append("EXISTS(SELECT 1 FROM m8r_participants p WHERE p.mailbox_id=m.mailbox_id AND p.message_id=m.message_id AND p.role IN ('to','cc') AND p.email=:recipient)")
                params['recipient']=spec.recipient
            for kind,clue in [('company',spec.company),('contact',spec.contact)]:
                if not clue:
                    continue
                if kind=='company':
                    ids=conn.execute(text('SELECT DISTINCT company_id AS id FROM m8r_aliases WHERE mailbox_id=:m AND alias=:name'), {'m':m,'name':clue.casefold()}).scalars().all()
                    suggestions=conn.execute(text('SELECT DISTINCT alias AS name FROM m8r_aliases WHERE mailbox_id=:m AND alias % :name ORDER BY name LIMIT 3'),{'m':m,'name':clue.casefold()}).scalars().all()
                else:
                    ids=conn.execute(text('SELECT contact_id AS id FROM m8r_contacts WHERE mailbox_id=:m AND lower(name)=:name'),{'m':m,'name':clue.casefold()}).scalars().all()
                    suggestions=conn.execute(text("""SELECT c.name || coalesce(' <' || string_agg(a.email,', ' ORDER BY a.email) || '>','') AS name
                        FROM m8r_contacts c LEFT JOIN m8r_addresses a ON a.mailbox_id=c.mailbox_id AND a.contact_id=c.contact_id AND a.verified
                        WHERE c.mailbox_id=:m AND c.name % :name GROUP BY c.contact_id,c.name ORDER BY name LIMIT 3"""),{'m':m,'name':clue}).scalars().all()
                if len(ids)!=1:
                    return {'candidates':[], 'pending_question':f'Please confirm the {kind}: ' + ', '.join(suggestions or [clue])+'?', 'complete':False}
                params[kind]=ids[0]
                if kind=='company':
                    clauses.append('''(EXISTS(SELECT 1 FROM m8r_domains d WHERE d.mailbox_id=m.mailbox_id AND d.company_id=:company AND d.verified AND split_part(m.sender_email,'@',2)=d.domain)
                      OR EXISTS(SELECT 1 FROM m8r_addresses a JOIN m8r_company_contacts cc USING(mailbox_id,contact_id)
                       WHERE a.mailbox_id=m.mailbox_id AND cc.company_id=:company AND a.verified AND a.email=m.sender_email))''')
                else:
                    clauses.append('EXISTS(SELECT 1 FROM m8r_addresses a WHERE a.mailbox_id=m.mailbox_id AND a.contact_id=:contact AND a.verified AND a.email=m.sender_email)')
            base=' WITH eligible AS MATERIALIZED (SELECT m.* FROM m8r_messages m WHERE '+ ' AND '.join(clauses)+') '
            channels=[]
            matched={}
            if not spec.text:
                lexical=conn.execute(text(base+'SELECT * FROM eligible ORDER BY received_at DESC,message_id LIMIT :cap'),params).mappings().all()
            else:
                lexical=conn.execute(text(base+'''SELECT e.*,ts_rank_cd(e.metadata_fts,websearch_to_tsquery('simple',:q)) AS rank
                    FROM eligible e WHERE e.metadata_fts @@ websearch_to_tsquery('simple',:q)
                    OR (:allow_content AND EXISTS(SELECT 1 FROM m8r_content c WHERE c.mailbox_id=e.mailbox_id AND c.message_id=e.message_id
                      AND c.expires_at>now() AND c.search_fts @@ websearch_to_tsquery('simple',:q)))
                    ORDER BY rank DESC,received_at DESC,message_id LIMIT :cap'''),params).mappings().all()
                fuzzy=[] if lexical else conn.execute(text(base+'''SELECT e.* FROM eligible e WHERE e.subject % :q OR e.sender_name % :q
                    ORDER BY greatest(similarity(e.subject,:q),similarity(e.sender_name,:q)) DESC,e.message_id LIMIT :cap'''),params).mappings().all()
                channels.append([r['message_id'] for r in fuzzy])
                for row in fuzzy:
                    matched.setdefault(row['message_id'],set()).add('subject_or_sender_trigram')
            channels.insert(0,[r['message_id'] for r in lexical])
            for row in lexical:
                matched.setdefault(row['message_id'],set()).add('metadata' if not spec.text else 'full_text')
            rows={r['message_id']:dict(r) for r in [*lexical,*(fuzzy if spec.text else [])]}
            semantic_status='disabled'
            if self.semantic and self.embedder and spec.text:
                vector=vector_literal(self.embedder.encode_query(spec.text))
                params.update(vector=vector,model=self.embedder.model_id)
                vectors=conn.execute(text(base+'''SELECT e.* FROM eligible e JOIN m8r_embeddings v USING(mailbox_id,message_id)
                    WHERE v.model=:model AND v.source_hash=e.source_hash AND v.expires_at>now()
                    ORDER BY v.embedding <=> CAST(:vector AS vector),e.message_id LIMIT :cap'''),params).mappings().all()
                channels.append([r['message_id'] for r in vectors]); semantic_status='local'
                for row in vectors:
                    rows[row['message_id']]=dict(row); matched.setdefault(row['message_id'],set()).add('local_vector')
            elif self.semantic:
                semantic_status='unavailable_local_model'
            order=fuse(*channels)
            cp=conn.execute(text('SELECT * FROM m8r_checkpoints WHERE mailbox_id=:m'),{'m':m}).mappings().one()
            stale=not cp['updated_at'] or (datetime.now(timezone.utc)-cp['updated_at']).total_seconds()>self.stale_seconds
            complete=cp['complete'] and not cp['reconcile_required'] and not stale and all(len(channel)<params['cap'] for channel in channels) and len(order)<=spec.limit
            return {'candidates':[{**rows[mid],'matched_fields':sorted(matched[mid])} for mid in order[:spec.limit]],
                    'complete':bool(complete),'stale':stale,'semantic_status':semantic_status}

    def search(self, spec, provider):
        result=self.candidates(spec)
        verified=[]; failures=[]
        if result.get('pending_question'):
            return {'messages':[], 'error':result['pending_question'],'pending_question':result['pending_question'],
                    'completeness':{'complete':False},'retrieval_backend':'postgres'}
        for candidate in result['candidates']:
            self.authorize()
            try:
                source=provider.get_message(candidate['message_id'])
                if (source.get('message_id'),source.get('thread_id')) != (candidate['message_id'],candidate['thread_id']):
                    raise ValueError('Provider identity mismatch')
                # Recheck every hard constraint against current provider metadata.
                stamp=datetime.fromisoformat(str(source['received_at']).replace('Z','+00:00'))
                recipients=[parseaddr(str(a))[1].casefold() for a in source.get('recipient_emails',[])]
                current_sender=parseaddr(source.get('sender',''))[1].casefold()
                if (current_sender!=candidate['sender_email'] or
                    spec.sender and current_sender!=spec.sender or spec.recipient and spec.recipient not in recipients or
                    spec.start and stamp<spec.start or spec.end and stamp>=spec.end or
                    spec.subject and source.get('subject','').casefold()!=spec.subject.casefold() or
                    spec.unread is not None and ('UNREAD' in source.get('labels',[]))!=spec.unread or
                    spec.has_attachments is not None and bool(source.get('attachments'))!=spec.has_attachments):
                    raise ValueError('Provider metadata changed')
                verified.append({**source,'matched_fields':candidate['matched_fields'],'provider_verified':True,
                                 'retrieval_source':['PostgreSQL index','provider verification']})
            except Exception:
                failures.append(candidate['message_id'])
        self.authorize()
        return {'messages':verified,'retrieval_backend':'postgres','semantic_status':result.get('semantic_status'),
            'completeness':{'complete':result['complete'] and not failures,'verification_failures':failures,
                            'stale':result.get('stale',False),'candidate_count':len(result['candidates'])},
            'freshness':{'stale':result.get('stale',False)}}

    def set_content_enabled(self, enabled):
        """Explicit owner consent/revocation; revoke removes content and derived vectors."""
        if enabled and (not self.content_key or len(self.content_key)!=32):
            raise ValueError('An explicit 32-byte encryption key is required')
        with self.transaction() as conn:
            params={'m':self.context.mailbox_id,'enabled':bool(enabled)}
            conn.execute(text('UPDATE m8r_mailboxes SET content_enabled=:enabled WHERE mailbox_id=:m'),params)
            if not enabled:
                conn.execute(text('DELETE FROM m8r_content WHERE mailbox_id=:m'),params)
                if conn.execute(text("SELECT to_regclass('m8r_embeddings')")).scalar():
                    conn.execute(text('DELETE FROM m8r_embeddings WHERE mailbox_id=:m'),params)

    def put_embedding(self, message_id, values, model):
        if not self.semantic:
            raise ValueError('Semantic indexing is disabled')
        with self.transaction() as conn:
            args={'m':self.context.mailbox_id,'id':message_id,'model':model,'vector':vector_literal(values)}
            conn.execute(text('''INSERT INTO m8r_embeddings SELECT mailbox_id,message_id,:model,source_hash,CAST(:vector AS vector),now()+interval '30 days'
                FROM m8r_messages WHERE mailbox_id=:m AND message_id=:id
                ON CONFLICT(mailbox_id,message_id,model) DO UPDATE SET source_hash=excluded.source_hash,embedding=excluded.embedding,expires_at=excluded.expires_at'''),args)

    def purge(self, *, content_only=False):
        with self.transaction() as conn:
            args={'m':self.context.mailbox_id}
            conn.execute(text('DELETE FROM m8r_content WHERE mailbox_id=:m AND expires_at<=now()'),args)
            if conn.execute(text("SELECT to_regclass('m8r_embeddings')")).scalar():
                conn.execute(text('DELETE FROM m8r_embeddings WHERE mailbox_id=:m AND expires_at<=now()'),args)
            if not content_only:
                conn.execute(text("DELETE FROM m8r_messages WHERE mailbox_id=:m AND received_at < now() - (SELECT retention_days FROM m8r_mailboxes WHERE mailbox_id=:m)*interval '1 day'"),args)
