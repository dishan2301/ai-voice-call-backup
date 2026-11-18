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


def test_playback_sends_a_short_final_frame_instead_of_stranding_it():
    async def run():
        writer = Mock()
        adapter = AudioAdapter(writer)

        async def drain():
            adapter.closed = True

        writer.drain = drain
        adapter.output(b'\x00\x10' * 120)
        assert 0 < len(adapter.buffer) < 320
        await asyncio.wait_for(adapter.playback(), 1)
        packet = writer.write.call_args.args[0]
        assert packet[:3] == b'\x10\x01\x40'
        assert len(packet) == 323 and any(packet[3:])
        assert not adapter.buffer

    asyncio.run(run())
