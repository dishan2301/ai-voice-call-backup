from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.database.db import Base
from app.database.models import Briefing
from app.services.speech_service import SpeechService
from app.speech.base import TTSProvider
from app.speech.system_tts import SystemTTSProvider


class MockTTS(TTSProvider):
    def __init__(self):
        self.played = []
        self.stopped = False

    def play(self, text: str) -> None:
        self.played.append(text)

    def stop(self) -> None:
        self.stopped = True

    def replay(self) -> None:
        self.play(self.played[-1])


def _briefing_database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.utcnow()
    with sessions() as session:
        briefing = Briefing(
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
            spoken_brief="Good morning. One item needs attention.",
        )
        session.add(briefing)
        session.commit()
        return sessions, briefing.id


def test_speech_requires_explicit_service_call_and_can_stop() -> None:
    sessions, briefing_id = _briefing_database()
    provider = MockTTS()
    service = SpeechService(provider, Settings(tts_enabled=True), sessions)

    assert provider.played == []
    assert service.speak(briefing_id) == "SPEAKING"
    service.stop()

    assert provider.played == ["Good morning. One item needs attention."]
    assert provider.stopped is True
    with sessions() as session:
        briefing = session.get(Briefing, briefing_id)
        assert briefing.consent_decision == "YES"
        assert briefing.status == "SPEAKING"


def test_system_tts_passes_email_text_as_one_argument_without_shell() -> None:
    process = Mock()
    process.poll.return_value = 0
    with patch("app.speech.system_tts.subprocess.Popen", return_value=process) as popen:
        provider = SystemTTSProvider(executable="/usr/bin/spd-say")
        provider.play("Hello; touch /tmp/should-not-exist")

    command = popen.call_args.args[0]
    assert command[-1] == "Hello; touch /tmp/should-not-exist"
    assert popen.call_args.kwargs["shell"] is False
