from functools import lru_cache
import logging
from typing import Optional

from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.database.db import SessionLocal
from app.database.models import Briefing
from app.speech.base import TTSError, TTSProvider
from app.speech.system_tts import SystemTTSProvider
from app.services.settings_service import get_runtime_settings


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_tts_provider() -> TTSProvider:
    settings = get_runtime_settings()
    if settings.tts_provider != "system":
        raise TTSError(f"Unsupported TTS_PROVIDER: {settings.tts_provider}")
    return SystemTTSProvider(settings.tts_rate, settings.tts_volume)


async def tts_provider_dependency() -> TTSProvider:
    return get_tts_provider()


class SpeechService:
    def __init__(
        self,
        provider: TTSProvider,
        settings: Optional[Settings] = None,
        session_factory: sessionmaker = SessionLocal,
    ):
        self.provider = provider
        self.settings = settings or get_runtime_settings()
        self.session_factory = session_factory

    def speak(self, briefing_id: int) -> str:
        if not self.settings.tts_enabled:
            raise TTSError("Text-to-speech is disabled")
        with self.session_factory() as session:
            briefing = session.get(Briefing, briefing_id)
            if not briefing:
                raise LookupError("Briefing not found")
            spoken_brief = briefing.spoken_brief
            briefing.consent_decision = "YES"
            briefing.status = "SPEAK_AUTHORIZED"
            session.commit()
        try:
            self.provider.play(spoken_brief)
        except Exception:
            with self.session_factory() as session:
                briefing = session.get(Briefing, briefing_id)
                briefing.status = "READY"
                session.commit()
            raise
        with self.session_factory() as session:
            briefing = session.get(Briefing, briefing_id)
            briefing.status = "SPEAKING"
            session.commit()
        logger.info("tts_start", extra={"briefing_id": briefing_id})
        return "SPEAKING"

    def stop(self) -> None:
        self.provider.stop()
        logger.info("tts_stop")
