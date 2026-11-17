"""Opt-in live Gemini evaluation with an isolated, generated test mailbox.
Run: .venv311/bin/python -m scripts.check_dynamic_search
This calls Gemini, never reads or writes the real Gmail account. The deliberately
broad lexical retriever is not an emulator of Gmail's complete query syntax.
"""
import json
import re
import uuid
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.agents.mail_agent import MailAgent
from app.core.config import get_settings
from app.database.db import Base
from app.database.models import Email


class TestMailbox:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def search(self, query, limit):
        self.queries.append(query)
        terms = [w.casefold() for w in re.findall(r'[\w-]+', query)
                 if w.casefold() not in {'from', 'subject', 'to', 'or', 'and', 'in', 'anywhere'}]
        scored = [(sum(t in json.dumps(row).casefold() for t in terms), row) for row in self.rows]
        return [row for score, row in sorted(scored, key=lambda pair: pair[0], reverse=True) if score][:limit]

    def get_thread(self, thread_id, limit):
        return [row for row in self.rows if row['thread_id'] == thread_id][:limit]


def main():
    name = 'Kestrel' + uuid.uuid4().hex[:8]
    rows = []
    def mail(identifier, subject, body):
        rows.append(dict(message_id=identifier, thread_id=identifier, sender=name + ' Operations',
                         subject=subject, body=body, received_at='2026-09-17T10:00:00+00:00'))
    mail('lab', 'Instrument maintenance', f'{name}: The chromatograph baseline drift came from a helium leak. Replace the inlet seal before recalibration.')
    mail('license', 'Contract addendum', f'{name}: The audiobook royalty increases from 7% to 9% on renewal. The print edition remains at 5%.')
    mail('shipment', 'Container exception', f'{name}: The refrigerated shipment had a temperature excursion at the port. Quarantine lot Q72 pending inspection.')
    mail('noise', 'Monthly digest', f'{name} sponsors sport and music. Our website has a support link. A laboratory company held its annual board meeting.')
    cases = [
        (f'Why did the lab instrument readings drift at {name}, and what should the team replace?', ['lab'], 'helium'),
        (f'What changed for spoken-book earnings in the {name} agreement?', ['license'], '9'),
        (f'Was anything wrong with the cold-chain cargo for {name}? What is the next action?', ['shipment'], 'Q72'),
        ('Any mail about lunarhabitat' + uuid.uuid4().hex[:10] + '?', [], None),
    ]
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = get_settings()
    # Exercise local recall as well as remote search: broad token matches must
    # not become answer cards without passing the same relevance review.
    with factory() as db:
        for row in rows:
            db.add(Email(provider=settings.email_provider,
                         mailbox_id=settings.mailbox_id,
                         provider_message_id=row['message_id'], thread_id=row['thread_id'],
                         sender_name=row['sender'], sender_email='operations@example.test',
                         subject=row['subject'], normalized_body=row['body'],
                         received_at=datetime(2026, 9, 17, 10), body_hash=row['message_id']))
        db.commit()
    results = []
    for i, (question, expected, fact) in enumerate(cases):
        mailbox = TestMailbox(rows)
        agent = MailAgent(session_factory=factory, gmail=mailbox)
        result = agent.run(question, session_key='unseen-' + str(i))
        ids = sorted(m['message_id'] for m in result.get('evidence', []))
        answer = result.get('answer', '')
        speech = result.get('spoken_answer', '')
        passed = (ids == expected and (fact is None or fact.casefold() in answer.casefold())
                  and bool(speech) and len(speech.split()) <= 25)
        if not expected:
            passed = passed and "couldn't find" in answer and bool(mailbox.queries)
        record = dict(case=i, question=question, queries=mailbox.queries, expected=expected,
                      returned=ids, answer=answer, spoken_answer=speech, passed=passed)
        print(json.dumps(record), flush=True)
        results.append(passed)
    assert all(results), 'Dynamic search evaluation failed; inspect the case output.'
    print('PASS: unseen topics, paraphrases, distractors and no-match behavior.')


if __name__ == '__main__':
    main()
