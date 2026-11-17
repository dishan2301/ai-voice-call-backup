"""M8 single-attempt transport and bounded owned-resource cleanup."""
import asyncio
from websockets.asyncio.client import connect


class SingleAttemptConnect(connect):
    def process_redirect(self, exc):
        return exc

    def __aiter__(self):
        raise RuntimeError('M8 reconnection is disabled')


async def cleanup(live, adapter, jobs=(), seconds=5, deadline=None):
    """One loop-clock deadline; abort transports if graceful closure stalls."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds if deadline is None else deadline
    owned = [job for job in jobs if job is not None]
    for job in owned:
        job.cancel()
    if live:
        owned.append(asyncio.create_task(live.close()))
    owned.append(asyncio.create_task(adapter.close()))
    pending = set()
    try:
        _, pending = await asyncio.wait(owned, timeout=max(0, deadline-loop.time()))
    finally:
        for job in owned:
            if not job.done():
                job.cancel()
                pending.add(job)
            elif not job.cancelled():
                job.exception()  # retrieve errors without exposing provider text
        if pending:
            adapter.closed = True
            adapter.interrupt()
            adapter.writer.transport.abort()
            session = getattr(live, 'session', None)
            socket = getattr(session, 'socket', None)
            transport = getattr(socket, 'transport', None)
            if transport:
                transport.abort()
    return {'deadline_exceeded': bool(pending), 'unconfirmed_tasks': len(pending)}


async def shutdown_runner(listener, processes, jobs=(), deadline=None):
    """Bound outer cleanup using the call's existing monotonic deadline.

    Processes must be Popen objects returned by this runner, never discovered PIDs.
    Returns failure rather than waiting indefinitely for resistant resources.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5 if deadline is None else deadline
    listener.close()
    owned = list(jobs)
    for task in owned:
        task.cancel()
    waiter = asyncio.create_task(listener.wait_closed())
    owned.append(waiter)
    for process in processes:
        if process.poll() is None:
            process.terminate()
    # Reserve the final second for kill and reaping; never reset the deadline.
    grace_end = max(loop.time(), deadline - 1)
    while loop.time() < grace_end:
        if all(p.poll() is not None for p in processes) and all(t.done() for t in owned):
            break
        await asyncio.sleep(min(.01, max(0, grace_end-loop.time())))
    for process in processes:
        if process.poll() is None:
            process.kill()
    while loop.time() < deadline and any(p.poll() is None for p in processes):
        await asyncio.sleep(min(.01, max(0, deadline-loop.time())))
    for task in owned:
        if not task.done():
            task.cancel()
        elif not task.cancelled():
            task.exception()
    pending = sum(not task.done() for task in owned)
    alive = sum(p.poll() is None for p in processes)
    return {'ok': not pending and not alive and loop.time() <= deadline,
            'unconfirmed_tasks': pending, 'unconfirmed_processes': alive}
