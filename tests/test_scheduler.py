from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database.db import Base
from app.database.models import Briefing
from app.desktop.morning_prompt import PromptDecision
from app.scheduler.morning_job import create_scheduler
from app.services.morning_job import MorningJob


def _sessions_with_briefing():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.utcnow()
    with sessions() as session:
        brief = Briefing(
            briefing_date=date.today(),
            generated_at=now,
            period_start=now - timedelta(days=1),
            period_end=now,
            total_emails=1,
            critical_count=0,
            high_count=1,
            reply_count=1,
            action_count=0,
            low_priority_count=0,
            detailed_brief="Details",
            spoken_brief="Spoken",
        )
        session.add(brief)
        session.commit()
        return sessions, brief.id


def test_scheduler_uses_0930_asia_kolkata_and_single_instance() -> None:
    scheduler = create_scheduler(
        Settings(briefing_time="09:30", app_timezone="Asia/Kolkata"),
        job_function=lambda: None,
        persistent=False,
    )

    job = scheduler.get_job("morning-mail-pa-daily")

    assert str(job.trigger.timezone) == "Asia/Kolkata"
    assert str(job.trigger.fields[5]) == "9"
    assert str(job.trigger.fields[6]) == "30"
    assert job.max_instances == 1


def test_no_stores_brief_and_never_calls_speech() -> None:
    sessions, briefing_id = _sessions_with_briefing()
    speech = Mock()
    job = MorningJob(Mock(), Mock(), speech, session_factory=sessions)

    job.handle_decision(briefing_id, PromptDecision.NO, datetime.now(timezone.utc))

    speech.speak.assert_not_called()
    with sessions() as session:
        brief = session.get(Briefing, briefing_id)
        assert brief.status == "DECLINED"
        assert brief.detailed_brief == "Details"


def test_yes_is_idempotent_and_reminder_is_bounded() -> None:
    sessions, briefing_id = _sessions_with_briefing()
    speech = Mock()
    scheduler = Mock()
    job = MorningJob(Mock(), Mock(), speech, scheduler=scheduler, session_factory=sessions)
    now = datetime.now(timezone.utc)

    job.handle_decision(briefing_id, PromptDecision.REMIND, now)
    job.handle_decision(briefing_id, PromptDecision.REMIND, now)

    scheduler.add_job.assert_called_once()
    speech.speak.assert_not_called()
    with sessions() as session:
        brief = session.get(Briefing, briefing_id)
        assert brief.status == "DECLINED"
        assert brief.reminder_count == 1


def test_yes_calls_speech_once_for_repeated_prompt_response() -> None:
    sessions, briefing_id = _sessions_with_briefing()
    speech = Mock()

    def mark_speaking(target_id):
        with sessions() as session:
            brief = session.get(Briefing, target_id)
            brief.status = "SPEAKING"
            session.commit()

    speech.speak.side_effect = mark_speaking
    job = MorningJob(Mock(), Mock(), speech, session_factory=sessions)
    now = datetime.now(timezone.utc)

    job.handle_decision(briefing_id, PromptDecision.YES, now)
    job.handle_decision(briefing_id, PromptDecision.YES, now)

    speech.speak.assert_called_once_with(briefing_id)


def test_disabled_and_hosted_lifespan_never_start_scheduler(monkeypatch):
    import asyncio
    import importlib
    module = importlib.import_module('app.main')
    from unittest.mock import AsyncMock
    start, stop = Mock(), Mock()
    monkeypatch.setattr(module, 'create_tables', Mock())
    monkeypatch.setattr(module, 'start_scheduler', start)
    monkeypatch.setattr(module, 'stop_scheduler', stop)
    monkeypatch.setattr(module, 'close_all', AsyncMock())
    async def exercise():
        for enabled, mode in [(False, 'personal'), (True, 'hosted')]:
            monkeypatch.setattr(module, 'settings', Settings(scheduler_enabled=enabled, deployment_mode=mode))
            async with module.lifespan(module.app):
                pass
    asyncio.run(exercise())
    start.assert_not_called()
    stop.assert_not_called()
