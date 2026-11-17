import pytest
from app.core.config import Settings
from app.voice.m8_credentials import test_live_settings as isolated_settings


def test_dedicated_key_never_changes_application_precedence(tmp_path):
    path = tmp_path / '.env.m8-test'
    path.write_text('M8_GEMINI_TEST_API_KEY=synthetic-test-value\n')
    path.chmod(0o600)
    original = Settings(gemini_api_key='original', gemini_reply_api_key='reply')
    result = isolated_settings(original, path)
    assert result.gemini_reply_api_key == 'synthetic-test-value'
    assert original.gemini_reply_api_key == 'reply'
    path.chmod(0o644)
    with pytest.raises(ValueError):
        isolated_settings(original, path)


def test_missing_dedicated_key_cannot_fall_back(tmp_path):
    with pytest.raises(FileNotFoundError):
        isolated_settings(Settings(gemini_api_key='original'), tmp_path / 'missing')
