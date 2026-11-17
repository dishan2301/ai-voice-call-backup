import asyncio
from unittest.mock import Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_hosted_auth import hosted
from app.core.auth import MailboxContext
from app.core.config import Settings
from app.main import app
from app.api import calendar_oauth
from app.services.calendar_provider import CALENDAR_SCOPES


def test_calendar_and_reminder_routes_require_authentication(hosted):
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for path in ['/phone/planner/ask', '/phone/planner/prepare', '/phone/planner/confirm',
                         '/phone/planner/notifications/claim', '/phone/planner/notifications/ack']:
                response = await client.post(path, json={})
                assert response.status_code == 401
            landing = await client.get('/auth/calendar/callback?code=synthetic&state=synthetic')
            assert landing.status_code == 200 and 'calendar-consent.js' in landing.text
            finish = await client.post('/auth/calendar/finish', json={'code': 'synthetic', 'state': 'synthetic'})
            assert finish.status_code == 401
    asyncio.run(exercise())


def test_http_reminder_confirmation_and_cross_account_denial(hosted):
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            headers = {'Authorization': 'Bearer alice-token'}
            response = await client.post('/phone/planner/prepare', headers=headers, json={
                'session_key': 'planner-session-1234', 'details': {'operation': 'create_reminder',
                    'title': 'Call supplier', 'scheduled_at': '2026-12-01T10:00:00+05:30', 'timezone': 'Asia/Kolkata'}})
            assert response.status_code == 200, response.text
            proposal = response.json()
            body = {'session_key': 'planner-session-1234', 'action_id': proposal['action_id'],
                    'confirmation_token': proposal['confirmation_token'], 'confirmation': 'confirm'}
            denied = await client.post('/phone/planner/confirm', headers={'Authorization': 'Bearer bob-token'}, json=body)
            assert denied.status_code == 409
            done = await client.post('/phone/planner/confirm', headers=headers, json=body)
            assert done.json()['state'] == 'DONE'
            again = await client.post('/phone/planner/confirm', headers=headers, json=body)
            assert again.json()['state'] == 'DONE'
    asyncio.run(exercise())


@pytest.mark.parametrize('identity,scopes,success', [
    ('provider-alice', CALENDAR_SCOPES, True), ('provider-bob', CALENDAR_SCOPES, False),
    ('provider-alice', [], False)])
def test_incremental_calendar_oauth_keeps_gmail_grants_separate(hosted, identity, scopes, success):
    context = MailboxContext('alice', 'acme', 'alice-mail', 'demo', 'session-alice')
    request = Mock()
    request.cookies = {'calendar_oauth': 'browser-bound'}
    transactions = Mock()
    transactions.claim.return_value.code_verifier = 'verifier'
    consent = Mock()
    consent.oauth2session.token = {'id_token': 'synthetic', 'scope': ' '.join(scopes)}
    consent.credentials.refresh_token = 'synthetic'
    consent.credentials.to_json.return_value = '{"token":"synthetic"}'
    claims = {'sub': identity, 'email': 'alice@example.test', 'email_verified': True}
    with hosted() as db, patch.object(calendar_oauth, 'setup', return_value=(Settings(), transactions)), \
            patch.object(calendar_oauth, 'flow', return_value=consent), \
            patch.object(calendar_oauth, 'verify_oauth2_token', return_value=claims), \
            patch.object(calendar_oauth, 'store_credential') as store:
        from fastapi import HTTPException
        if success:
            assert calendar_oauth.callback(request, 'code', 'state', '', context, db)['status'] == 'calendar_connected'
            assert store.call_args.args[2] == 'CALENDAR'
        else:
            with pytest.raises(HTTPException):
                calendar_oauth.callback(request, 'code', 'state', '', context, db)
            store.assert_not_called()
        transactions.claim.assert_called_once_with(db, 'state', 'calendar', '', 'browser-bound:session-alice')
