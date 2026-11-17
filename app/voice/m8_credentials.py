"""Dedicated M8 settings; never fall back to personal application credentials."""
from scripts.m8_test_credentials import load_test_key


def test_live_settings(settings, path=None):
    key = load_test_key() if path is None else load_test_key(path)
    return settings.model_copy(update={'gemini_api_key': key, 'gemini_reply_api_key': key})
