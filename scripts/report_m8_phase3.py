"""Summarize only completed Phase 3 integration evaluations; never contacts a service."""
import argparse
import json
import math
from pathlib import Path
import statistics


def report(directory):
    paths=sorted(Path(directory).glob('[0-4].json'))
    if len(paths)!=5:
        raise ValueError('All five evaluation conversation files are required')
    records=[row for path in paths for row in json.loads(path.read_text())]
    if len(records)!=20:raise ValueError('Expected twenty recorded turns')
    positive=[r for r in records if r['expected']]
    negative=[r for r in records if r['expected_clarification']]
    latencies=sorted(r['latency_ms'] for r in records)
    topic=[r for r in records if (r['conversation'],r['turn']) in {(0,1),(1,1),(2,1),(2,3),(3,2)}]
    languages=[['English']*4,['Hindi','Hindi','Hinglish','Hinglish'],
               ['Gujarati','Gujarati','English','English'],['Gujarati-English','English','English','English'],['English']*4]
    multilingual={}
    for language in ['English','Hindi','Gujarati','Hinglish','Gujarati-English']:
        turns=[r for r in positive if languages[r['conversation']][r['turn']]==language]
        multilingual[language]={'positive_turns':len(turns),'correct':sum(r['correct_answer'] for r in turns)}
    return {'multilingual_retrieval':multilingual,'conversations':5,'turns':20,'positive_turns':len(positive),'clarification_turns':len(negative),
        'correct_answer_rate':sum(r['correct_answer'] for r in positive)/len(positive),
        'wrong_confident_answer_rate':sum(r['wrong_confident'] for r in records)/len(records),
        'source_recall':sum(len(set(r['expected'])&set(r['returned'])) for r in positive)/sum(len(r['expected']) for r in positive),
        'clarification_accuracy':sum(r['clarified'] for r in negative)/len(negative),
        'unnecessary_clarification_rate':sum(r['clarified'] for r in positive)/len(positive),
        'topic_memory_correctness':sum(r['expected']==r['returned'] for r in topic)/len(topic),
        'p50_ms':statistics.median(latencies),'p95_ms':latencies[math.ceil(.95*len(latencies))-1],
        'boundary':'Real authenticated offer/Live delegation/MailAgent/adaptive/PostgreSQL/original verification/topic/answer path; scripted semantic plans, synthetic mail provider, mocked RTC and generated PCM. Includes 300ms transcript settle and polling, excludes offer setup.',
        'records':records}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
    result=report(args.directory)
    Path('docs/M8_PHASE3_RESULTS.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='records'},indent=2))
