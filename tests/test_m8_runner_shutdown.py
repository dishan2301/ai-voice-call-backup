import asyncio
import subprocess
import sys
from unittest.mock import Mock
from app.voice.m8_lifecycle import shutdown_runner


def test_owned_processes_reaped_unrelated_survives():
    async def run():
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])
        owned=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])
        listener=Mock()
        async def closed():pass
        listener.wait_closed=closed
        try:
            result=await shutdown_runner(listener,[owned],deadline=asyncio.get_running_loop().time()+1.2)
            assert result['ok'] and owned.poll() is not None
            assert unrelated.poll() is None
        finally:
            for p in (owned,unrelated):
                if p.poll() is None:p.kill()
                p.wait()
    asyncio.run(run())


def test_resistant_task_listener_and_process_report_failure():
    async def run():
        release=asyncio.Event()
        async def resistant():
            while not release.is_set():
                try:await asyncio.sleep(.001)
                except asyncio.CancelledError:pass
        listener=Mock(wait_closed=resistant)
        process=Mock()
        process.poll.return_value=None
        task=asyncio.create_task(resistant())
        await asyncio.sleep(0)
        start=asyncio.get_running_loop().time()
        result=await shutdown_runner(listener,[process],[task],deadline=start+.03)
        assert not result['ok'] and result['unconfirmed_processes']==1
        assert result['unconfirmed_tasks']>0
        assert asyncio.get_running_loop().time()-start<.15
        process.kill.assert_called_once()
        release.set()
        await asyncio.sleep(.01)
    asyncio.run(run())
