"""AudioSocket adapter. Caller authorization must precede constructing this adapter."""
import asyncio
import audioop
import time

from app.services.receptionist import ReceptionistSession
from app.voice.gemini_live import GeminiLive
from app.voice.sip_bridge import audiosocket_frame
from app.voice.m8_lifecycle import SingleAttemptConnect


class ReceptionistLive(GeminiLive):
    """Reuse Gemini transport while removing mailbox vocabulary and all tools."""
    connector = staticmethod(SingleAttemptConnect)

    async def receive(self):
        # Tap decoded metadata before the unchanged Gemini receiver processes it.
        diagnostics = getattr(self, 'diagnostics', None)
        if diagnostics is None:
            return await super().receive()
        import json
        original = self.socket
        class ObservedSocket:
            def __aiter__(self):
                return self.iterate()
            async def iterate(self):
                async for raw in original:
                    diagnostics.provider(json.loads(raw))
                    yield raw
            def __getattr__(self, name):
                return getattr(original, name)
        self.socket = ObservedSocket()
        try:
            await super().receive()
        finally:
            self.socket = original
    async def send(self, payload):
        if "setup" in payload:
            setup = dict(payload["setup"])
            setup["systemInstruction"] = {"parts": [{"text": ReceptionistSession("", "").prompt()}]}
            setup.pop("tools", None)
            setup["inputAudioTranscription"] = {}
            payload = {"setup": setup}
        await super().send(payload)

    @staticmethod
    def _vocabulary():
        return []

    async def delegate_after_transcription(self, call):
        raise RuntimeError("Receptionist tools are disabled")


class AudioAdapter:
    def __init__(self, writer):
        self.writer = writer
        self.buffer = bytearray()
        self.input_state = self.output_state = None
        self.closed = False
        self.first_input = self.first_output = None

    async def input(self, live, pcm8):
        if len(pcm8) % 2:
            raise ValueError("Invalid PCM sample")
        if self.first_input is None:
            self.first_input = time.monotonic()
        pcm, self.input_state = audioop.ratecv(pcm8, 2, 1, 8000, 16000, self.input_state)
        await live.audio(pcm)

    def output(self, pcm24):
        if getattr(self, 'diagnostics', None):
            self.diagnostics.audio(pcm24)
        pcm, self.output_state = audioop.ratecv(pcm24, 2, 1, 24000, 8000, self.output_state)
        if len(self.buffer) + len(pcm) > 160000:
            raise RuntimeError("Playback buffer exhausted")
        self.buffer.extend(pcm)

    def interrupt(self):
        self.buffer.clear()
        self.output_state = None

    async def playback(self):
        while not self.closed:
            if len(self.buffer) >= 320:
                pcm = bytes(self.buffer[:320])
                del self.buffer[:320]
                self.writer.write(audiosocket_frame(0x10, pcm))
                await asyncio.wait_for(self.writer.drain(), 2)
                if getattr(self, 'diagnostics', None):
                    self.diagnostics.socket_frame()
                if self.first_output is None:
                    self.first_output = time.monotonic()
            await asyncio.sleep(.02)

    async def close(self):
        self.closed = True
        self.interrupt()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass
