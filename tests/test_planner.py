"""Synthetic M5: no Google calls, user notifications or mail sends."""
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import MailboxContext
from app.database.db import Base
from app.database.models import PlannerAction, PlannerAudit, Reminder
from app.services import planner
from app.api.planner import assistant_request, execute_plan, is_planner_request
from app.services.calendar_provider import CalendarProvider, CALENDAR_SCOPES


@pytest.fixture
def setup():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    context = MailboxContext('alice', 'workspace', 'mailbox-a', legacy_local=True)
    with factory() as db:
        yield db, context, factory
    engine.dispose()


class Calendar:
    def __init__(self):
        self.calls = []
        self.event = {'id': 'event-1', 'etag': 'version-1', 'summary': 'Meeting',
                      'start': {'dateTime': '2026-12-01T10:00:00+05:30'},
                      'end': {'dateTime': '2026-12-01T11:00:00+05:30'}}
    def get(self, event_id):
        assert event_id == 'event-1'
        return self.event.copy()
    def mutate(self, action_id, data):
        self.calls.append((action_id, data))
        return {'id': action_id, 'status': 'confirmed'}


def event(operation='create_event'):
    return {'operation': operation, 'summary': 'Meeting', 'start_at': '2026-12-01T10:00:00+05:30',
            'end_at': '2026-12-01T11:00:00+05:30', 'timezone': 'Asia/Kolkata', 'event_id': 'event-1'}


def reminder(operation='create_reminder', **changes):
    return {'operation': operation, 'title': 'Check report', 'scheduled_at': '2026-12-01T10:00:00+05:30',
            'timezone': 'Asia/Kolkata', **changes}


def confirmed(db, context, proposal, calendar=None, session='session-123456789'):
    return planner.confirm(db, context, session, proposal['action_id'], proposal['confirmation_token'], 'confirm', calendar)


@pytest.mark.parametrize('operation', ['create_event', 'edit_event', 'cancel_event'])
def test_event_mutations_require_exact_confirm_and_are_once_only(setup, operation):
    db, context, _ = setup
    calendar = Calendar()
    result = planner.prepare(db, context, 'session-123456789', event(operation), calendar)
    assert result['details']['operation'] == operation
    assert not calendar.calls
    with pytest.raises(ValueError):
        planner.confirm(db, context, 'session-123456789', result['action_id'], result['confirmation_token'], 'yes', calendar)
    assert confirmed(db, context, result, calendar)['state'] == 'DONE'
    assert confirmed(db, context, result, calendar)['state'] == 'DONE'
    assert len(calendar.calls) == 1


def test_reconnect_deduplication_and_changed_details_invalidate_review(setup):
    db, context, _ = setup
    calendar = Calendar()
    first = planner.prepare(db, context, 'session-123456789', event(), calendar)
    second = planner.prepare(db, context, 'session-123456789', event(), calendar)
    assert first['action_id'] == second['action_id']
    with pytest.raises(ValueError):
        confirmed(db, context, first, calendar)
    third = planner.prepare(db, context, 'session-123456789', {**event(), 'summary': 'Changed'}, calendar)
    assert confirmed(db, context, second, calendar)['state'] == 'SUPERSEDED'
    assert not calendar.calls
    confirmed(db, context, third, calendar)
    again = planner.prepare(db, context, 'session-123456789', {**event(), 'summary': 'Changed'}, calendar)
    assert again['state'] == 'DONE' and len(calendar.calls) == 1


@pytest.mark.parametrize('kind', ['user', 'mailbox', 'session'])
def test_confirmation_isolation(setup, kind):
    db, context, _ = setup
    proposal = planner.prepare(db, context, 'session-123456789', reminder())
    other = MailboxContext('bob' if kind == 'user' else context.user_id, 'workspace',
                           'mailbox-b' if kind == 'mailbox' else context.mailbox_id, legacy_local=True)
    with pytest.raises(ValueError):
        confirmed(db, other, proposal, session='different-session' if kind == 'session' else 'session-123456789')
    assert not db.scalars(select(Reminder)).all()


def test_expired_or_interrupted_confirmation_cannot_execute(setup):
    db, context, _ = setup
    proposal = planner.prepare(db, context, 'session-123456789', reminder())
    row = db.get(PlannerAction, proposal['action_id'])
    row.expires_at = datetime.utcnow() - timedelta(seconds=1); db.commit()
    with pytest.raises(ValueError):
        confirmed(db, context, proposal)
    assert not db.scalars(select(Reminder)).all()


@pytest.mark.parametrize('value,zone', [
    ('2026-12-01T10:00:00', 'Asia/Kolkata'),
    ('2026-12-01T10:00:00+00:00', 'Asia/Kolkata'),
    ('2026-03-08T02:30:00-05:00', 'America/New_York'),
    ('2026-11-01T01:30:00-04:00', 'America/New_York'),
    ('2026-12-01T10:00:00+05:30', 'Unknown/Zone')])
def test_timezone_ambiguity_is_rejected(value, zone):
    with pytest.raises(ValueError):
        planner.instant(value, zone)


def test_india_time_converts_to_utc():
    assert planner.instant('2026-12-01T10:00:00+05:30', 'Asia/Kolkata') == datetime(2026, 12, 1, 4, 30)


def test_attendee_names_do_not_silently_become_addresses(setup):
    db, context, _ = setup
    with pytest.raises(ValueError, match='attendee'):
        planner.prepare(db, context, 'session-123456789', {**event(), 'attendees': [{'email': 'Priya'}]}, Calendar())


def test_provider_failure_keeps_unknown_and_never_retries(setup):
    db, context, _ = setup
    calendar = Calendar()
    calendar.mutate = Mock(side_effect=TimeoutError())
    proposal = planner.prepare(db, context, 'session-123456789', event(), calendar)
    assert confirmed(db, context, proposal, calendar)['state'] == 'UNKNOWN'
    assert confirmed(db, context, proposal, calendar)['state'] == 'UNKNOWN'
    calendar.mutate.assert_called_once()
    audits = db.scalars(select(PlannerAudit)).all()
    assert all(not hasattr(a, 'payload') for a in audits)


def test_reminders_downtime_claim_retry_ack_and_isolation(setup):
    db, context, _ = setup
    proposal = planner.prepare(db, context, 'session-123456789', reminder())
    confirmed(db, context, proposal)
    other = MailboxContext('bob', 'workspace', 'mailbox-b', legacy_local=True)
    now = datetime(2026, 12, 2)
    assert planner.claim_notifications(db, other, now) == []
    result = planner.claim_notifications(db, context, now)
    assert len(result) == 1
    assert planner.claim_notifications(db, context, now) == []
    retried = planner.claim_notifications(db, context, now + timedelta(minutes=3))
    assert len(retried) == 1
    with pytest.raises(ValueError):
        planner.acknowledge(db, context, result[0]['id'], result[0]['delivery_token'], 'SUBMITTED')
    planner.acknowledge(db, context, retried[0]['id'], retried[0]['delivery_token'], 'SUBMITTED')
    row = db.get(Reminder, result[0]['id'])
    assert row.attempts == 2 and row.status == 'NOTIFIED' and row.notification_status == 'SUBMITTED'


@pytest.mark.parametrize('status', ['DENIED', 'FAILED'])
def test_notification_failures_are_not_delivered(setup, status):
    db, context, _ = setup
    proposal = planner.prepare(db, context, 'session-123456789', reminder())
    confirmed(db, context, proposal)
    for attempt in range(5):
        result = planner.claim_notifications(db, context, datetime(2026, 12, 2) + timedelta(minutes=attempt*3))
        planner.acknowledge(db, context, result[0]['id'], result[0]['delivery_token'], status)
    assert planner.claim_notifications(db, context, datetime(2026, 12, 3)) == []
    assert db.get(Reminder, proposal['action_id']).notification_status == status


@pytest.mark.parametrize('operation', ['edit_reminder', 'complete_reminder', 'cancel_reminder'])
def test_reminder_management(setup, operation):
    db, context, _ = setup
    first = planner.prepare(db, context, 'session-123456789', reminder())
    confirmed(db, context, first)
    change = planner.prepare(db, context, 'session-123456789', reminder(operation, reminder_id=first['action_id'], title='New title'))
    assert confirmed(db, context, change)['state'] == 'DONE'
    row = db.get(Reminder, first['action_id'])
    assert row.title == 'New title' if operation == 'edit_reminder' else row.status in {'COMPLETED', 'CANCELLED'}


@pytest.mark.parametrize('text', ['calendar mein kal meeting', 'कल याद दिलाना', 'yaad dilao kal'])
def test_ambiguous_hindi_date_never_calls_model_or_provider(setup, text):
    _, context, factory = setup
    with patch('app.api.planner.SessionLocal', factory), patch('app.api.planner.GeminiProvider', side_effect=AssertionError('No model')):
        result = assistant_request(text, 'session-123456789', context)
    assert result['state'] == 'NEEDS_CLARIFICATION'


def test_calendar_adapter_verified_read_busy_and_conditional_mutation():
    service = Mock()
    service.events().list().execute.return_value = {'items': [{'id': 'event-1'}], 'nextPageToken': 'more'}
    service.freebusy().query().execute.return_value = {'calendars': {'primary': {'busy': []}}}
    service.events().get().execute.return_value = {'etag': 'v1'}
    service.events().patch().headers = {}
    service.events().patch().execute.return_value = {'id': 'event-1'}
    provider = CalendarProvider(service)
    assert provider.read('start', 'end')['nextPageToken'] == 'more'
    assert provider.read('start', 'end', busy=True)['calendars']['primary']['busy'] == []
    provider.mutate('abcdef1234', {**event('edit_event'), 'etag': 'v1'})
    assert service.events().patch().headers['If-Match'] == 'v1'
    with pytest.raises(ValueError):
        provider.mutate('abcdef1234', {**event('edit_event'), 'etag': 'old'})


def test_calendar_scopes_separate_from_gmail():
    from app.email.gmail import GMAIL_SCOPES, GMAIL_SEND_SCOPE
    assert not set(CALENDAR_SCOPES) & set(GMAIL_SCOPES + [GMAIL_SEND_SCOPE])


def test_linked_foreign_gmail_thread_is_rejected(setup):
    db, context, _ = setup
    with pytest.raises(ValueError, match='Gmail thread'):
        planner.prepare(db, context, 'session-123456789', reminder(gmail_thread_id='foreign'))


@pytest.mark.parametrize('text', ['Create a calendar meeting on December 1 at 10 to 11 IST',
                                 'calendar mein meeting banao', 'कैलेंडर में बैठक बनाओ'])
def test_voice_assistant_prepares_only_and_reconnect_keeps_scoped_proposal(setup, text):
    db, context, factory = setup
    provider = Calendar()
    model = Mock()
    model.generate_json.return_value = event()
    with patch('app.api.planner.SessionLocal', factory), patch('app.api.planner.GeminiProvider', return_value=model), \
            patch('app.api.planner.calendar_provider', return_value=provider):
        first = assistant_request(text, 'session-123456789', context)
        second = assistant_request(text, 'session-123456789', context)
    assert first['state'] == 'REVIEW' and first['action_id'] == second['action_id']
    assert not provider.calls
    assert 'previous_proposal' in model.generate_json.call_args.args[1]


def test_cancelled_final_voice_turn_does_not_prepare(setup):
    db, context, factory = setup
    model = Mock(); model.generate_json.return_value = event()
    with patch('app.api.planner.SessionLocal', factory), patch('app.api.planner.GeminiProvider', return_value=model):
        assert assistant_request('calendar meeting', 'session-123456789', context, lambda: True) == {'cancelled': True}
    assert not db.scalars(select(PlannerAction)).all()


def test_late_notification_ack_does_not_revive_cancelled_reminder(setup):
    db, context, _ = setup
    proposal = planner.prepare(db, context, 'session-123456789', reminder())
    confirmed(db, context, proposal)
    notification = planner.claim_notifications(db, context, datetime(2026, 12, 2))[0]
    row = db.get(Reminder, notification['id']); row.status = 'CANCELLED'; db.commit()
    with pytest.raises(ValueError):
        planner.acknowledge(db, context, row.id, notification['delivery_token'], 'SUBMITTED')
    assert row.status == 'CANCELLED'


def test_calendar_read_answers_are_verified_and_disclose_more_results(setup):
    db, context, _ = setup
    provider = Mock()
    provider.read.return_value = {'items': [{'id': 'e1', 'summary': 'Verified appointment',
        'start': {'dateTime': '2026-12-01T10:00:00+05:30'},
        'end': {'dateTime': '2026-12-01T11:00:00+05:30'}}], 'nextPageToken': 'more'}
    with patch('app.api.planner.calendar_provider', return_value=provider):
        result = execute_plan(db, context, 'session-123456789', {**event(), 'operation': 'list_events'})
    assert 'Verified appointment' in result['answer']
    assert 'incomplete' in result['answer']


def test_invalid_edit_also_invalidates_previous_approval(setup):
    db, context, _ = setup
    first = planner.prepare(db, context, 'session-123456789', event(), Calendar())
    with pytest.raises(ValueError):
        planner.prepare(db, context, 'session-123456789', {**event(), 'end_at': ''}, Calendar())
    assert confirmed(db, context, first, Calendar())['state'] == 'SUPERSEDED'


@pytest.mark.parametrize('text', ['Am I free tomorrow?', 'Show my meetings', 'main free hoon?',
                                 'मेरी बैठक कब है?', 'क्या मैं खाली हूँ?'])
def test_natural_schedule_questions_route_to_calendar(text):
    assert is_planner_request(text)


def test_meeting_mail_requests_keep_the_gmail_path():
    assert not is_planner_request('Find my meeting email')


def test_rescheduling_keeps_existing_attendees_and_location(setup):
    db, context, _ = setup
    provider = Calendar()
    provider.event.update(attendees=[{'email': 'person@example.test'}], location='Office')
    proposal = planner.prepare(db, context, 'session-123456789', event('edit_event'), provider)
    assert proposal['details']['attendees'] == [{'email': 'person@example.test'}]
    assert proposal['details']['location'] == 'Office'
