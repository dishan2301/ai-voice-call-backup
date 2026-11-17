import asyncio
from unittest.mock import AsyncMock, Mock, patch
import pytest
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response
from app.voice.m8_lifecycle import SingleAttemptConnect, cleanup


@pytest.mark.parametrize('status', [301,302,303,307,308])
def test_redirect_single_attempt(status):
    async def run():
        from websockets.datastructures import Headers
        error = InvalidStatus(Response(status,'Redirect',Headers({'Location':'wss://elsewhere.invalid'})))
        connection = Mock()
        connection.handshake = AsyncMock(side_effect=error)
        with patch.object(SingleAttemptConnect,'create_connection',AsyncMock(return_value=connection)) as create:
            with pytest.raises(InvalidStatus):
                await SingleAttemptConnect('wss://example.invalid')
            assert create.await_count == 1
            connection.transport.abort.assert_called_once()
    asyncio.run(run())


def test_no_retry_iterator():
    with pytest.raises(RuntimeError):
        SingleAttemptConnect('wss://example.invalid').__aiter__()


@pytest.mark.parametrize('hang_live,hang_audio',[(False,False),(True,False),(False,True),(True,True)])
def test_shared_cleanup_deadline(hang_live,hang_audio):
    async def run():
        async def close(hang):
            if hang: await asyncio.Event().wait()
        live=Mock(close=AsyncMock(side_effect=lambda: None))
        live.close=AsyncMock(side_effect=lambda: None)
        async def live_close(): await close(hang_live)
        async def audio_close(): await close(hang_audio)
        live.close=live_close
        adapter=Mock(close=audio_close)
        job=asyncio.create_task(asyncio.Event().wait())
        started=asyncio.get_running_loop().time()
        result=await cleanup(live,adapter,[job],seconds=.03)
        assert asyncio.get_running_loop().time()-started < .15
        assert result['deadline_exceeded'] == (hang_live or hang_audio)
        await asyncio.sleep(0)
        assert job.done()
        if hang_live or hang_audio: adapter.writer.transport.abort.assert_called_once()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())
