import asyncio
from unittest.mock import patch

from aiortc import RTCPeerConnection, RTCConfiguration, RTCSessionDescription, AudioStreamTrack

from app.api.voice import Offer, offer, close_all, close_peer, ice_servers, peers, Speaker, voice_config
from app.core.auth import MailboxContext
from app.core.config import Settings


def test_real_webrtc_audio_and_event_channel_with_fake_provider():
    async def exercise():
        received = asyncio.Event()
        captured = asyncio.Event()
        events = []

        class FakeLive:
            def __init__(self, settings, voice, output, interrupt):
                self.voice, self.output = voice, output
            async def start(self): pass
            async def audio(self, pcm):
                assert len(pcm) > 0
                captured.set()
                self.output(b'\x01\x10' * 320)
                self.voice.partial('meeting mail')
            async def receive(self): await asyncio.Event().wait()
            async def close(self): pass

        client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        client.addTrack(AudioStreamTrack())
        channel = client.createDataChannel('mail-events')
        @channel.on('message')
        def message(data): events.append(data)
        tasks = []
        @client.on('track')
        def track(track):
            async def consume():
                for _ in range(200):
                    frame = await track.recv()
                    if any(bytes(frame.planes[0])):
                        received.set()
                        break
            tasks.append(asyncio.create_task(consume()))
        try:
            started = asyncio.get_running_loop().time()
            await client.setLocalDescription(await client.createOffer())
            with patch('app.api.voice.GeminiLive', FakeLive):
                answer = await offer(Offer(sdp=client.localDescription.sdp, type='offer', session_key='test-rtc-session-123'))
            signaling = asyncio.get_running_loop().time()
            await client.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type='answer'))
            await asyncio.wait_for(captured.wait(), 10)
            await asyncio.wait_for(received.wait(), 10)
            print('FAKE_VOICE_LATENCY_MS', {
                'offer_answer': round((signaling - started) * 1000, 2),
                'offer_to_first_returned_audio': round((asyncio.get_running_loop().time() - started) * 1000, 2),
            })
            for _ in range(50):
                if any('transcript.partial' in e for e in events): break
                await asyncio.sleep(.02)
            assert any('transcript.partial' in e for e in events)
            assert peers
        finally:
            await close_all()
            await client.close()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert not peers
    asyncio.run(exercise())


def test_interruption_clears_queued_pcm():
    async def exercise():
        speaker = Speaker()
        speaker.push(b'\xff\x01' * 480)
        speaker.clear()
        assert not any(bytes((await speaker.recv()).planes[0]))
    asyncio.run(exercise())


def test_close_peer_cleans_tasks_and_peer_even_if_provider_close_fails():
    from unittest.mock import AsyncMock, Mock
    from app.api.voice import close_peer
    async def exercise():
        pc = AsyncMock()
        live = AsyncMock()
        live.close.side_effect = RuntimeError('provider shutdown failure')
        voice = Mock()
        worker = asyncio.create_task(asyncio.Event().wait())
        disconnect = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        peers['cleanup-test'] = dict(pc=pc, live=live, voice=voice,
                                     tasks=[worker], disconnect_task=disconnect)
        await close_peer('cleanup-test')
        await close_peer('cleanup-test')
        assert worker.cancelled()
        await asyncio.gather(disconnect, return_exceptions=True)
        assert disconnect.cancelled()
        pc.close.assert_awaited_once()
        live.close.assert_awaited_once()
        assert 'cleanup-test' not in peers
    asyncio.run(exercise())


def test_disconnect_timer_can_close_its_own_peer_without_cancelling_cleanup():
    from unittest.mock import AsyncMock, Mock
    from app.api.voice import close_peer
    async def exercise():
        pc, live = AsyncMock(), AsyncMock()
        async def expire():
            peers['expiry-test'] = dict(pc=pc, live=live, voice=Mock(), tasks=[],
                                        disconnect_task=asyncio.current_task())
            await close_peer('expiry-test')
        await asyncio.create_task(expire())
        pc.close.assert_awaited_once()
        live.close.assert_awaited_once()
        assert 'expiry-test' not in peers
    asyncio.run(exercise())


def test_m4_ice_configuration_is_shared_with_authenticated_browser():
    settings = Settings(voice_ice_urls=["stun:stun.example:3478", "turn:turn.example:3478"],
                        voice_ice_username="ephemeral", voice_ice_credential="secret")
    servers = ice_servers(settings)
    assert servers[0].urls == settings.voice_ice_urls
    assert servers[0].username == "ephemeral"

    async def exercise():
        with patch("app.api.voice.get_settings", return_value=settings):
            return await voice_config(MailboxContext(
                "user", "workspace", "mailbox", "gmail", "session"))
    browser = asyncio.run(exercise())
    assert browser == {"iceServers": [{"urls": settings.voice_ice_urls,
                                        "username": "ephemeral", "credential": "secret"}],
                       "maxReconnectAttempts": 2}


def test_m4_stop_refuses_another_mailbox_voice_session():
    from fastapi import HTTPException
    from unittest.mock import AsyncMock, Mock
    from app.api.voice import stop

    async def exercise():
        peers["private-peer"] = {"owner": ("mailbox-a", "session-key"),
                                  "pc": AsyncMock(), "live": AsyncMock(),
                                  "voice": Mock(), "tasks": []}
        try:
            try:
                await stop("private-peer", MailboxContext(
                    "other", "workspace", "mailbox-b", "gmail", "other-session"))
                raise AssertionError("foreign mailbox closed the peer")
            except HTTPException as exc:
                assert exc.status_code == 404
            assert "private-peer" in peers
        finally:
            await close_peer("private-peer")
    asyncio.run(exercise())
