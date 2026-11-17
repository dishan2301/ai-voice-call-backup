from datetime import datetime, timedelta, timezone

import pytest

from app.database.models import Email
from tests.test_mail_agent import make_agent, decision


@pytest.mark.parametrize('question', ['kal wala mail dikhao', 'कल का मेल दिखाओ'])
def test_kal_never_calls_provider_or_model(question):
    agent, _ = make_agent([])
    result = agent.run(question, 'ambiguous')
    assert result['pending_question'] == 'Do you mean yesterday or tomorrow?'
    assert result['evidence'] == []
    assert agent.gmail.queries == []


@pytest.mark.parametrize('sent,unread,expected', [
    (False, True, {'received-unread'}),
    (False, False, {'received-read'}),
    (True, True, {'sent-unread'}),
])
def test_count_filters_preserve_direction_unread_date_and_scope(sent, unread, expected):
    agent, factory = make_agent([])
    now = datetime.now(agent.settings.timezone).replace(hour=0, minute=0, second=0, microsecond=0)
    now = now.astimezone(timezone.utc).replace(tzinfo=None)
    with factory() as db:
        for ident, labels, days, mailbox in [
            ('received-unread', ['INBOX', 'UNREAD'], 0, 'default'),
            ('received-read', ['INBOX'], 0, 'default'),
            ('sent-unread', ['SENT', 'UNREAD'], 0, 'default'),
            ('old', ['INBOX', 'UNREAD'], -2, 'default'),
            ('foreign', ['INBOX', 'UNREAD'], 0, 'other'),
        ]:
            db.add(Email(provider='gmail', mailbox_id=mailbox, provider_message_id=ident,
                         thread_id=ident, sender_name='Acme', sender_email='acme@test',
                         subject='Delivery', normalized_body='Acme delivery',
                         received_at=now + timedelta(days=days), labels_json=labels,
                         body_hash=ident, is_deleted=False))
        db.commit()
        result = agent._search_history(db, dict(text='Acme missingword', date_range='today',
                                               sent=sent, unread=unread))
    # The missing word forces the fallback, which must keep metadata filters.
    assert {m['message_id'] for m in result['messages']} == expected


def test_filtered_count_uses_search_and_announces_unknown_freshness():
    agent, factory = make_agent([
        decision(tool='search_history', arguments={'text': 'Acme', 'limit': 20}),
        decision(kind='final', answer='Found 1 matching email.'),
    ])
    with factory() as db:
        db.add(Email(provider='gmail', mailbox_id='default', provider_message_id='acme',
                     thread_id='acme', sender_name='Acme', sender_email='acme@test',
                     subject='Acme delivery', normalized_body='Delivery confirmed',
                     labels_json=['INBOX', 'UNREAD'], received_at=datetime.utcnow(),
                     body_hash='acme', is_deleted=False))
        db.commit()
    result = agent.run('How many unread emails from Acme did I receive today?', 'filtered')
    assert result['steps'] == 2
    assert [m['message_id'] for m in result['evidence']] == ['acme']
    assert 'stale' in result['answer'] and 'stale' in result['spoken_answer']
    assert len(result['spoken_answer'].split()) <= 25


def test_sender_count_excludes_body_mentions():
    agent, factory = make_agent([])
    with factory() as db:
        for ident, sender in [('direct', 'Acme'), ('digest', 'Newsletter')]:
            db.add(Email(provider='gmail', mailbox_id='default', provider_message_id=ident,
                         thread_id=ident, sender_name=sender, sender_email=ident+'@test',
                         subject='Acme delivery', normalized_body='Acme', received_at=datetime.utcnow(),
                         body_hash=ident, is_deleted=False))
        db.commit()
        result = agent._search_history(db, {'text': 'Acme', 'sender': 'Acme'})
    assert [m['message_id'] for m in result['messages']] == ['direct']
    assert agent.parse_structured_intent('How many unread emails from Acme today?')['sender'] == 'Acme'


def test_last_week_count_does_not_search_today():
    from app.agents.slots import search_query
    agent, _ = make_agent([])
    zone = agent.settings.timezone
    now = datetime(2026, 11, 4, 12, tzinfo=zone)
    query = search_query('is:unread from:Acme', 'Count emails from Acme last week', zone, now)
    assert 'after:'+str(int(datetime(2026, 10, 26, tzinfo=zone).timestamp())) in query
    assert 'before:'+str(int(datetime(2026, 11, 2, tzinfo=zone).timestamp())) in query
    assert 'is:unread from:Acme' in query
