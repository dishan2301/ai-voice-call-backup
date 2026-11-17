import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_schedule_is_explicitly_timezone_aware() -> None:
    settings = Settings()

    assert settings.app_timezone == "Asia/Kolkata"
    assert settings.briefing_time == "09:30"
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.gemini_live_model == "gemini-3.8-live"
    assert str(settings.timezone) == "Asia/Kolkata"


def test_invalid_schedule_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(briefing_time="25:99")


def test_mailbox_id_is_a_safe_scope_identifier() -> None:
    assert Settings(mailbox_id="work").mailbox_id == "work"
    with pytest.raises(ValidationError):
        Settings(mailbox_id="gmail:work")


def test_voice_ice_configuration_accepts_stun_turn_without_embedded_credentials() -> None:
    settings = Settings(voice_ice_urls=["stun:stun.example:3478", "turns:turn.example:5349"],
                        voice_ice_username="temporary-user", voice_ice_credential="temporary-secret")
    assert settings.voice_ice_urls == ["stun:stun.example:3478", "turns:turn.example:5349"]
    with pytest.raises(ValidationError):
        Settings(voice_ice_urls=["turn:user:secret@turn.example:3478"])
    with pytest.raises(ValidationError):
        Settings(voice_ice_urls=["https://not-an-ice-server.example"])
