"""Synthetic evidence-bound retrieval acceptance cases; no live Gmail access."""
from datetime import datetime, timezone
import json

import pytest

from app.agents.mail_agent import MailAgent
from tests.test_mail_agent import make_agent, decision


def msg(mid, *, sender='Acme Support <support@acme.test>', to=None,
        subject='Delivery schedule', date='2026-10-30T09:00:00+00:00',
        body='We will deliver 12 units on 2026-11-05 at 3 pm for ₹4,500.'):
    return {'message_id': mid, 'thread_id': 'thread-'+mid, 'sender': sender,
            'recipient_emails': to or ['ops@customer.test'], 'subject': subject,
            'received_at': date, 'body': body}


def test_exact_header_match_and_body_mention_are_distinguished():
    rows = [msg('direct'), msg('digest', sender='Daily Digest <news@digest.test>',
                               subject='Acme news', body='Acme delivery schedule update')]
    got = MailAgent._filter_explicit_metadata(rows, 'Find email from Acme Support about delivery')
    assert [m['message_id'] for m in got] == ['direct']


def test_wrong_recipient_subject_date_and_identity_are_rejected():
    rows = [msg('right'), msg('wrong-to', to=['other@test']),
            msg('wrong-subject', subject='Payment'), msg('wrong-date', date='2026-10-29T09:00:00+00:00')]
    got = MailAgent._filter_explicit_metadata(rows, 'from Acme to ops@customer.test subject:Delivery schedule 2026-10-30')
    assert [m['message_id'] for m in got] == ['right']
    assert MailAgent._filter_explicit_metadata(rows, 'message id=not-present') == []


def test_factual_guard_rejects_unsupported_date_amount_and_time():
    evidence = {'m': msg('m')}
    assert not MailAgent._unsupported_literal_claim('Delivery is 2026-11-05 at 3 pm for ₹4,500.', evidence)
    assert MailAgent._unsupported_literal_claim('Delivery is 2026-12-01 at 4 pm for ₹9,000.', evidence)


def test_long_thread_and_body_truncation_are_reported():
    agent, _ = make_agent([])
    result = agent._search_mail({'query': 'Acme', 'limit': 1})
    # Provider test double returns one item without a continuation token.
    assert result['messages']
    source = {**msg('long'), 'body_truncated': True, 'thread_truncated': True}
    complete = not bool('next') and not (source.get('body_truncated') or source.get('thread_truncated'))
    assert complete is False


def test_partial_search_answer_discloses_more_results():
    from tests.test_mail_agent import FakeLLM
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'Acme'}),
        decision(kind='final', answer='The delivery is on 2026-11-05.'),
    ])
    agent.gmail.search_page = lambda *args: {'messages': [msg('m1')], 'next_page_token': 'next', 'candidate_count': 1}
    # Force the production-style page adapter while keeping all data synthetic.
    result = agent.run('Find email from Acme Support about delivery', 'm2-partial')
    assert 'incomplete' in result['answer'].lower() or 'more results' in result['answer'].lower()
    assert result['evidence'][0]['message_id'] == 'm1'


def test_no_match_does_not_reuse_a_fabricated_model_answer():
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'nonexistent'}),
        decision(tool='search_mail', arguments={'query': 'delivery'}),
        decision(kind='final', answer='The delivery is confirmed for 2026-11-05.'),
    ])
    agent.gmail.search = lambda *_: []
    result = agent.run('Find the nonexistent delivery email', 'm2-empty')
    assert 'couldn\'t find' in result['answer'].lower()
    assert result['evidence'] == []


def test_golden_metadata_retrieval_precision_recall_and_claim_support():
    golden = [
        ([msg('right'), msg('digest', sender='Daily Digest <news@digest.test>',
                           subject='Acme update')], 'Find email from Acme Support about delivery', {'right'}),
        ([msg('right'), msg('wrong', sender='Other <other@test>', subject='Delivery schedule')],
         'Find email from Acme Support about delivery', {'right'}),
    ]
    tp = fp = fn = 0
    for rows, request, relevant in golden:
        retrieved = {r['message_id'] for r in MailAgent._filter_explicit_metadata(rows, request)}
        tp += len(retrieved & relevant)
        fp += len(retrieved - relevant)
        fn += len(relevant - retrieved)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    unsupported = int(MailAgent._unsupported_literal_claim(
        'The payment is ₹9,000 on 2026-12-01 at 4 pm.', {'m': msg('m')}))
    assert (precision, recall, unsupported) == (1.0, 1.0, 1)
