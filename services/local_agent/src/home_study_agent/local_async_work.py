"""Keep blocking local verification off the voice server's event loop."""
import asyncio


async def _drain(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.exception()


async def finish_owned(awaitable):
    """Propagate cancellation once, then wait for the owned coroutine to settle."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        await _drain(task)
        raise


async def run_local(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. Drain before the caller records failure
        # or advances to dispatch, so no unowned write races the recovery journal.
        await _drain(task)
        raise

