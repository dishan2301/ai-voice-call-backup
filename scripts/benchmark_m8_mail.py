"""Offline-only M8 SQLite/retrieval baseline. Run as a module from the repo root.

Language planning and relevance review are mocks; this never measures Gemini.
"""
import argparse
import json
import math
from pathlib import Path
import socket
import statistics
import time
from unittest.mock import patch

from app.agents.requests import handle_mail_request
from app.voice.mail_first import mail_agent_factory
from tests.m8_mailbox import MailboxHarness, cases


def summarize(samples):
    timings = sorted(row['latency_ms'] for row in samples)
    positives = [row for row in samples if row['expected']]
    returned = sum(len(row['returned']) for row in samples)
    wrong = sum(len(set(row['returned']) - set(row['expected'])) for row in samples)
    def recall(k):
        if not positives:
            return None
        return statistics.mean(len(set(row['returned'][:k]) & set(row['expected'])) / len(row['expected'])
                               for row in positives)
    return {'runs': len(samples),
            'exact_set_accuracy': statistics.mean(set(r['returned']) == set(r['expected']) for r in samples),
            'thread_set_accuracy': statistics.mean(set(r['threads']) == set(r['expected_threads']) for r in samples),
            'recall_at_5': recall(5), 'recall_at_10': recall(10),
            'wrong_email_selection_rate': wrong / returned if returned else 0,
            'wrong_selections': wrong, 'returned_selections': returned,
            'latency_p50_ms': statistics.median(timings),
            'latency_p95_ms': timings[math.ceil(len(timings)*.95)-1]}


def benchmark(repeats=10, fts=True):
    h = MailboxHarness(fts=fts)
    rows_by_id = {r['message_id']: r for r in h.rows if r['mailbox_id'] == 'owner-mail'}
    raw, integrated = [], []
    def forbidden(*a, **kw):
        raise AssertionError('External network/providers are forbidden')
    try:
        with patch.object(socket.socket, 'connect', forbidden), \
             patch('app.llm.gemini.GeminiProvider.generate_json', forbidden), \
             patch('app.email.gmail.GmailProvider.from_settings', forbidden), \
             patch('app.api.dependencies.create_mail_agent', h.agent):
            factory = mail_agent_factory(h.context)
            for iteration in range(repeats):
                for index, case in enumerate(cases()):
                    agent = h.agent()
                    start = time.perf_counter()
                    with h.sessions() as db:
                        found = agent._search_history(db, {**case.args, 'limit': 10})
                    elapsed = (time.perf_counter()-start)*1000
                    def record(messages, latency, **extra):
                        return {'case': case.name, 'language': case.language, 'iteration': iteration,
                            'expected': list(case.expected), 'expected_threads': sorted({rows_by_id[mid]['thread_id'] for mid in case.expected}),
                            'returned': [r['message_id'] for r in messages],
                            'threads': sorted({r['thread_id'] for r in messages}), 'latency_ms': latency, **extra}
                    raw.append(record(found['messages'], elapsed, complete=found['completeness']['complete']))
                    h.plan(case, 20)
                    start = time.perf_counter()
                    result = handle_mail_request(factory, case.request, f'bench-{iteration}-{index}',
                                                 input_mode='voice', preserve_literals=True)
                    elapsed = (time.perf_counter()-start)*1000
                    integrated.append(record(result.get('evidence', []), elapsed,
                        incomplete_disclosed='incomplete' in result['answer'].lower()))
        return {'dataset_messages': len(h.rows), 'stored_messages': len(h.rows)-1,
            'owner_messages': sum(r['mailbox_id']=='owner-mail' for r in h.rows),
            'companies': 4, 'topics': 4, 'cases': len(cases()), 'repeats': repeats,
            'dataset': 'tests.m8_mailbox.corpus; fixed dates, all .test addresses',
            'fts_enabled': fts,
            'boundary': 'Existing SQLite FTS5 index and SQL/LIKE fallback. '
                        'Real MailAgent/evidence/owner/memory/request handling. Mock plans/relevance; no Gmail query or language-model evaluation.',
            'raw_candidates_limit10': summarize(raw), 'integrated_mock_model': summarize(integrated),
            'per_case': {case.name: {'raw': summarize([r for r in raw if r['case']==case.name]),
                'integrated': summarize([r for r in integrated if r['case']==case.name])} for case in cases()},
            'incomplete_disclosures': sum(r['incomplete_disclosed'] for r in integrated),
            'raw_samples': raw, 'integrated_samples': integrated}
    finally:
        h.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--without-fts', action='store_true')
    parser.add_argument('--repeats', type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    result = benchmark(args.repeats, fts=not args.without_fts)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered+'\n')
    print(json.dumps({k: v for k, v in result.items() if k not in {'raw_samples', 'integrated_samples', 'per_case'}}, indent=2))
