from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.briefings import router as briefings_router
from app.api.auth import router as auth_router
from app.api.microsoft_oauth import router as microsoft_oauth_router
from app.api.planner import router as planner_router
from app.api.daily_briefing import router as daily_briefing_router
from app.api.calendar_oauth import router as calendar_oauth_router
from app.api.dashboard import router as dashboard_router
from app.api.decisions import router as decisions_router
from app.api.emails import router as emails_router
from app.api.health import router as health_router
from app.api.items import router as items_router
from app.api.phone import router as phone_router
from app.api.settings import router as settings_router
from app.api.speech import router as speech_router
from app.api.voice import router as voice_router, close_all
from app.api.receptionist import router as receptionist_router
from app.agents.draft_safety import recover_stale_sends
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import LocalSecurityMiddleware
from app.database.db import SessionLocal, create_tables
from app.scheduler.morning_job import start_scheduler, stop_scheduler


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    if settings.scheduler_enabled and settings.deployment_mode == "personal":
        start_scheduler()
    logger.info("application_start", extra={"environment": settings.app_env})
    yield
    await close_all()
    if settings.scheduler_enabled and settings.deployment_mode == "personal":
        stop_scheduler()


app = FastAPI(title="Morning Mail PA", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],
)
app.add_middleware(LocalSecurityMiddleware)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(microsoft_oauth_router)
app.include_router(planner_router)
app.include_router(daily_briefing_router)
app.include_router(calendar_oauth_router)
app.include_router(briefings_router)
app.include_router(emails_router)
app.include_router(items_router)
app.include_router(phone_router)
app.include_router(settings_router)
app.include_router(speech_router)
app.include_router(voice_router)
app.include_router(receptionist_router)
app.include_router(dashboard_router)
app.include_router(decisions_router)
