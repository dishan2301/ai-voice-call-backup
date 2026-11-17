import asyncio
from scripts.m8_audiosocket_echo import handle
from app.voice.sip_bridge import AUDIO_SOCKET_AUDIO, AUDIO_SOCKET_UUID, audiosocket_frame

def test_echo_server_accepts_uuid_and_audio():
    async def run():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(audiosocket_frame(AUDIO_SOCKET_UUID, b"1" * 16))
        writer.write(audiosocket_frame(AUDIO_SOCKET_AUDIO, b"\x01\x02" * 160))
        await writer.drain()
        header = await reader.readexactly(3)
        payload = await reader.readexactly(int.from_bytes(header[1:], "big"))
        assert header[0] == AUDIO_SOCKET_AUDIO and payload == b"\x01\x02" * 160
        writer.close(); await writer.wait_closed(); server.close(); await server.wait_closed()
    asyncio.run(run())
