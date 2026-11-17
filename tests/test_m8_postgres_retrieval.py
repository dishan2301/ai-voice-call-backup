"""Real PostgreSQL integration, synthetic data only. DB tests skip without explicit test DSN."""
import os
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine,text

from app.core.auth import MailboxContext
from app.retrieval.migrate import migrate
from app.retrieval.postgres import PostgresIndex, CursorConflict, IndexUnavailable
from app.retrieval.query import SearchSpec, from_arguments, fuse, vector_literal
from app.retrieval.sync import IndexSynchronizer, SyncPage, CursorExpired
from tests.m8_mailbox import MailboxHarness, cases, tool, final


@pytest.fixture(scope='module')
def pg_engine():
    dsn=os.environ.get('M8_TEST_DATABASE_URL','')
    if not dsn:
        pytest.skip('Set isolated M8_TEST_DATABASE_URL; no automatic database provisioning')
    engine=create_engine(dsn,hide_parameters=True)
    yield engine
    engine.dispose()


@pytest.fixture
def indexed(pg_engine):
    h=MailboxHarness()
    h.context=replace(h.context,mailbox_id='test-'+uuid.uuid4().hex)
    # Keep actual primary authentication, with an independently bound synthetic index identity.
    from app.database.models import AuthSession,MailboxConnection
    with h.sessions() as db:
        old=db.get(MailboxConnection,'owner-mail')
        db.add(MailboxConnection(id=h.context.mailbox_id,workspace_id=old.workspace_id,owner_user_id=old.owner_user_id,
            provider='gmail',provider_account_id=h.context.mailbox_id,verified_sender='owner@example.test'))
        db.get(AuthSession,'owner-session').mailbox_id=h.context.mailbox_id
        db.commit()
    from app.core.auth import validate_mailbox_context
    def authorize():
        with h.sessions() as db:
            validate_mailbox_context(db,h.context)
    index=PostgresIndex(pg_engine,h.context,authorize)
    index.register()
    rows=[r for r in h.rows if r['mailbox_id']=='owner-mail' and not r['deleted'] and not r['inaccessible'] and r['sender']]
    index.apply_batch(rows,cursor='100',complete=True,mode='history')
    def get_message(mid):
        return next(dict(r) for r in rows if r['message_id']==mid)
    h.provider.get_message=get_message
    yield index,h
    with index.transaction() as conn:
        conn.execute(text('DELETE FROM m8r_mailboxes WHERE mailbox_id=:m'),{'m':h.context.mailbox_id})
    h.close()


def test_rrf_deduplicates_and_rejects_invalid_vectors():
    assert fuse(['a','b','a'],['b','c'])[0]=='b'
    with pytest.raises(ValueError): vector_literal([float('nan')]*384)
    with pytest.raises(ValueError): vector_literal([0]*384)


def test_hard_query_validation():
    with pytest.raises(ValueError): SearchSpec(sender='Ravi Shah')
    with pytest.raises(ValueError): SearchSpec(start=datetime(2026,1,1))
    with pytest.raises(ValueError): SearchSpec(mailbox_id='foreign')
    with pytest.raises(ValueError): from_arguments({'query':'{Aster OR Cobalt}'},timezone.utc)
    assert from_arguments({'query':'from:owner@example.test is:unread'},timezone.utc).sender=='owner@example.test'


@pytest.mark.parametrize('case',cases(),ids=lambda c:c.name)
def test_actual_postgres_pipeline(indexed,case):
    index,h=indexed
    spec=from_arguments({**case.args,'limit':20},h.settings.timezone)
    if case.name=='exact-id':
        spec=spec.model_copy(update={'message_id':'aster-invoice-1'})
    result=index.search(spec,h.provider)
    ids={r['message_id'] for r in result['messages']}
    # Company-only free text also finds the lookalike subject. Exact company lookup uses verified domains.
    expected=set(case.expected)
    if case.name=='company': expected.add('lookalike')
    assert ids==expected
    assert all(r['provider_verified'] for r in result['messages'])


def test_metadata_only_minimization(indexed):
    index,h=indexed
    with index.transaction() as conn:
        assert conn.execute(text('SELECT count(*) FROM m8r_content')).scalar()==0
        assert conn.execute(text("SELECT count(*) FROM m8r_messages WHERE subject LIKE '%FOREIGN%'")).scalar()==0
    result=index.search(SearchSpec(text='જવાબ'),h.provider)
    assert not result['messages']  # body is not silently indexed


def test_rls_and_composite_foreign_keys(indexed):
    index,h=indexed
    with index.transaction() as conn:
        own=conn.execute(text('SELECT count(*) FROM m8r_messages')).scalar()
        assert own>0
        conn.execute(text("SELECT set_config('app.m8_mailbox','foreign',true)"))
        assert conn.execute(text('SELECT count(*) FROM m8r_messages')).scalar()==0
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError):
        with index.transaction() as conn:
            conn.execute(text("INSERT INTO m8r_threads VALUES ('foreign','stolen')"))


def test_alias_contact_ambiguity_and_wrong_domain(indexed):
    index,h=indexed
    index.register_contact('ravi','Ravi Shah',['ravi@aster.example.test'])
    index.register_company('aster','Aster',aliases=['Indian Infotech Demo','એસ્ટર'],verified_domains=['aster.example.test'],contacts=['ravi'])
    for alias in ['Aster','Indian Infotech Demo','એસ્ટર']:
        response=index.search(SearchSpec(company=alias,text='Invoice'),h.provider)
        assert {r['message_id'] for r in response['messages']}=={'aster-invoice-0','aster-invoice-1','aster-invoice-2'}
    index.register_contact('other','Ravi Shah',['ravi@banyan.example.test'])
    assert index.search(SearchSpec(contact='Ravi Shah'),h.provider)['pending_question']
    assert index.search(SearchSpec(company='Asterr'),h.provider)['pending_question']


def test_verification_blocks_current_permission_or_metadata_changes(indexed):
    index,h=indexed
    original=h.provider.get_message
    h.provider.get_message=lambda mid: {**original(mid),'sender':'Attacker <attacker@example.test>'}
    response=index.search(SearchSpec(sender='ravi@aster.example.test'),h.provider)
    assert not response['messages'] and not response['completeness']['complete']
    h.provider.get_message=lambda mid: (_ for _ in ()).throw(PermissionError('revoked'))
    assert not index.search(SearchSpec(text='Invoice'),h.provider)['messages']


def test_sync_cas_deletion_rollback_and_modified_unread(indexed):
    index,h=indexed
    cp=index.checkpoint(); row=h.provider.get_message('aster-invoice-0')
    index.apply_batch([{**row,'labels':['UNREAD']}],['aster-invoice-1'],cursor='101',complete=True,expected_generation=cp['generation'])
    with pytest.raises(CursorConflict):
        index.apply_batch([],cursor='old',expected_generation=cp['generation'])
    cp=index.checkpoint()
    with pytest.raises(ValueError):
        index.apply_batch([{**row,'sender':''}],cursor='broken',expected_generation=cp['generation'])
    assert index.checkpoint()['cursor']=='101'
    response=index.search(SearchSpec(sender='ravi@aster.example.test',text='Invoice',unread=True),h.provider)
    # Provider still says row 0 is read, so it must be dropped despite a changed index.
    assert {r['message_id'] for r in response['messages']}=={'aster-invoice-2'}
    assert not response['completeness']['complete']


def test_sync_bounded_retries_and_expired_cursor(indexed):
    index,h=indexed
    class Source:
        calls=0
        def changes(self,*args):
            self.calls+=1
            if self.calls<3: raise TimeoutError()
            return SyncPage([],[],'102','next')
    source=Source();sleep=[]
    response=IndexSynchronizer(index,source,sleep=sleep.append).run(max_pages=1)
    assert source.calls==3 and len(sleep)==2 and not response['complete']
    class Expired:
        def changes(self,*args): raise CursorExpired()
    response=IndexSynchronizer(index,Expired()).run()
    assert response['status']=='reconciliation_required'
    assert index.checkpoint()['mode']=='initial'


def test_current_m8_tools_use_index_and_preserve_draft_gate(indexed):
    index,h=indexed
    agent=h.agent();agent.gmail=h.provider;agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    agent.retrieval_index=index
    h.reasoner.review_terms=['Aster','Invoice']
    h.reasoner.decisions=[tool('search_history',text='Aster Invoice',sender='ravi@aster.example.test'),final()]
    response=agent.run('Find from ravi@aster.example.test about invoice','phase2-integration')
    assert {r['message_id'] for r in response['evidence']}=={'aster-invoice-0','aster-invoice-1','aster-invoice-2'}
    assert not h.provider.send_calls
    assert agent.run('done','phase2-integration')['state']!='SENT'


def test_local_semantic_fusion_never_relaxes_sender(indexed):
    index,h=indexed
    class SyntheticVectors:
        model_id='synthetic-test-not-language-quality'
        def encode_query(self,text): return [1.0]+[0.0]*383
    index.semantic=True;index.embedder=SyntheticVectors()
    for mid in ['aster-invoice-0','banyan-invoice-0']:
        index.put_embedding(mid,[1.0]+[0.0]*383,index.embedder.model_id)
    response=index.search(SearchSpec(text='unmatched semantic query',sender='ravi@aster.example.test'),h.provider)
    assert [r['message_id'] for r in response['messages']]==['aster-invoice-0']
    assert response['messages'][0]['matched_fields']==['local_vector']


def test_encrypted_opt_in_content_and_revocation(indexed):
    index,h=indexed
    index.content_key=b'x'*32
    index.set_content_enabled(True)
    row={**h.provider.get_message('aster-invoice-0'),'body':'confidential-search-token'}
    cp=index.checkpoint()
    index.apply_batch([row],cursor='200',complete=True,expected_generation=cp['generation'])
    with index.transaction() as conn:
        cipher=bytes(conn.execute(text('SELECT ciphertext FROM m8r_content WHERE message_id=:id'),{'id':row['message_id']}).scalar_one())
        assert b'confidential-search-token' not in cipher
    assert index.candidates(SearchSpec(text='confidential-search-token'))['candidates']
    index.set_content_enabled(False)
    assert not index.candidates(SearchSpec(text='confidential-search-token'))['candidates']


def test_index_outage_preserves_original_tool_with_disclosure(indexed):
    index,h=indexed
    agent=h.agent();agent.gmail=h.provider
    agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    class Offline:
        def search(self,*a): raise ConnectionError('private DSN detail must not surface')
    agent.retrieval_index=Offline()
    result=agent._search_mail({'query':'from:ravi@aster.example.test'})
    assert result['index_fallback'] and not result['completeness']['complete']
    assert 'private DSN' not in str(result)


def test_authorization_failure_never_falls_back(indexed):
    from fastapi import HTTPException
    index,h=indexed
    agent=h.agent();agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    class Denied:
        def search(self,*a): raise HTTPException(403,'revoked')
    agent.retrieval_index=Denied()
    with pytest.raises(HTTPException):
        agent._search_mail({'query':'Invoice'})


def test_freshness_cap_and_retention_are_explicit(indexed):
    index,h=indexed
    with index.transaction() as conn:
        conn.execute(text("UPDATE m8r_checkpoints SET updated_at=now()-interval '2 days'"))
    result=index.search(SearchSpec(limit=1,text='Invoice'),h.provider)
    assert result['completeness']['stale'] and not result['completeness']['complete']
    with index.transaction() as conn:
        conn.execute(text("UPDATE m8r_messages SET received_at=now()-interval '400 days'"))
    index.purge()
    assert not index.candidates(SearchSpec())['candidates']


def test_reconciliation_resumes_and_does_not_delete_unseen_rows(indexed):
    index,h=indexed
    cp=index.checkpoint()
    index.apply_batch([],cursor='100',complete=True,expected_generation=cp['generation'],reconcile_required=True)
    source=SimpleNamespace(provider=h.provider)
    sync=IndexSynchronizer(index,source)
    first=sync.reconcile(limit=20)
    assert not first['complete'] and index.checkpoint()['reconcile_required']
    seen=first['checked']
    while index.checkpoint()['reconcile_required']:
        result=sync.reconcile(limit=20);seen+=result['checked']
    assert seen==52
    assert not index.checkpoint()['reconcile_after']


def test_history_pagination_keeps_incremental_mode(indexed):
    index,h=indexed
    class Source:
        calls=[]
        def changes(self,cursor,page,limit):
            self.calls.append((cursor,page))
            return SyncPage([],[],'100' if not page else '200','second' if not page else '')
        def initial(self,*a): raise AssertionError('Delta pagination must not restart initial indexing')
    source=Source()
    response=IndexSynchronizer(index,source).run(max_pages=2)
    assert source.calls==[('100',''),('100','second')]
    assert response['complete'] and index.checkpoint()['cursor']=='200'


def test_parameterized_search_and_forced_rls_on_every_table(indexed):
    index,h=indexed
    index.search(SearchSpec(text="'; SELECT private FROM secret; --"),h.provider)
    with index.transaction() as conn:
        tables=conn.execute(text("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname LIKE 'm8r_%' AND relkind='r' AND relname<>'m8r_schema_version'")).all()
        assert tables and all(row[1] and row[2] for row in tables)


def test_unknown_model_stays_lexical_and_local_only(indexed):
    index,h=indexed
    index.semantic=True
    response=index.search(SearchSpec(text='Aster Invoice'),h.provider)
    assert response['messages'] and response['semantic_status']=='unavailable_local_model'


def test_query_keeps_explicit_offset_and_remote_tls():
    from app.retrieval.integration import index_engine
    spec=from_arguments({'start_date':'2026-11-15T00:00:00+05:30'},timezone.utc)
    assert spec.start.utcoffset()==timedelta(hours=5,minutes=30)
    with pytest.raises(ValueError): index_engine('postgresql+psycopg://synthetic@remote.example.test/test')


def test_defaults_preserve_original_backend():
    from app.core.config import Settings
    settings=Settings()
    assert settings.retrieval_backend=='existing' and not settings.retrieval_semantic_enabled
    assert not settings.retrieval_content_enabled


def test_ambiguity_reaches_existing_m8_clarification_state(indexed):
    index,h=indexed
    index.register_contact('one','Ravi Shah',['ravi@aster.example.test'])
    index.register_contact('two','Ravi Shah',['ravi@banyan.example.test'])
    agent=h.agent();agent.gmail=h.provider;agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    agent.retrieval_index=index
    h.reasoner.decisions=[tool('search_history',contact='Ravi Shah'),final()]
    result=agent.run('Find mail by Ravi Shah','clarify-index')
    assert result['state']=='NEEDS_CLARIFICATION'
    assert 'ravi@aster.example.test' in result['answer'] and 'ravi@banyan.example.test' in result['answer']
    assert not result['evidence'] and not result.get('draft')


def test_index_sync_is_connected_to_existing_on_demand_tool(indexed,monkeypatch):
    index,h=indexed
    agent=h.agent();agent.gmail=h.provider;agent.retrieval_index=index
    agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    calls=[]
    monkeypatch.setattr('app.api.dependencies.create_generation_service',lambda *a:SimpleNamespace(generate=lambda *a:SimpleNamespace(total_emails=0)))
    monkeypatch.setattr('app.retrieval.integration.sync_for_agent',lambda a:calls.append(a) or {'status':'bounded','complete':False})
    h.reasoner.decisions=[tool('sync_mail'),final('The mail index needs another bounded sync.')]
    agent.run('Sync my mail','sync-index')
    assert calls==[agent] and not h.provider.send_calls


def test_outage_cannot_relax_company_or_recipient_filter(indexed):
    index,h=indexed
    agent=h.agent();agent.gmail=h.provider;agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    class Offline:
        def search(self,*a): raise ConnectionError()
    agent.retrieval_index=Offline()
    with h.sessions() as db:
        result=agent._search_history(db,{'text':'Invoice','company':'Aster','recipient':'owner@example.test'})
    assert not result['messages'] and not result['completeness']['complete']


@pytest.mark.parametrize('constraints',[{'unread':False},{'has_attachments':True},{'start_date':'bad-date'}])
def test_outage_does_not_drop_boolean_or_invalid_date_constraints(indexed,constraints):
    index,h=indexed
    agent=h.agent();agent.gmail=h.provider;agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres'})
    class Offline:
        def search(self,*a): raise ConnectionError()
    agent.retrieval_index=Offline()
    result=agent._search_mail({'query':'Invoice',**constraints})
    assert not result['messages'] and not result['completeness']['complete']
