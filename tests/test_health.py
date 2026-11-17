import asyncio

from httpx import ASGITransport, AsyncClient

from app.database.db import create_tables
from app.main import app


def test_health_reports_database_ready() -> None:
    async def request_health():
        create_tables()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_readiness_reports_evidence_without_secret_values() -> None:
    async def request_readiness():
        create_tables()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/health/ready")

    response = asyncio.run(request_readiness())
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] in {"ready", "needs_setup"}
    assert set(payload["checks"]) == {"supported_python", "gemini_key_configured", "gmail_read_token",
                                      "gmail_send_token", "https_certificate", "saved_briefing", "recent_verified_backup",
                                      "latest_briefing_full_ai", "production_environment",
                                      "mail_sync_checkpoint", "mail_sync_fresh", "mail_sync_status", "mail_last_sync_at"}
    assert "secret" not in str(payload).lower()


def test_readiness_rejects_fresh_corrupt_backup_and_stale_sync(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from app.api import health as module
    from app.core.config import Settings

    backup = tmp_path / 'fresh.db'
    backup.write_bytes(b'not a database')
    monkeypatch.setattr(module, 'latest_backup', lambda: backup)
    monkeypatch.setattr(module, 'get_settings', lambda: Settings(database_key_file=''))
    class DB:
        def scalar(self, query): return None
        def get(self, model, scope):
            return SimpleNamespace(last_history_id='1', status='ready',
                                   last_successful_sync_at=datetime.utcnow() - timedelta(days=1))
    result = asyncio.run(module.readiness(DB()))
    assert result['checks']['recent_verified_backup'] is False
    assert result['checks']['mail_sync_fresh'] is False
    assert result['status'] == 'needs_setup'
