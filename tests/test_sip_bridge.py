import asyncio
from app.voice.sip_bridge import (AudioSocketParser, SipBridge, audiosocket_frame,
                                  gemini_to_pcmu, pcmu_to_gemini, rtp_payload)

def test_audiosocket_is_tcp_framed_not_rtp():
    parser = AudioSocketParser()
    first = audiosocket_frame(0x10, b"abc")
    second = audiosocket_frame(0x10, b"defg")
    assert parser.feed(first[:2]) == []
    assert parser.feed(first[2:] + second) == [(0x10, b"abc"), (0x10, b"defg")]

def test_rtp_and_codec_roundtrip_shapes():
    payload = b"\xff" * 160
    packet = b"\x80\x00\x00\x01\x00\x00\x00\x01\x12\x34\x56\x78" + payload
    assert rtp_payload(packet) == payload
    pcm, _ = pcmu_to_gemini(payload)
    encoded, _ = gemini_to_pcmu(pcm)
    assert len(pcm) >= 300 and encoded

def test_bridge_stops_audio_after_disconnect():
    class FakeLive:
        def __init__(self): self.audio_calls = 0; self.closed = False
        async def audio(self, pcm): self.audio_calls += 1
        async def close(self): self.closed = True
    async def run():
        live = FakeLive(); bridge = SipBridge(live)
        packet = b"\x80\x00\x00\x01\x00\x00\x00\x01\x12\x34\x56\x78" + b"\xff" * 160
        await bridge.inbound(packet); await bridge.close(); await bridge.inbound(packet)
        assert live.audio_calls == 1 and live.closed
    asyncio.run(run())
