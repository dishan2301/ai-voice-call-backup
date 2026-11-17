from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.database.db import SessionLocal
from app.database.models import Setting
from app.services.morning_job import run_scheduled_briefing
from app.services.analysis_worker import run_analysis_jobs
from app.services.settings_service import get_runtime_settings


def recover_due_reminders():
    from app.services.planner import recover_reminders
    with SessionLocal() as db:
        recover_reminders(db)


def _schedule_preferences(settings: Settings):
    briefing_time = settings.briefing_time
    timezone_name = settings.app_timezone
    with SessionLocal() as session:
        rows = {
            row.key: row.value_json
            for row in session.scalars(
                select(Setting).where(Setting.key.in_(["briefing_time", "app_timezone"]))
            ).all()
        }
    return rows.get("briefing_time", briefing_time), rows.get("app_timezone", timezone_name)


def create_scheduler(
    settings: Optional[Settings] = None,
    job_function: Callable[[], None] = run_scheduled_briefing,
    persistent: bool = True,
) -> BackgroundScheduler:
    settings = settings or get_runtime_settings()
    scheduler_options = {"timezone": settings.timezone}
    # Jobs are recreated from saved preferences at startup. Keep the job store
    # in memory so it never opens the encrypted mailbox through a plain driver.
    scheduler = BackgroundScheduler(**scheduler_options)
    briefing_time, timezone_name = _schedule_preferences(settings)
    hour, minute = map(int, briefing_time.split(":"))
    scheduler.add_job(
        job_function,
        CronTrigger(hour=hour, minute=minute, timezone=timezone_name),
        id="morning-mail-pa-daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_analysis_jobs,
        "interval",
        minutes=1,
        id="morning-mail-analysis-worker",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(recover_due_reminders, "interval", minutes=1, id="reminder-recovery",
                      replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=60)
    from app.services.daily_briefing import tick
    scheduler.add_job(tick, 'interval', minutes=1, id='owned-daily-briefings',
                      replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=60)
    return scheduler


_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = create_scheduler()
    return _scheduler


def start_scheduler() -> BackgroundScheduler:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    _scheduler = None


def refresh_scheduler_schedule() -> None:
    if _scheduler is None or not _scheduler.running:
        return
    settings = get_settings()
    briefing_time, timezone_name = _schedule_preferences(settings)
    hour, minute = map(int, briefing_time.split(":"))
    _scheduler.reschedule_job(
        "morning-mail-pa-daily",
        trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone_name),
    )
