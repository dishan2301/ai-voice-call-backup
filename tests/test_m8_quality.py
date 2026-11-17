from app.retrieval.adaptive import AdaptiveRetrieval,plan
from app.retrieval.query import SearchSpec
from scripts.benchmark_m8_quality import dataset,Index


def test_independent_holdout_has_no_wrong_evidence():
    rows,cases=dataset();index=Index(rows)
    for query,spec,gold in cases:
        result=AdaptiveRetrieval(index).search(spec,None,request=query)
        ids={r['message_id'] for r in result['messages']}
        assert ids<=gold
        if gold:assert ids==gold


def test_unsupported_paraphrase_returns_choices_without_asserting_matches(retrieval):
    engine,h,index=retrieval
    result=engine.search(SearchSpec(text='commercial billing paperwork',sender='ravi@aster.example.test'),h.provider)
    assert result['decision']=='CLARIFY' and not result['messages']
    assert {'aster-invoice-0','aster-invoice-1','aster-invoice-2'}<={r['message_id'] for r in result['candidate_choices']}
    assert len(result['search_attempts'])<=4


def test_recovery_keeps_exact_identity_and_requires_verified_originals():
    class Index:
        def search(self,spec,provider):
            assert spec.sender=='owner@example.test' and spec.unread is False
            return {'messages':[dict(message_id='bad',thread_id='bad',subject='equipment cost',provider_verified=False)],
                    'completeness':{'complete':False,'verification_failures':['bad']}}
    result=AdaptiveRetrieval(Index()).search(SearchSpec(text='equipment cost',sender='owner@example.test',unread=False),None)
    assert not result['messages'] and not result['candidate_choices'] and not result['completeness']['complete']

from tests.test_m8_adaptive import retrieval
