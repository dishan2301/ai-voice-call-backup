"""Local AudioSocket/RTP bridge primitives for the isolated M8.5 prototype."""
import asyncio
import audioop
import struct
from dataclasses import dataclass

AUDIO_SOCKET_AUDIO = 0x10
AUDIO_SOCKET_UUID = 0x01
AUDIO_SOCKET_HANGUP = 0x00

def audiosocket_frame(kind: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("AudioSocket payload is too large")
    return bytes((kind,)) + len(payload).to_bytes(2, "big") + payload

class AudioSocketParser:
    """Incremental TCP AudioSocket framing parser (not UDP/RTP)."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 3:
            kind, length = self.buffer[0], int.from_bytes(self.buffer[1:3], "big")
            if len(self.buffer) < 3 + length:
                break
            frames.append((kind, bytes(self.buffer[3:3 + length])))
            del self.buffer[:3 + length]
        return frames

async def audiosocket_read(reader, on_audio, on_uuid=None):
    parser = AudioSocketParser()
    while True:
        data = await reader.read(4096)
        if not data:
            return
        for kind, payload in parser.feed(data):
            if kind == AUDIO_SOCKET_AUDIO:
                await on_audio(payload)
            elif kind == AUDIO_SOCKET_UUID and on_uuid:
                on_uuid(payload)
            elif kind == AUDIO_SOCKET_HANGUP:
                return

def rtp_payload(packet: bytes) -> bytes:
    if len(packet) < 12 or (packet[0] >> 6) != 2:
        return b""
    header = 12 + (packet[0] & 0x0F) * 4
    if packet[0] & 0x10:
        if len(packet) <= header:
            return b""
        header += 4 + packet[header + 1]
    return packet[header:]

def pcmu_to_gemini(payload: bytes, state=None):
    return audioop.ratecv(audioop.ulaw2lin(payload, 2), 2, 1, 8000, 16000, state)

def gemini_to_pcmu(pcm16: bytes, state=None):
    pcm8, state = audioop.ratecv(pcm16, 2, 1, 24000, 8000, state)
    return audioop.lin2ulaw(pcm8, 2), state

@dataclass
class SipBridge:
    live: object
    transport: asyncio.DatagramTransport | None = None
    peer: tuple[str, int] | None = None
    inbound_state: object = None
    outbound_state: object = None
    closed: bool = False

    async def inbound(self, packet: bytes):
        if self.closed:
            return
        pcm, self.inbound_state = pcmu_to_gemini(rtp_payload(packet), self.inbound_state)
        if pcm:
            await self.live.audio(pcm)

    def output(self, pcm24: bytes):
        if self.closed or not self.transport or not self.peer:
            return
        payload, self.outbound_state = gemini_to_pcmu(pcm24, self.outbound_state)
        if payload:
            self.transport.sendto(struct.pack("!BBHII", 0x80, 0, 0, 0, 0) + payload, self.peer)

    async def close(self):
        self.closed = True
        if self.transport:
            self.transport.close()
        await self.live.close()
