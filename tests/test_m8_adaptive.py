"""Offline adaptive retrieval: real SQLite recall, synthetic index/provider boundary."""
from dataclasses import dataclass
import json
from pathlib import Path
import pytest
from app.retrieval.adaptive import AdaptiveRetrieval, plan, verify_answer, terms
from app.retrieval.query import SearchSpec
from tests.m8_mailbox import MailboxHarness,tool,final


@pytest.fixture
def retrieval():
    h=MailboxHarness()
    original={r['message_id']:r for r in h.rows if r['mailbox_id']=='owner-mail' and not r['deleted'] and not r['inaccessible']}
    class Index:
        calls=[]
        def search(self,spec,provider):
            self.calls.append(spec)
            with h.sessions() as db:
                rows=(h.provider.get_thread(spec.thread_id,20) if spec.thread_id else
                      h.agent()._search_history(db,{'text':spec.text,'limit':20,**({'sender':spec.sender} if spec.sender else {})})['messages'])
            messages=[]
            for row in rows:
                source=original[row['message_id']]
                if spec.thread_id and source['thread_id']!=spec.thread_id:continue
                if spec.message_id and source['message_id']!=spec.message_id:continue
                # Simulate the PostgreSQL conjunctive lexical channel, not model semantics.
                if all(t in terms(source['subject']+' '+source['sender']) for t in terms(spec.text)):
                    messages.append({**source,'provider_verified':True})
            return {'messages':messages,'completeness':{'complete':len(rows)<20}}
    index=Index()
    yield AdaptiveRetrieval(index),h,index
    h.close()


CASES=[
    ('vague','show me my email',SearchSpec(text='show me my email'),False,set()),
    ('incomplete','Aster invoice',SearchSpec(text='Aster invoice',sender='ravi@aster.example.test'),True,{'aster-invoice-0','aster-invoice-1','aster-invoice-2'}),
    ('ambiguous','reply to invoice',SearchSpec(text='invoice'),False,set()),
    ('misleading','Aster invoice approved',SearchSpec(text='Aster invoice approved'),False,set()),
    ('gujarati','Aster ના invoice બતાવો',SearchSpec(text='Aster ના invoice બતાવો',sender='ravi@aster.example.test'),True,{'aster-invoice-0','aster-invoice-1','aster-invoice-2'}),
    ('hindi','Banyan का payroll दिखाओ',SearchSpec(text='Banyan का payroll दिखाओ'),True,{'banyan-payroll-0','banyan-payroll-1','banyan-payroll-2'}),
    ('hinglish','Cobalt ke attendance mail dikhao',SearchSpec(text='Cobalt ke attendance mail dikhao'),True,{'cobalt-attendance-0','cobalt-attendance-1','cobalt-attendance-2'}),
    ('missing','nonexistent reactor',SearchSpec(text='nonexistent reactor'),False,set()),
    ('wrong-sender','invoice',SearchSpec(text='invoice',sender='nobody@example.test'),False,set()),
    ('selected','read it',SearchSpec(text='',thread_id='aster-invoice'),True,{'aster-invoice-0','aster-invoice-1','aster-invoice-2'}),
]


@pytest.mark.parametrize('name,utterance,spec,answer,gold',CASES,ids=[c[0] for c in CASES])
def test_adaptive_decisions(retrieval,name,utterance,spec,answer,gold):
    engine,h,index=retrieval
    result=engine.search(spec,h.provider,request=utterance)
    assert (result['decision']=='ANSWER')==answer
    ids={r['message_id'] for r in result['messages']}
    # Identical subject decoy has a lookalike sender; enforce exact identity when requested.
    assert ids==gold
    assert len(index.calls)<=4
    if not answer: assert result['pending_question'] and not result['messages']


def test_expansion_never_changes_hard_constraints():
    spec=SearchSpec(text='please Aster invoice',sender='ravi@aster.example.test',unread=False,recipient='owner@example.test')
    for variant in plan(spec):
        assert variant.model_dump(exclude={'text','limit'})==spec.model_dump(exclude={'text','limit'})


@pytest.mark.parametrize('status',['approved','paid','confirmed','delivered','resolved','accepted'])
def test_counter_evidence_blocks_positive_status(status):
    rows=[{'subject':'Invoice','body':f'This invoice is not {status}.'}]
    assert not verify_answer(f'The invoice is {status}.',rows)
    assert verify_answer(f'The invoice is not {status}.',rows)


def test_false_match_and_failed_verification_cannot_be_an_answer():
    class Index:
        def search(self,*a):
            return {'messages':[{'message_id':'bad','thread_id':'bad','subject':'invoice'}],
                    'completeness':{'complete':False,'verification_failures':['bad']}}
    result=AdaptiveRetrieval(Index()).search(SearchSpec(text='invoice'),None)
    assert result['decision']=='CLARIFY' and not result['messages'] and not result['completeness']['complete']


def test_actual_mail_tool_uses_adaptive_clarification(retrieval):
    engine,h,index=retrieval
    agent=h.agent();agent.retrieval_index=index
    agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres','retrieval_adaptive_enabled':True})
    h.reasoner.decisions=[tool('search_history',text='nonexistent reactor'),final('It was approved.')]
    result=agent.run('Find nonexistent reactor','universal-test')
    assert result['state']=='NEEDS_CLARIFICATION' and not result['evidence']
    assert 'approved' not in result['answer']


def test_measure_wrong_answers_and_misses(retrieval):
    engine,h,index=retrieval
    records=[]
    for name,utterance,spec,answer,gold in CASES:
        index.calls.clear()
        result=engine.search(spec,h.provider,request=utterance)
        ids={r['message_id'] for r in result['messages']}
        records.append({'case':name,'decision':result['decision'], 'wrong_answer':bool(ids-gold),
                        'missed_correct':len(gold-ids),'expected':len(gold)})
    assert not any(r['wrong_answer'] or r['missed_correct'] for r in records)
    print('ADAPTIVE_MEASUREMENTS',json.dumps(records))


def test_source_contradiction_is_blocked_in_mail_answer(retrieval):
    engine,h,index=retrieval
    source={**h.rows[3],'body':'This invoice is not approved.','provider_verified':True}
    class StaticIndex:
        def search(self,*a):return {'messages':[source],'completeness':{'complete':True}}
    agent=h.agent();agent.retrieval_index=StaticIndex()
    agent.settings=agent.settings.model_copy(update={'retrieval_backend':'postgres','retrieval_adaptive_enabled':True})
    h.reasoner.decisions=[tool('search_history',text='Aster Invoice'),final('The invoice is approved.')]
    result=agent.run('Was the Aster invoice approved?','contradiction')
    assert 'contradicts' in result['answer'] and result['evidence']
