"""Small reproducible synthetic adaptive-decision benchmark; no provider network."""
import json
from pathlib import Path
from tests.test_m8_adaptive import retrieval, CASES
from app.retrieval.query import SearchSpec


def benchmark():
    session=retrieval.__wrapped__()
    engine,h,index=next(session)
    records=[]
    scenarios=[*CASES,('unsupported-paraphrase','commercial billing paperwork',
        SearchSpec(text='commercial billing paperwork',sender='ravi@aster.example.test'),False,
        {'aster-invoice-0','aster-invoice-1','aster-invoice-2'})]
    try:
        for name,utterance,spec,answer,gold in scenarios:
            baseline=index.search(spec,h.provider)
            result=engine.search(spec,h.provider,request=utterance)
            ids={r['message_id'] for r in result['messages']}
            raw={r['message_id'] for r in baseline['messages']}
            records.append(dict(case=name,decision=result['decision'],expected_answer=answer,
                wrong_answer=bool(ids-gold),missed_correct=len(gold-ids),expected_sources=len(gold),
                returned=sorted(ids),single_query_misses=len(gold-raw)))
        return {'cases':len(records),'wrong_answer_cases':sum(r['wrong_answer'] for r in records),
            'missed_correct_sources':sum(r['missed_correct'] for r in records),
            'expected_sources':sum(r['expected_sources'] for r in records),
            'single_query_missed_sources':sum(r['single_query_misses'] for r in records),
            'boundary':'Real SQLite recall; mocked conjunctive PostgreSQL/provider boundary. No live model or semantic paraphrase engine.',
            'records':records}
    finally:
        try:next(session)
        except StopIteration:pass


if __name__=='__main__':
    result=benchmark()
    Path('docs/M8_UNIVERSAL_RESULTS.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='records'}))
