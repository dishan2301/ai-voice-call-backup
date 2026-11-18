"""Loopback mock-only receptionist. Run with python -m scripts.m8_receptionist_server.

An operator grant authorizes one synthetic test call, not a telephone identity.
No database or production caller policy is modified and no Gemini socket is opened.
"""
import argparse
import asyncio
import json
import math
import struct
import time
import uuid

from fastapi import HTTPException
from app.voice.receptionist_audio import AudioAdapter
from app.voice.m8_lifecycle import cleanup


class RestrictedVoice:
    transcript = ''

    def event(self, *args, **kwargs):
        pass

    def transition(self, state):
        pass

    def interrupt(self):
        self.transcript = ''

    def replace_transcript(self, text):
        self.transcript = text


class LiveSession:
    def __init__(self, adapter, settings):
        from app.voice.receptionist_audio import ReceptionistLive
        self.session = ReceptionistLive(settings, RestrictedVoice(), adapter.output, adapter.interrupt)
        self.frames = 0
        from app.voice.m8_diagnostics import Diagnostics
        self.diagnostics = Diagnostics()
        adapter.diagnostics = self.diagnostics
        self.session.diagnostics = self.diagnostics

    async def start(self):
        await self.session.start()

    async def audio(self, pcm):
        self.frames += 1
        await self.session.audio(pcm)

    async def receive(self):
        await self.session.receive()

    async def close(self):
        try:
            await self.session.close()
        finally:
            print(json.dumps({'event': 'live_diagnostics', **self.diagnostics.snapshot()}), flush=True)


class MockLive:
    def __init__(self, adapter):
        self.adapter = adapter
        self.frames = 0

    async def audio(self, pcm):
        self.frames += 1
        # Synthetic 440 Hz response; deliberately no transcription or tools.
        tone = b''.join(struct.pack('<h', int(3000 * math.sin(i * 2 * math.pi * 440 / 24000)))
                        for i in range(480))
        self.adapter.output(tone)

    async def close(self):
        pass


class CallServer:
    def __init__(self, enabled=False, grant=None, grant_seconds=60, call_seconds=30,
                 live_factory=None, reusable=False):
        self.enabled = enabled
        self.grant = grant
        self.expires = time.monotonic() + grant_seconds
        self.call_seconds = call_seconds
        self.active = False
        self.live_factory = live_factory
        self.reusable = reusable
        self.cleanup_deadline = None
        self.termination = asyncio.Event()

    async def handle(self, reader, writer):
        adapter = AudioAdapter(writer)
        task = None
        receiver = None
        live = None
        claimed = False
        reason = 'rejected'
        try:
            if not self.enabled or self.active or time.monotonic() > self.expires:
                return
            header = await asyncio.wait_for(reader.readexactly(3), 3)
            if header != b'\x01\x00\x10':
                return
            call_id = str(uuid.UUID(bytes=await asyncio.wait_for(reader.readexactly(16), 3)))
            if not self.grant or call_id != self.grant:
                return
            # Atomic admission: no await between checking and consuming the grant.
            if self.active or call_id != self.grant:
                return
            if not self.reusable:
                self.grant = None
            self.active = claimed = True
            live = self.live_factory(adapter) if self.live_factory else MockLive(adapter)
            task = asyncio.create_task(adapter.playback())
            async with asyncio.timeout(self.call_seconds):
                if self.live_factory:
                    await live.start()
                    receiver = asyncio.create_task(live.receive())
                print(json.dumps({'event': 'authorized_call', 'engine': 'gemini' if self.live_factory else 'mock'}), flush=True)
                while True:
                    header = await asyncio.wait_for(reader.readexactly(3), 5)
                    kind, length = header[0], int.from_bytes(header[1:], 'big')
                    if kind == 0 and length == 0:
                        break
                    if kind != 0x10 or length > 3200 or length % 2 or not length:
                        raise ValueError('Invalid audio frame')
                    pcm = await asyncio.wait_for(reader.readexactly(length), 3)
                    if task.done():
                        task.result()
                    if receiver and receiver.done():
                        receiver.result()
                        raise RuntimeError('Provider disconnected')
                    await adapter.input(live, pcm)
            reason = 'hangup'
        except asyncio.IncompleteReadError:
            reason = 'eof'
        except (TimeoutError, ValueError, OSError, RuntimeError, HTTPException) as exc:
            reason = type(exc).__name__
        finally:
            if claimed:
                self.cleanup_deadline = asyncio.get_running_loop().time() + 5
                self.termination.set()
            try:
                cleanup_result = await cleanup(live, adapter, (task, receiver), deadline=self.cleanup_deadline)
            finally:
                if claimed:
                    self.active = False
            print(json.dumps({'event': 'closed', 'reason': reason,
                              'cleanup': cleanup_result,
                              'input_frames': live.frames if live else 0,
                              'output_started': adapter.first_output is not None}), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorize-mock-test', action='store_true')
    parser.add_argument('--authorize-live-test', action='store_true')
    parser.add_argument('--uuid', type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    if args.authorize_mock_test and args.authorize_live_test:
        parser.error('Choose exactly one test engine')
    factory = None
    if args.authorize_live_test:
        from app.core.config import get_settings
        from app.voice.m8_credentials import test_live_settings
        settings = test_live_settings(get_settings())
        factory = lambda adapter: LiveSession(adapter, settings)
    calls = CallServer(args.authorize_mock_test or args.authorize_live_test, args.uuid, live_factory=factory)
    server = await asyncio.start_server(calls.handle, '127.0.0.1', 19000)
    print('READY 127.0.0.1:19000 ' + ('LIVE_TEST' if factory else 'MOCK_ONLY'), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
