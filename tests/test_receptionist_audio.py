import asyncio
from unittest.mock import AsyncMock, Mock
from app.voice.receptionist_audio import AudioAdapter, ReceptionistLive


def test_audio_conversion_interrupt_and_cleanup():
    async def run():
        writer = AsyncMock()
        writer.close = Mock()
        adapter = AudioAdapter(writer)
        live = AsyncMock()
        await adapter.input(live, b'\x01\x00' * 160)
        assert len(live.audio.call_args.args[0]) > 320
        adapter.output(b'\x01\x00' * 960)
        assert adapter.buffer
        adapter.interrupt()
        assert not adapter.buffer
        await adapter.close()
        assert adapter.closed
    asyncio.run(run())


def test_receptionist_never_loads_mailbox_vocabulary():
    assert ReceptionistLive._vocabulary() == []
