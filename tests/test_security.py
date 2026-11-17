import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from fastapi.responses import HTMLResponse

from app.database.db import create_tables
from app.main import app


ROOT = Path(__file__).resolve().parents[1]


def test_host_and_origin_boundaries_and_security_headers(monkeypatch) -> None:
    from app.core.config import get_settings
    app_settings = get_settings().model_copy(update={
        "allowed_hosts": ["test", "phone.example"],
        "phone_install_code": "synthetic-pairing-code", "phone_api_token": "synthetic-api-token",
    })
    monkeypatch.setattr("app.core.security.get_settings", lambda: app_settings)
    monkeypatch.setattr("app.core.auth.get_settings", lambda: app_settings)
    monkeypatch.setattr("app.api.phone.get_settings", lambda: app_settings)
    async def exercise():
        create_tables()
        async def request(awaitable):
            return await asyncio.wait_for(awaitable, timeout=3)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            safe = await request(client.get("/health"))
            hostile_origin = await request(client.patch(
                "/settings",
                headers={"Origin": "https://attacker.example"},
                json={"briefing_time": "08:00"},
            ))
            browser_form = await request(client.post(
                "/dashboard/speech/stop",
                headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
            ))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.example"
        ) as client:
            hostile_host = await request(client.get("/health"))
            remote_without_token = await request(client.get("/phone/briefing"))
            remote_with_token = await request(client.get(
                "/phone/briefing",
                headers={"Authorization": "Bearer " + app_settings.phone_api_token},
            ))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://phone.example"
        ) as client:
            unpaired_preview = await request(client.get("/phone/preview"))
            wrong_code_preview = await request(client.get("/phone/preview", params={"code": "wrong"}))
            paired = await request(client.get("/phone/preview", params={"code": app_settings.phone_install_code},
                                              follow_redirects=False))
            clean_preview = await request(client.get("/phone/preview"))
            cookie_brief = await request(client.get("/phone/briefing"))
            cookie_recent = await request(client.get("/phone/recent"))
        return (safe, hostile_origin, hostile_host, browser_form, remote_without_token,
                remote_with_token, unpaired_preview, wrong_code_preview, paired,
                clean_preview, cookie_brief, cookie_recent)

    (safe, hostile_origin, hostile_host, browser_form, remote_without_token,
     remote_with_token, unpaired_preview, wrong_code_preview, paired, clean_preview,
     cookie_brief, cookie_recent) = asyncio.run(exercise())

    assert safe.status_code == 200
    assert safe.headers["x-frame-options"] == "DENY"
    assert "form-action 'self'" in safe.headers["content-security-policy"]
    assert hostile_origin.status_code == 403
    assert hostile_host.status_code == 403
    assert remote_without_token.status_code == 403
    assert remote_with_token.status_code == 403  # A bearer token cannot authorize an untrusted Host.
    assert unpaired_preview.status_code == 200
    assert "name=\"code\"" in unpaired_preview.text
    assert wrong_code_preview.status_code == 200
    assert "code was not accepted" in wrong_code_preview.text
    assert paired.status_code == 303
    assert paired.headers["location"] == "/phone/preview"
    assert "httponly" in paired.headers["set-cookie"].lower()
    assert "secure" in paired.headers["set-cookie"].lower()
    assert clean_preview.status_code == 200
    assert cookie_brief.status_code in {200, 404}
    assert cookie_recent.status_code == 200
    assert browser_form.status_code == 303


def test_security_middleware_returns_application_errors_without_stalling(monkeypatch) -> None:
    from fastapi import FastAPI, HTTPException

    from app.core.config import Settings
    from app.core.security import LocalSecurityMiddleware

    settings = Settings(deployment_mode="hosted", allowed_hosts=["test"])
    monkeypatch.setattr("app.core.security.get_settings", lambda: settings)
    test_app = FastAPI()
    test_app.add_middleware(LocalSecurityMiddleware)

    @test_app.get("/phone/recent")
    async def unauthorized():
        raise HTTPException(status_code=401, detail="session required")

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
            return await asyncio.wait_for(client.get("/phone/recent"), timeout=3)

    response = asyncio.run(exercise())
    assert response.status_code == 401
    assert response.headers["x-frame-options"] == "DENY"


def test_source_has_no_unapproved_gmail_mutation_or_shell_execution() -> None:
    runtime_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app").rglob("*.py")
    )
    compact = runtime_source.replace(" ", "").lower()

    assert ".messages().modify(" not in compact
    assert ".messages().delete(" not in compact
    assert "send_message" in compact
    assert "shell=true" not in compact


def test_secrets_and_private_data_are_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in ignored
    assert "secrets/*" in ignored
    assert "*.db" in ignored
