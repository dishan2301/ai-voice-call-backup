from tests.test_mail_agent import make_agent, decision
from app.agents.slots import search_query
from app.core.config import Settings


def test_missing_instruction_question_survives_without_claiming_mail_facts():
    question = 'Should the reply accept or decline the invitation?'
    response = decision(kind='final', answer=question)
    response['pending_question'] = question
    agent, _ = make_agent([response])
    result = agent.run('Help me decide what to say in a reply', 'clarify-instruction')
    assert result['answer'] == result['spoken_answer'] == result['pending_question'] == question
    assert result['state'] == 'NEEDS_CLARIFICATION'
    assert not result['evidence'] and not agent.gmail.queries


def test_pending_question_cannot_restore_an_overridden_unverified_answer():
    response = decision(kind='final', answer='You have five meeting emails. Read them?')
    response['pending_question'] = 'Read them?'
    agent, _ = make_agent([response])
    result = agent.run('Any meeting mail?', 'unverified-question')
    assert 'five' not in result['answer']
    assert result['pending_question'] == 'Which sender or topic should I search?'


def test_multiple_threads_are_counted_without_model_picking_one():
    agent, _ = make_agent([decision(tool='search_mail', arguments={'query':'meeting'}),
        decision(kind='final', answer='2 relevant threads. Would you like details?')])
    agent.gmail.search = lambda *_: [
        {'message_id':'m1', 'thread_id':'t1', 'sender':'Zydus', 'subject':'Meeting one',
         'body':'Planning a meeting for Vadodara next week.'},
        {'message_id':'m2', 'thread_id':'t2', 'sender':'Customer', 'subject':'Meeting two',
         'body':'Planning a meeting for Vadodara next week.'},
    ]
    result = agent.run('Any meeting mail?', 'count')
    assert result['state'] == 'ANSWERING'
    assert '2 relevant' in result['answer']
    assert result['pending_question'] == 'Would you like details?'
    assert result['answer'].count('?') == 1
    assert len(result['evidence']) == 2
    assert result['spoken_answer'] == result['answer']


def test_empty_search_overrides_model_guess():
    agent, _ = make_agent([decision(tool='search_mail', arguments={'query':'Zydus'}),
                           decision(kind='final', answer='No mail.'),
                           decision(tool='search_mail', arguments={'query':'"Zydus"'}),
                           decision(kind='final', answer='There are probably five.')])
    agent.gmail.search = lambda *_: []
    result = agent.run('Any Zydus mail?', 'empty')
    assert "couldn't find mail matching your request" in result['answer']


def test_short_entity_query_retries_plain_entity_when_planned_query_misses():
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'from:versal'}),
        decision(kind='final', answer='I found the Versal message from 21 September.'),
    ])
    calls = []
    agent.gmail.search = lambda query, limit: calls.append(query) or ([] if query == 'from:versal' else [
        {'message_id': 'versal-1', 'thread_id': 'versal-thread', 'sender': 'Versal',
         'subject': 'Versal update', 'body': 'The Versal update was sent on 21 September.'}
    ])
    result = agent.run('Any mail from Versal?', 'versal-fallback')
    assert calls == ['from:versal', 'from:Versal']
    assert result['evidence'][0]['message_id'] == 'versal-1'


def test_lowercase_topic_gets_one_fallback_search():
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'subject:"video"'}),
        decision(kind='final', answer='I found the YouTube mail.'),
        decision(kind='final', answer='I found the YouTube mail.'),
    ])
    calls = []
    agent.gmail.search = lambda query, limit: calls.append(query) or ([] if len(calls) == 1 else [{
        'message_id': 'youtube-1', 'thread_id': 'youtube-thread', 'sender': 'YouTube',
        'subject': 'Your video', 'body': 'Your YouTube video is ready.'}])
    result = agent.run('Is there mail about youtube?', 'youtube-lowercase')
    assert calls[0] == 'subject:"video"'
    assert any('youtube' in query.casefold() for query in calls[1:])
    assert [item['message_id'] for item in result['evidence']] == ['youtube-1']


def test_explicit_sender_is_searched_when_model_asks_before_using_tools():
    agent, _ = make_agent([
        decision(kind='final', answer='Which sender should I search?'),
        decision(kind='final', answer='I found one YouTube email.'),
    ])
    calls = []
    agent.gmail.search = lambda query, limit: calls.append(query) or [{
        'message_id': 'youtube-1', 'thread_id': 'youtube-thread', 'sender': 'YouTube',
        'subject': 'Your video', 'body': 'Your YouTube video is ready.'}]
    result = agent.run('Is there mail from YouTube today?', 'pretool-sender')
    assert calls and 'youtube' in calls[0].casefold()
    assert result['evidence'][0]['message_id'] == 'youtube-1'


def test_entity_phonetic_signal_is_generic():
    from app.agents.mail_agent import MailAgent
    assert MailAgent._phonetic_key('Vercel') == MailAgent._phonetic_key('Versal')
    assert MailAgent._phonetic_key('Vercel') != MailAgent._phonetic_key('Invoice')


def test_sender_retry_still_checks_the_requested_topic():
    import json
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'from:versal invoice'}),
        decision(kind='final', answer='I found the invoice email.'),
    ])
    agent.gmail.search = lambda query, limit: [] if 'from:versal' in query else [
        {'message_id': 'invoice', 'thread_id': 'invoice', 'sender': 'Versal',
         'subject': 'Invoice', 'body': 'The invoice is attached.'},
        {'message_id': 'unrelated', 'thread_id': 'unrelated', 'sender': 'Versal',
         'subject': 'Holiday hours', 'body': 'The office is closed next week.'},
    ]
    original = agent.llm
    reviewed_ids = []

    class TopicReview:
        def generate_json(self, system, prompt, schema):
            if 'matches' not in schema['properties']:
                return original.generate_json(system, prompt, schema)
            rows = json.loads(prompt)['messages']
            reviewed_ids.extend(row['message_id'] for row in rows)
            return {'requested_scope': 'operational', 'matches': [{
                'message_id': 'invoice', 'excerpt': 'The invoice is attached.',
                'relationship': 'direct_correspondence', 'reason': 'This is the requested invoice.'}]}

    agent.llm = TopicReview()
    result = agent.run('Find invoice mail from Versal', 'sender-topic')
    assert set(reviewed_ids) == {'invoice', 'unrelated'}
    assert [item['message_id'] for item in result['evidence']] == ['invoice']


def test_model_query_is_preserved_for_arbitrary_domains():
    query = '"isotope calibration" {spectrometer dosimeter} from:lab.example'
    assert search_query(query, 'Any update on instrument calibration?', Settings().timezone) == query
    assert search_query('"instrument calibration"', 'Is calibration happening today?', Settings().timezone) == '"instrument calibration"'


def test_search_can_revise_query_and_follow_pages_before_answering():
    import json
    agent, _ = make_agent([])
    calls = []
    class Mailbox:
        def search_page(self, query, limit, page_token):
            calls.append((query, page_token))
            if query == 'rare instrument failure':
                return {'messages': [], 'next_page_token': ''}
            if not page_token:
                return {'messages': [{'message_id': 'noise', 'thread_id': 'n',
                         'subject': 'Digest', 'body': 'General technology news.'}], 'next_page_token': 'page2'}
            return {'messages': [{'message_id': 'source', 'thread_id': 't', 'subject': 'Lab update',
                     'body': 'The inlet seal has failed.'}], 'next_page_token': ''}
    agent.gmail = Mailbox()
    steps = iter([
        decision(tool='search_mail', arguments={'query': 'rare instrument failure'}),
        decision(tool='search_mail', arguments={'query': '{instrument seal}'}),
        decision(tool='search_mail', arguments={'query': '{instrument seal}', 'page_token': 'page2'}),
        decision(kind='final', answer='The inlet seal has failed.'),
    ])
    class LLM:
        def generate_json(self, system, prompt, schema):
            if 'matches' in schema['properties']:
                row = json.loads(prompt)['messages'][0]
                return {'requested_scope': 'operational', 'matches': [
                    {'message_id': 'source', 'excerpt': row['body'], 'relationship': 'operational',
                     'reason': 'The source answers the instrument question.'}
                ] if row['message_id'] == 'source' else []}
            return next(steps)
    agent.llm = LLM()
    result = agent.run('What is wrong with our laboratory instrument?', 'pages')
    assert calls == [('rare instrument failure', ''), ('{instrument seal}', ''), ('{instrument seal}', 'page2')]
    assert [m['message_id'] for m in result['evidence']] == ['source']
    assert result['answer'] == 'The inlet seal has failed.'


def test_ambiguous_day_is_asked_before_model_or_mail_call():
    agent, _ = make_agent([])
    result = agent.run('kal wala mail dikhao', 'date')
    assert result['state'] == 'NEEDS_CLARIFICATION'
    assert result['answer'] == 'Do you mean yesterday or tomorrow?'


def test_semantic_search_preserves_body_match_and_rejects_incidental_news():
    agent, _ = make_agent([])
    calls = []
    class LLM:
        def generate_json(self, system, prompt, schema):
            if 'matches' in schema['properties']:
                return {'requested_scope': 'operational', 'matches': [{'message_id': 'demo',
                    'excerpt': 'Demonstration at our Vadodara facility.', 'relationship': 'operational',
                    'reason': 'The message proposes the requested demonstration.'}]}
            calls.append(prompt)
            if len(calls) == 1:
                return decision(tool='search_mail', arguments={'query': 'demo Vadodara'})
            return decision(kind='final', answer='The email proposes a demonstration in Vadodara; no time is stated.')
    agent.llm = LLM()
    agent.gmail.search = lambda *_: [
        {'message_id': 'demo', 'thread_id': 't1', 'subject': 'Your enquiry',
         'body': 'Demonstration at our Vadodara facility.'},
        {'message_id': 'news', 'thread_id': 't2', 'subject': 'Markets',
         'body': 'The Vadodara company had a meeting about a demo product.'}]
    result = agent.run('Can you check whether somebody arranged a demo in Vadodra at 3 pm?', 'natural')
    assert [m['message_id'] for m in result['evidence']] == ['demo']
    assert 'no time is stated' in result['answer']


def test_semantic_review_rejects_fabricated_source_evidence():
    import pytest
    agent, _ = make_agent([])
    class LLM:
        def generate_json(self, *_):
            return {'requested_scope': 'operational', 'matches': [{'message_id': 'm1',
                'excerpt': 'Meeting at 3 pm', 'relationship': 'operational',
                'reason': 'The excerpt would support the requested meeting.'}]}
    agent.llm = LLM()
    with pytest.raises(ValueError, match='verify the search evidence'):
        agent._review_search_results([{'message_id': 'm1', 'subject': 'Meeting',
                                     'body': 'Please propose a time.'}], 'Meeting at 3 pm?')


def test_review_failure_is_not_reported_as_no_matching_mail():
    from app.llm.base import LLMProviderError
    agent, _ = make_agent([])
    class LLM:
        count = 0
        def generate_json(self, system, prompt, schema):
            if 'matches' in schema['properties']:
                raise LLMProviderError('temporary outage')
            self.count += 1
            if self.count == 1:
                return decision(tool='search_mail', arguments={'query': 'Zydus'})
            return decision(kind='final', answer='You have a meeting at 3 pm.')
    agent.llm = LLM()
    result = agent.run('Any Zydus mail?', 'review-outage')
    assert 'couldn\'t complete and verify' in result['answer']
    assert not result['evidence']
    assert '3 pm' not in result['answer']


def test_matching_messages_in_same_thread_are_not_lost_or_double_counted():
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'Helix'}),
        decision(kind='final', answer='I found 2 messages.'),
    ])
    first = {'message_id': 'a', 'thread_id': 'shared', 'sender': 'Helix <support@helix.example.test>', 'subject': 'Inspection',
             'body': 'Inspection requested.'}
    second = dict(first, message_id='b', body='Inspection rescheduled.')
    agent.gmail.search = lambda *_: [first, second, first]
    result = agent.run('Find inspection mail from Helix')
    assert {row['message_id'] for row in result['evidence']} == {'a', 'b'}
    assert result['answer'] == 'I found 2 messages.'


def test_history_union_reviews_every_candidate_even_when_it_exceeds_one_page():
    import json
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'Helix', 'limit': 20}),
        decision(kind='final', answer='I found 40 messages.'),
    ])
    def row(i):
        return {'message_id': str(i), 'thread_id': str(i), 'subject': 'Helix inspection',
                'body': 'Inspection update.', 'received_at': f'2026-09-{1 + i % 20:02d}'}
    agent.gmail.search = lambda *_: [row(i) for i in range(20)]
    agent._search_history = lambda *_: {'messages': [row(i) for i in range(20, 40)]}
    batches = []
    original = agent.llm
    class RecordingLLM:
        def generate_json(self, system, prompt, schema):
            if 'matches' in schema['properties']:
                batches.append(len(json.loads(prompt)['messages']))
            return original.generate_json(system, prompt, schema)
    agent.llm = RecordingLLM()
    result = agent.run('Find all Helix inspection mail')
    assert batches == [20, 20, 20]  # local pre-retrieval, then both merged search pages
    assert len(result['evidence']) == 40
    assert result['answer'].startswith('I found 40 messages.')
    assert 'incomplete' in result['answer'].lower()  # the preliminary local candidate set hit its cap


def test_query_refinement_also_checks_history_for_the_new_clue():
    agent, _ = make_agent([
        decision(tool='search_mail', arguments={'query': 'from:helix.example'}),
        decision(tool='search_mail', arguments={'query': 'bearing', 'text': 'bearing maintenance'}),
        decision(kind='final', answer='The bearing needs maintenance.'),
    ])
    agent.gmail.search = lambda *_: []
    searched = []
    def history(db, args):
        searched.append(args['text'])
        return {'messages': [] if len(searched) == 1 else [
            {'message_id': 'local', 'thread_id': 'thread', 'subject': 'Maintenance',
             'summary': 'The bearing needs maintenance.'}]}
    agent._search_history = history
    result = agent.run('What is wrong with the Helix machine?')
    assert searched[0] == 'What is wrong with the Helix machine?'
    assert 'helix.example' in searched
    assert 'bearing maintenance' in searched
    assert [row['message_id'] for row in result['evidence']] == ['local']
