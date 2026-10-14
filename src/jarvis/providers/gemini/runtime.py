"""Bounded async operation ownership using the core's recorded stop intent."""

import asyncio

from jarvis.audio.control import POLL_SECONDS, StreamControl
from jarvis.providers.gemini.errors import ProviderTimeout


async def guarded(operation, control: StreamControl, seconds: float):
    """Keep signals responsive during awaits; cancel and await owned cleanup."""
    task = asyncio.create_task(operation)
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while not task.done():
            control.check()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderTimeout("Gemini operation exceeded its deadline.")
            await asyncio.wait({task}, timeout=min(POLL_SECONDS, remaining))
        control.check()
        return task.result()
    finally:
        if not task.done():
            control.request_stop()
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=15
            )
        except TimeoutError:
            raise ProviderTimeout(
                "Gemini cleanup exceeded its fifteen-second deadline."
            ) from None
