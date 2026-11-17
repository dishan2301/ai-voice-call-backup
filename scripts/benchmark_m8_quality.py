"""Independent synthetic holdout; provider boundary is an explicit in-memory mock."""
import json
import math
import statistics
import time
from pathlib import Path
from app.retrieval.adaptive import AdaptiveRetrieval,terms
from app.retrieval.query import SearchSpec


def dataset():
    rows=[];cases=[]
    domains=['lumen','nimbus','orbit','prism','quartz','river','summit','willow']
    phrases=['equipment maintenance cost','annual licence charges','warehouse delivery schedule',
             'quarterly expense statement','project staffing requirement','printer replacement estimate',
             'remote access instructions','inventory reconciliation discrepancy']
    for i,(domain,phrase) in enumerate(zip(domains,phrases)):
        address=f'ops@{domain}.example.test'
        for n in range(3):
            rows.append(dict(message_id=f'{domain}-{n}',thread_id=domain,sender=address,
                subject=f'{domain.title()} reference {n}',body=f'Please review the {phrase}.',provider_verified=True))
        gold={f'{domain}-{n}' for n in range(3)}
        for text in [phrase, 'please show email about '+phrase, phrase+' બતાવો', phrase+' दिखाओ', phrase+' ke mail dikhao']:
            cases.append((text,SearchSpec(text=text,sender=address),gold))
        cases.append(('read missing turbine',SearchSpec(text='missing turbine',sender=address),set()))
        cases.append(('show me my mail',SearchSpec(text='show me my mail'),set()))
        # Same sender, unrelated body: a metadata-scoped recovery must not assert it matches.
        rows.append(dict(message_id=f'{domain}-noise',thread_id=domain+'-noise',sender=address,
                         subject='Office lunch',body='Team lunch menu.',provider_verified=True))
    # Similar subject/entity mentions from unrelated identities.
    for i in range(64):
        rows.append(dict(message_id=f'distractor-{i}',thread_id=f'distractor-{i}',sender=f'news{i}@digest.example.test',
                         subject=phrases[i%8],body='Unrelated newsletter coverage.',provider_verified=True))
    return rows,cases


class Index:
    def __init__(self,rows):self.rows=rows
    def search(self,spec,provider):
        rows=[row for row in self.rows if not spec.sender or row['sender']==spec.sender]
        hits=[dict(row) for row in rows if set(terms(spec.text))<=set(terms(row['subject']))]
        return {'messages':hits[:spec.limit], 'completeness':{'complete':len(hits)<=spec.limit}}


def benchmark():
    rows,cases=dataset();index=Index(rows);records=[]
    for mode in ['previous_enhancement','enhanced']:
        for repetition in range(3):
            for query,spec,gold in cases:
                start=time.perf_counter()
                result=AdaptiveRetrieval(index,recover_scoped=mode=='enhanced').search(spec,None,request=query)
                ms=(time.perf_counter()-start)*1000
                ids={r['message_id'] for r in result['messages']}
                choices={r['message_id'] for r in result.get('candidate_choices',[])}
                records.append(dict(mode=mode,query=query,repetition=repetition,latency_ms=ms,
                    wrong=bool(ids-gold),hit=len(ids&gold),gold=len(gold),candidate_hit=len((ids|choices)&gold),
                    clarify=result['decision']=='CLARIFY'))
    summary={}
    for mode in ['previous_enhancement','enhanced']:
        data=[r for r in records if r['mode']==mode];timings=sorted(r['latency_ms'] for r in data)
        denominator=sum(r['gold'] for r in data)
        summary[mode]={'requests':len(data),'wrong_answer_rate':sum(r['wrong'] for r in data)/len(data),
             'source_recall':sum(r['hit'] for r in data)/denominator,
             'candidate_recall':sum(r['candidate_hit'] for r in data)/denominator,
             'clarification_rate':sum(r['clarify'] for r in data)/len(data),
             'p50_ms':statistics.median(timings),'p95_ms':timings[math.ceil(.95*len(timings))-1]}
    return {'messages':len(rows),'independent_cases':len(cases),'repetitions':3,
        'boundary':'In-memory conjunctive metadata index and synthetic verified provider responses; no PostgreSQL/network/model timing.',
        'results':summary,'samples':records}


if __name__=='__main__':
    result=benchmark();Path('docs/M8_QUALITY_HOLDOUT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2))
