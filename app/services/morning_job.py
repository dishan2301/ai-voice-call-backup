from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.database.db import SessionLocal
from app.database.models import Briefing, BriefingRun
from app.desktop.morning_prompt import DesktopPrompt, PromptDecision
from app.services.persistent_briefing_service import PersistentBriefingService
from app.services.speech_service import SpeechService
from app.services.settings_service import get_runtime_settings


logger = logging.getLogger(__name__)


class MorningJob:
    def __init__(
        self,
        generation_service: PersistentBriefingService,
        prompt: DesktopPrompt,
        speech_service: SpeechService,
        scheduler=None,
        settings: Optional[Settings] = None,
        session_factory: sessionmaker = SessionLocal,
        max_reminders: int = 1,
    ):
        self.generation_service = generation_service
        self.prompt = prompt
        self.speech_service = speech_service
        self.scheduler = scheduler
        self.settings = settings or get_runtime_settings()
        self.session_factory = session_factory
        self.max_reminders = max_reminders

    def run(self, now: Optional[datetime] = None) -> PromptDecision:
        moment = now or datetime.now(timezone.utc)
        logger.info("scheduler_job_start", extra={"scheduled_for": moment.isoformat()})
        self.generation_service.generate(moment)
        with self.session_factory() as session:
            briefing_id = session.scalar(
                select(BriefingRun.briefing_id)
                .where(
                    BriefingRun.provider == self.settings.email_provider,
                    BriefingRun.status == "COMPLETE",
                )
                .order_by(BriefingRun.period_end.desc())
                .limit(1)
            )
        if not briefing_id:
            raise RuntimeError("A complete briefing was not published")
        return self.prompt_existing(briefing_id, moment)

    def prompt_existing(
        self, briefing_id: int, now: Optional[datetime] = None
    ) -> PromptDecision:
        decision = self.prompt.ask()
        logger.info("desktop_prompt_decision", extra={"briefing_id": briefing_id, "decision": decision.value})
        self.handle_decision(briefing_id, decision, now or datetime.now(timezone.utc))
        return decision

    def handle_decision(
        self, briefing_id: int, decision: PromptDecision, now: datetime
    ) -> None:
        with self.session_factory() as session:
            briefing = session.get(Briefing, briefing_id)
            if not briefing:
                raise LookupError("Briefing not found")
            if decision == PromptDecision.YES:
                if briefing.consent_decision == "YES" and briefing.status in {"SPEAKING", "SPOKEN"}:
                    return
                briefing.consent_decision = "YES"
                session.commit()
            elif decision == PromptDecision.NO:
                briefing.consent_decision = "NO"
                briefing.status = "DECLINED"
                session.commit()
                return
            else:
                if briefing.reminder_count >= self.max_reminders or not self.scheduler:
                    briefing.consent_decision = "NO"
                    briefing.status = "DECLINED"
                    session.commit()
                    return
                briefing.reminder_count += 1
                briefing.status = "READY"
                session.commit()
                self.scheduler.add_job(
                    prompt_existing_briefing,
                    "date",
                    run_date=now + timedelta(minutes=15),
                    args=[briefing_id],
                    id=f"briefing-reminder-{briefing_id}",
                    replace_existing=True,
                )
                return
        self.speech_service.speak(briefing_id)


def _build_job(scheduler=None) -> MorningJob:
    from app.api.dependencies import create_generation_service
    from app.services.speech_service import get_tts_provider

    return MorningJob(
        create_generation_service(),
        DesktopPrompt(),
        SpeechService(get_tts_provider()),
        scheduler=scheduler,
        settings=get_runtime_settings(),
    )


def run_scheduled_briefing() -> None:
    # An opted-in owned M6 schedule replaces the legacy desktop publication path.
    from app.database.models import DailyBriefingSchedule
    from app.services.daily_briefing import run_schedule
    settings = get_runtime_settings()
    with SessionLocal() as db:
        owned_schedule = db.scalar(select(DailyBriefingSchedule.id).where(
            DailyBriefingSchedule.mailbox_id == settings.mailbox_id,
            DailyBriefingSchedule.legacy_local.is_(True), DailyBriefingSchedule.enabled.is_(True)))
    if owned_schedule:
        run_schedule(owned_schedule)
        return
    from app.scheduler.morning_job import get_scheduler

    _build_job(get_scheduler()).run()


def prompt_existing_briefing(briefing_id: int) -> None:
    from app.scheduler.morning_job import get_scheduler

    _build_job(get_scheduler()).prompt_existing(briefing_id)
