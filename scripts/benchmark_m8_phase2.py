"""Reproducible A/B benchmark: real SQLite versus PostgreSQL, synthetic providers/models.

No embeddings are downloaded. Synthetic vectors are not language-quality evidence.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import socket
import time
from unittest.mock import patch

from sqlalchemy import create_engine,text
from app.agents.requests import handle_mail_request
from app.core.auth import validate_mailbox_context
from app.retrieval.postgres import PostgresIndex
from app.voice.mail_first import mail_agent_factory
from tests.m8_mailbox import MailboxHarness,corpus,cases
from scripts.benchmark_m8_mail import summarize


def benchmark(dsn,*,noise=0,repeats=3):
    dataset=corpus()
    base=dataset[0]
    for i in range(noise):
        dataset.append({**base,'message_id':f'noise-{i}','thread_id':f'noise-thread-{i//3}',
            'sender':'Operations <ops@noise.example.test>','sender_name':'Operations','sender_email':'ops@noise.example.test',
            'subject':f'Routine service notice {i}','body':'Synthetic routine message.','attachments':[],
            'rfc_message_id':f'<noise-{i}@example.test>'})
    with patch('tests.m8_mailbox.corpus',lambda:dataset):
        h=MailboxHarness()
    engine=create_engine(dsn,hide_parameters=True,connect_args={'connect_timeout':3})
    def authorize():
        with h.sessions() as db: validate_mailbox_context(db,h.context)
    index=PostgresIndex(engine,h.context,authorize)
    index.register()
    rows=[r for r in dataset if r['mailbox_id']=='owner-mail' and not r['deleted'] and not r['inaccessible'] and r['sender']]
    by_id={r['message_id']:r for r in rows}
    h.provider.get_message=lambda mid:dict(by_id[mid])
    cp=index.checkpoint()
    start=time.perf_counter()
    index.apply_batch(rows,cursor='synthetic-100',complete=True,mode='history',expected_generation=cp['generation'])
    indexing_ms=(time.perf_counter()-start)*1000
    samples={'existing':[],'postgres':[]};cpu={}
    gold={r['message_id']:r for r in dataset if r['mailbox_id']=='owner-mail'}
    def forbidden(*a,**kw): raise AssertionError('Real provider access is forbidden')
    try:
        for backend in samples:
            def create(context):
                agent=h.agent(context);agent.gmail=h.provider
                agent.settings=agent.settings.model_copy(update={'retrieval_backend':backend})
                if backend=='postgres':agent.retrieval_index=index
                return agent
            # Loopback DB is required; only real provider factories are forbidden.
            with patch('app.api.dependencies.create_mail_agent',create), \
                 patch('app.llm.gemini.GeminiProvider.generate_json',forbidden), \
                 patch('app.email.gmail.GmailProvider.from_settings',forbidden):
                factory=mail_agent_factory(h.context)
                started_cpu=time.process_time()
                for iteration in range(repeats):
                    for ci,case in enumerate(cases()):
                        h.plan(case,20)
                        started=time.perf_counter()
                        result=handle_mail_request(factory,case.request,f'phase2-{noise}-{backend}-{iteration}-{ci}',
                            input_mode='voice',preserve_literals=True)
                        elapsed=(time.perf_counter()-started)*1000
                        evidence=result.get('evidence',[])
                        samples[backend].append({'case':case.name,'language':case.language,'iteration':iteration,
                            'expected':list(case.expected),'expected_threads':sorted({gold[mid]['thread_id'] for mid in case.expected}),
                            'returned':[r['message_id'] for r in evidence],'threads':sorted({r['thread_id'] for r in evidence}),
                            'latency_ms':elapsed,'incomplete':'incomplete' in result['answer'].lower()})
                cpu[backend]=time.process_time()-started_cpu
        return {'source_records':len(dataset),'postgres_owner_records':len(rows),'queries':len(cases()),'repeats':repeats,
            'indexing_ms':indexing_ms,'cpu_seconds':cpu,'python_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'semantic':'Not benchmarked: local multilingual model/weights unavailable; fake vectors test only hard filters and fusion.',
            'boundary':'Actual M8 request/auth/tool/evidence/topic path; deterministic language plans/relevance and synthetic provider revalidation. No external model/provider access.',
            'results':{backend:summarize(data) for backend,data in samples.items()},
            'language_accuracy':{backend:{language:sum(set(r['returned'])==set(r['expected']) for r in data if r['language']==language)/sum(r['language']==language for r in data)
                for language in sorted({r['language'] for r in data})} for backend,data in samples.items()},
            'samples':samples}
    finally:
        with index.transaction() as conn:
            conn.execute(text('DELETE FROM m8r_mailboxes WHERE mailbox_id=:m'),{'m':h.context.mailbox_id})
        h.close();engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    dsn=os.environ.get('M8_TEST_DATABASE_URL','')
    if not dsn:
        parser.error('Set the isolated M8_TEST_DATABASE_URL')
    results=[benchmark(dsn,noise=size) for size in [0,500]]
    args.output.write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
    for result in results:
        print(json.dumps({k:v for k,v in result.items() if k not in {'samples','language_accuracy'}},indent=2))
