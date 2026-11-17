import asyncio
from unittest.mock import AsyncMock, Mock
from scripts.m8_receptionist_server import CallServer
from app.voice.receptionist_audio import AudioAdapter

def test_off_busy_expired_and_wrong_grants_reject():
    async def run():
        for server in [CallServer(), CallServer(True, 'other'), CallServer(True, 'id', -1)]:
            reader = asyncio.StreamReader()
            reader.feed_data(b'\x01\x00\x10' + b'1' * 16)
            reader.feed_eof()
            writer = Mock()
            writer.wait_closed = AsyncMock()
            await server.handle(reader, writer)
            writer.close.assert_called_once()
            assert not server.active
        server = CallServer(True, 'id')
        server.active = True
        writer = Mock(wait_closed=AsyncMock())
        await server.handle(asyncio.StreamReader(), writer)
        assert server.active
    asyncio.run(run())

def test_buffer_limit_and_interruption():
    adapter = AudioAdapter(Mock())
    try:
        adapter.output(b'\x00\x00' * 250000)
        assert False, 'buffer must be bounded'
    except RuntimeError:
        pass
    adapter.output(b'\x01\x00' * 960)
    adapter.interrupt()
    assert not adapter.buffer
