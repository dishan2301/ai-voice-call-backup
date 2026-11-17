from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import MailboxContext
from app.database.db import Base
from app.database.models import DailyBriefingSchedule, DailyBriefingRun, Reminder, Email, ConversationState
from app.services import daily_briefing as service, planner
from app.api import daily_briefing as api


NOW = datetime(2026, 11, 5, 3, 30, tzinfo=timezone.utc)
CONTEXT = MailboxContext('alice', 'workspace', 'mailbox-a', 'gmail', legacy_local=True)


@pytest.fixture
def setup(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setattr(service, 'get_settings', lambda: Settings(mailbox_id='mailbox-a', email_provider='gmail'))
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        api.save_schedule(api.Preferences(enabled=True), CONTEXT, db)
    yield factory
    engine.dispose()


def snapshot(*args):
    return {'items': [{'message_id': 'a1', 'thread_id': 't1', 'sender': 'Supplier',
                      'subject': 'Verified source', 'summary': 'Please confirm the meeting.'}],
            'generated_at': NOW.isoformat(), 'calendar': []}


def test_defaults_9am_disabled_until_opt_in():
    defaults = api.Preferences()
    assert defaults.time == '09:00' and defaults.timezone == 'Asia/Kolkata' and not defaults.enabled


@pytest.mark.parametrize('delta,expected', [(-1, False), (0, True), (179, True), (181, False), (1440, True)])
def test_bounded_daily_catchup(setup, delta, expected):
    with setup() as db:
        row = db.get(DailyBriefingSchedule, service.schedule_id(CONTEXT))
        assert bool(service.due(row, NOW + timedelta(minutes=delta))) == expected


def test_restart_and_schedule_edit_do_not_duplicate_snapshot_or_notification(setup):
    builder = Mock(side_effect=snapshot)
    key = service.schedule_id(CONTEXT)
    assert service.run_schedule(key, NOW, setup, builder) == 'COMPLETE'
    assert service.run_schedule(key, NOW + timedelta(minutes=1), setup, builder) == 'ALREADY_HANDLED'
    with setup() as db:
        api.save_schedule(api.Preferences(enabled=True, time='09:01'), CONTEXT, db)
    assert service.run_schedule(key, NOW + timedelta(minutes=2), setup, builder) == 'ALREADY_HANDLED'
    builder.assert_called_once()
    with setup() as db:
        assert len(list(db.scalars(select(DailyBriefingRun)))) == 1
        assert len(list(db.scalars(select(Reminder)))) == 1


def test_provider_failure_is_bounded_and_never_published_as_fresh(setup):
    builder = Mock(side_effect=RuntimeError('private provider text must not be saved'))
    key = service.schedule_id(CONTEXT)
    for minute in [0, 11, 22]:
        assert service.run_schedule(key, NOW + timedelta(minutes=minute), setup, builder) == 'PROVIDER_OR_NETWORK_UNAVAILABLE'
    assert service.run_schedule(key, NOW + timedelta(minutes=33), setup, builder) == 'ALREADY_HANDLED'
    assert builder.call_count == 3
    with setup() as db:
        row = db.scalar(select(DailyBriefingRun))
        assert row.document == {} and row.generated_at is None
        assert row.failure_code == 'PROVIDER_OR_NETWORK_UNAVAILABLE'
        assert not list(db.scalars(select(Reminder)))


def test_expired_running_lease_can_recover_once(setup):
    key = service.schedule_id(CONTEXT)
    with setup() as db:
        schedule = db.get(DailyBriefingSchedule, key)
        date, at = service.due(schedule, NOW)
        import hashlib
        run_id = hashlib.sha256(f'{key}:{date}'.encode()).hexdigest()[:32]
        db.add(DailyBriefingRun(id=run_id, user_id=CONTEXT.user_id, mailbox_id=CONTEXT.mailbox_id,
            local_date=date, scheduled_at=at, lease='dead-worker', lease_until=at - timedelta(seconds=1), attempts=1))
        db.commit()
    assert service.run_schedule(key, NOW, setup, snapshot) == 'COMPLETE'


def test_disabling_during_sync_blocks_publication(setup):
    def disable(*args):
        with setup() as db:
            api.save_schedule(api.Preferences(enabled=False), CONTEXT, db)
        return snapshot()
    assert service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, disable) == 'DISABLED_DURING_RUN'
    with setup() as db:
        assert not list(db.scalars(select(Reminder)))


def test_no_valid_hosted_session_never_calls_gmail(setup):
    with setup() as db:
        row = db.get(DailyBriefingSchedule, service.schedule_id(CONTEXT)); row.legacy_local = False; db.commit()
    builder = Mock(side_effect=AssertionError('No provider call'))
    assert service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, builder) == 'SIGN_IN_REQUIRED'
    builder.assert_not_called()


def test_snapshot_sources_are_owned_and_literal(setup):
    with setup() as db:
        for mailbox, body in [('mailbox-a', 'Please confirm Friday.'), ('mailbox-b', 'Secret other account')]:
            db.add(Email(provider='gmail', mailbox_id=mailbox, provider_message_id='colliding-id', thread_id='t',
                sender_email='supplier@example.test', subject='Meeting', normalized_body=body,
                received_at=NOW.replace(tzinfo=None), labels_json=['UNREAD'], body_hash=mailbox))
        db.commit()
        schedule = db.get(DailyBriefingSchedule, service.schedule_id(CONTEXT))
        with patch('app.api.dependencies.create_generation_service') as factory, \
                patch('app.services.calendar_provider.calendar_provider', side_effect=ValueError('not connected')):
            document = service.build_snapshot(CONTEXT, schedule, NOW.replace(tzinfo=None), setup)
            factory.return_value.sync_only.assert_called_once()
    assert len(document['items']) == 1 and document['items'][0]['summary'] == 'Please confirm Friday.'
    assert document['calendar_status'] == 'unavailable_or_not_authorized'


def test_owned_open_seeds_references_not_remembered_facts(setup):
    service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, snapshot)
    with setup() as db:
        row = db.scalar(select(DailyBriefingRun))
        result = api.open_run(row.id, api.OpenRequest(session_key='same-explicit-session'), CONTEXT, db)
        assert result['saved_snapshot']
        state = db.scalar(select(ConversationState))
        assert state.selected_thread_id == 't1'
        assert 'summary' not in state.topics_json[0]
        other = MailboxContext('bob', 'workspace', 'mailbox-b', legacy_local=True)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            api.get_run(row.id, other, db)
        assert error.value.status_code == 404


def test_daily_notification_claims_are_private_and_separate_from_reminders(setup):
    service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, snapshot)
    with setup() as db:
        # Use the run time so this fixture is due independently of the wall clock.
        row = db.scalar(select(Reminder)); row.scheduled_at = datetime.utcnow() - timedelta(seconds=1); db.commit()
        assert planner.claim_notifications(db, CONTEXT) == []
        values = api.notifications(CONTEXT, db)['notifications']
        assert len(values) == 1 and set(values[0]) == {'id', 'delivery_token'}
        assert api.notifications(CONTEXT, db)['notifications'] == []
        planner.acknowledge(db, CONTEXT, values[0]['id'], values[0]['delivery_token'], 'SUBMITTED')
        assert api.notifications(CONTEXT, db)['notifications'] == []


def test_gemini_playback_uses_saved_snapshot_without_mail_actions(setup):
    import asyncio
    from app.voice.daily_briefing import play
    service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, snapshot)
    with setup() as db:
        run_id = db.scalar(select(DailyBriefingRun.id))
    live = Mock(pending=set()); live.send = AsyncMock()
    voice = Mock(state='LISTENING', epoch=0, closed=False, session_key='same-explicit-session')
    asyncio.run(play(live, voice, CONTEXT, run_id, setup))
    payload = live.send.call_args.args[0]
    text = payload['clientContent']['turns'][0]['parts'][0]['text']
    assert 'saved_at' in text and 'Do not claim they are current' in text
    assert 'Please confirm the meeting.' in text
    live.send.assert_awaited_once()


def test_personal_account_change_never_uses_new_accounts_credentials(setup, monkeypatch):
    from app.core.config import Settings
    monkeypatch.setattr(service, 'get_settings', lambda: Settings(mailbox_id='mailbox-b', email_provider='gmail'))
    builder = Mock()
    assert service.run_schedule(service.schedule_id(CONTEXT), NOW, setup, builder) == 'ACCOUNT_CHANGED'
    builder.assert_not_called()


def test_revoked_session_cannot_play_saved_briefing(setup):
    import asyncio
    from fastapi import HTTPException
    from app.voice.daily_briefing import play
    live = Mock(pending=set()); live.send = AsyncMock()
    voice = Mock(state='LISTENING', epoch=0, closed=False, session_key='same-explicit-session')
    with patch('app.api.daily_briefing.validate_mailbox_context', side_effect=HTTPException(401)):
        asyncio.run(play(live, voice, CONTEXT, 'a'*32, setup))
    live.send.assert_not_awaited()
    assert voice.event.call_args.args[0] == 'speech.error'


def test_legacy_daily_job_does_not_generate_a_second_briefing(setup):
    from app.services import morning_job
    from app.core.config import Settings
    with patch.object(morning_job, 'SessionLocal', setup), \
            patch.object(morning_job, 'get_runtime_settings', return_value=Settings(mailbox_id='mailbox-a')), \
            patch.object(service, 'run_schedule') as run, patch.object(morning_job, '_build_job') as old:
        morning_job.run_scheduled_briefing()
    run.assert_called_once(); old.assert_not_called()
