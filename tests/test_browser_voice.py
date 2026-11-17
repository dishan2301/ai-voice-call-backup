import pytest
from starlette.testclient import TestClient
from deploy.cloud_app import app
from deploy import browser_voice


def test_rejects_other_origin_and_bad_access_code(monkeypatch):
    monkeypatch.setenv('VOICE_PREVIEW_TOKEN','synthetic-private-code')
    client=TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect('/talk/ws',headers={'origin':'https://evil.example'}):pass
    with client.websocket_connect('/talk/ws',headers={'origin':'https://testserver'}) as ws:
        ws.send_json({'sample_rate':48000,'token':'incorrect'})
        assert ws.receive_json()['type']=='error'
    assert not browser_voice.active


def test_valid_code_starts_general_voice_without_mail_or_real_provider(monkeypatch):
    monkeypatch.setenv('VOICE_PREVIEW_TOKEN','synthetic-private-code')
    import asyncio
    class FakeLive:
        def __init__(self,*a):self.closed=False
        async def start(self):pass
        async def receive(self):await asyncio.Event().wait()
        async def audio(self,pcm):pass
        async def close(self):self.closed=True
    monkeypatch.setattr(browser_voice,'OutlookConversationLive',FakeLive)
    with TestClient(app).websocket_connect('/talk/ws',headers={'origin':'https://testserver'}) as ws:
        ws.send_json({'sample_rate':48000,'token':'synthetic-private-code'})
        result=ws.receive_json()
        assert result['type']=='ready' and result['mail_connected'] is False
        ws.send_text('stop')
    assert not browser_voice.active


def test_phone_call_page_loads_without_private_configuration():
    response=TestClient(app).get('/talk')
    assert response.status_code==200
    assert 'type="password"' in response.text
