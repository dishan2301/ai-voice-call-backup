from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.email import google_oauth


@pytest.mark.parametrize("email_verified,profile_email,expected_error", [
    (True, "pilot@example.test", None),
    (False, "pilot@example.test", "not verified"),
    (True, "other@example.test", "does not match"),
])
def test_google_exchange_binds_verified_subject_to_mailbox_profile(
        monkeypatch, email_verified, profile_email, expected_error):
    credentials = SimpleNamespace(
        refresh_token="synthetic-refresh",
        to_json=lambda: '{"token":"synthetic-access","refresh_token":"synthetic-refresh"}',
    )
    flow = SimpleNamespace(
        oauth2session=SimpleNamespace(token={
            "scope": "openid https://www.googleapis.com/auth/gmail.readonly",
            "id_token": "synthetic-id-token",
        }),
        credentials=credentials,
        fetch_token=lambda **kwargs: None,
    )
    monkeypatch.setattr(google_oauth, "_flow", lambda *args, **kwargs: flow)
    monkeypatch.setattr(google_oauth, "verify_oauth2_token", lambda *args: {
        "sub": "stable-google-subject", "email": "pilot@example.test",
        "email_verified": email_verified,
    })

    class FakeSession:
        def __init__(self, _credentials):
            pass

        def get(self, url, timeout):
            assert url.endswith("/users/me/profile") and timeout == 15
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"emailAddress": profile_email})

        def close(self):
            pass

    monkeypatch.setattr(google_oauth, "AuthorizedSession", FakeSession)
    settings = Settings(google_web_client_id="synthetic-client")
    if expected_error:
        with pytest.raises(google_oauth.GoogleOAuthError, match=expected_error):
            google_oauth.exchange_code(settings, "synthetic-code", "synthetic-verifier")
    else:
        result = google_oauth.exchange_code(settings, "synthetic-code", "synthetic-verifier")
        assert result.subject == "stable-google-subject"
        assert result.email == "pilot@example.test"
        assert result.credentials["read"]["refresh_token"] == "synthetic-refresh"
