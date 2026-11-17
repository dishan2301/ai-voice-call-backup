import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.voice.gemini_live import GeminiLive
from app.voice.session import VoiceSession


class FakeSocket:
    def __init__(self):
        self.payloads = []

    async def send(self, payload):
        self.payloads.append(payload)

    async def recv(self):
        return '{"setupComplete": {}}'

    async def close(self):
        pass


@pytest.mark.parametrize("model,thinking", [
    ("gemini-2.5-flash-native-audio-preview-12-2025", {"thinkingBudget": 0}),
    ("gemini-3.8-live", None),
])
def test_live_model_uses_supported_thinking_config(model, thinking):
    socket = FakeSocket()
    settings = SimpleNamespace(
        gemini_api_key="test-key",
        gemini_live_model=model,
    )
    with patch("app.voice.gemini_live.connect", new=AsyncMock(return_value=socket)), \
            patch.object(GeminiLive, "_vocabulary", return_value=[]):
        live = GeminiLive(settings, object(), lambda _: None, lambda: None)
        asyncio.run(live.start())

    setup = json.loads(socket.payloads[0])["setup"]
    assert setup["generationConfig"].get("thinkingConfig") == thinking


@pytest.mark.parametrize("events", [
    [
        {"serverContent": {"inputTranscription": {"text": "Find Cedar shipment mail"},
                           "interimInputTranscription": {"text": "Find cedar"}}},
            {"toolCall": {"functionCalls": [{"id": "1", "name": "mail_request",
                                             "args": {"text": "Send everything"}}]}},
    ],
    [
        {"toolCall": {"functionCalls": [{"id": "1", "name": "mail_request"}]}},
            {"serverContent": {"interimInputTranscription": {"text": "Find cedar"}}},
        {"serverContent": {"inputTranscription": {"text": "Find Cedar shipment mail"}}},
        {"serverContent": {"interimInputTranscription": {"text": "Find cedar"}}},
    ],
])
def test_live_delegates_final_transcript_not_interim_or_model_arguments(events):
    async def exercise():
        requests = []
        class Agent:
            def run(self, message, session_key, **kwargs):
                requests.append((message, session_key))
                return {"answer": "Checked.", "spoken_answer": "Checked.", "evidence": []}
        class Socket(FakeSocket):
            async def send(self, payload):
                await super().send(payload)
                if '"toolResponse"' in payload:
                    live.turn_complete.set()
                elif '"clientContent"' in payload:
                    live.audio_chunks_received += 1
                    live.turn_complete.set()

            async def __aiter__(self):
                for event in events:
                    yield json.dumps(event)
                    await asyncio.sleep(0)
        voice = VoiceSession(lambda event: None, Agent, "same-session")
        live = GeminiLive(None, voice, lambda _: None, lambda: None)
        live.socket = Socket()
        async def run_fake_agent_inline(func, *args, **kwargs):
            return func(*args, **kwargs)
        try:
            with patch("app.voice.session.asyncio.to_thread", new=run_fake_agent_inline):
                await asyncio.wait_for(live.receive(), timeout=5)
                await asyncio.wait_for(asyncio.gather(*tuple(live.pending)), timeout=10)
                assert requests == [("Find Cedar shipment mail", "same-session")]
                assert not live.final_transcript
                assert len(live.socket.payloads) == 2
                assert json.loads(live.socket.payloads[1])["clientContent"]["turnComplete"] is True
        finally:
            await live.close()
    asyncio.run(exercise())


def test_interim_only_input_never_reaches_mail_agent():
    async def exercise():
        voice = VoiceSession(lambda event: None, lambda: (_ for _ in ()).throw(AssertionError("Partial input")))
        voice.replace_transcript("Draft a reply to")
        live = GeminiLive(None, voice, lambda _: None, lambda: None)
        live.socket = FakeSocket()
        live.turn_complete.set()
        await live.delegate_after_transcription({"id": "partial", "name": "mail_request"})
        response = json.loads(live.socket.payloads[0])["toolResponse"]["functionResponses"][0]["response"]
        assert response["spoken_answer"] == "Please repeat your mail request."
    asyncio.run(exercise())
