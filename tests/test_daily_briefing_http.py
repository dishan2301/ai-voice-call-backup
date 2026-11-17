import asyncio
from datetime import datetime, timedelta

from httpx import ASGITransport, AsyncClient
from tests.test_hosted_auth import hosted
from app.main import app
from app.database.models import DailyBriefingRun


def test_authenticated_notification_tap_and_schedule_isolation(hosted):
    with hosted() as db:
        db.add(DailyBriefingRun(id='a'*32, user_id='alice', mailbox_id='alice-mail', local_date='2026-11-05',
            scheduled_at=datetime.utcnow() - timedelta(hours=6), generated_at=datetime.utcnow() - timedelta(hours=6),
            status='COMPLETE', document={'items': [], 'calendar': []}))
        db.commit()
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for path in ['/phone/daily/schedule', '/phone/daily/runs', '/phone/daily/runs/' + 'a'*32]:
                assert (await client.get(path)).status_code == 401
            alice = {'Authorization': 'Bearer alice-token'}
            bob = {'Authorization': 'Bearer bob-token'}
            url = '/phone/daily/runs/' + 'a'*32
            assert (await client.get(url, headers=bob)).status_code == 404
            value = await client.get(url, headers=alice)
            assert value.status_code == 200 and value.json()['stale']
            assert (await client.post(url + '/open', headers=bob, json={'session_key': 'same-session-12345'})).status_code == 404
            assert (await client.post(url + '/open', headers=alice, json={'session_key': 'same-session-12345'})).status_code == 200
            saved = await client.post('/phone/daily/schedule', headers=alice, json={'enabled': True, 'time': '09:00'})
            assert saved.status_code == 200
            other = await client.get('/phone/daily/schedule', headers=bob)
            assert not other.json()['enabled']
            invalid = await client.post('/phone/daily/schedule', headers=alice, json={'enabled': True, 'time': '25:00'})
            assert invalid.status_code == 422
            disabled = await client.post('/phone/daily/schedule', headers=alice, json={'enabled': False})
            assert not disabled.json()['enabled']
    asyncio.run(exercise())
