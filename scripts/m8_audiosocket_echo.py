#!/usr/bin/env python3
"""Loopback AudioSocket verifier; deterministic PCM echo, never production."""
import asyncio
import json
import sys
from app.voice.sip_bridge import (AUDIO_SOCKET_AUDIO, AUDIO_SOCKET_HANGUP,
                                  AUDIO_SOCKET_UUID, AudioSocketParser,
                                  audiosocket_frame)

async def handle(reader, writer):
    parser = AudioSocketParser()
    frames = audio = sent = 0
    peer = writer.get_extra_info("peername")
    print(json.dumps({"event": "connected", "peer": peer}), flush=True)
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            for kind, payload in parser.feed(data):
                frames += 1
                if kind == AUDIO_SOCKET_UUID:
                    if len(payload) != 16:
                        raise ValueError("AudioSocket UUID must be 16 bytes")
                    print(json.dumps({"event": "uuid", "bytes": len(payload)}), flush=True)
                elif kind == AUDIO_SOCKET_AUDIO:
                    audio += 1
                    writer.write(audiosocket_frame(AUDIO_SOCKET_AUDIO, payload))
                    await writer.drain()
                    sent += 1
                elif kind == AUDIO_SOCKET_HANGUP:
                    break
    except Exception as exc:
        print(json.dumps({"event": "error", "type": type(exc).__name__}), flush=True)
    finally:
        writer.close()
        await writer.wait_closed()
        print(json.dumps({"event": "closed", "frames": frames,
                          "audio_frames": audio, "echo_frames": sent}), flush=True)

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 19000)
    print(json.dumps({"event": "ready", "address": server.sockets[0].getsockname()}), flush=True)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
