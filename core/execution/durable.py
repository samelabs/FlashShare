"""Awaiting durable work submitted to a thread.

A sync business segment submitted via asyncio.to_thread cannot be
stopped by cancelling the awaiting task. Once durable work (a claim, a
charge, a settlement transaction) has been submitted, its result
decides the durable state — dropping the await orphans the obligation
(charged-but-running, quota held, staged objects lost). These helpers
keep the submitted task alive across a cancellation of the caller and
converge on its result before the cancellation propagates.
"""

import asyncio


def spawn(fn, *args, **kwargs) -> asyncio.Future:
    """Submit one durable-work thread task; the caller holds the handle."""
    return asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))


async def await_durable(task):
    """Await a submitted durable-work task. Returns (result, cancelled).

    Normal path: the result (or the task's own exception) comes back
    promptly. On cancellation the backing thread keeps running and its
    result decides the durable settlement, so further cancels are
    absorbed until the work converges; the caller must then settle per
    the result and re-raise the cancellation."""
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError:
        pass
    while True:
        try:
            return await asyncio.shield(task), True
        except asyncio.CancelledError:
            continue


async def converge(task):
    """Absorb every cancellation until the task's result (or exception)
    is in — for use inside cancellation handling, where the durable
    obligation must complete regardless."""
    return (await await_durable(task))[0]
