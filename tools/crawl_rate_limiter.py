import asyncio
import random
import time
from typing import Awaitable, Callable


MIN_ITEMS_PER_MINUTE = 1
MAX_ITEMS_PER_MINUTE = 5
MAX_JITTER_SECONDS = 3.0


class ContentRateLimiter:
    """Evenly schedule content starts without allowing concurrent bursts."""

    def __init__(
        self,
        items_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter_picker: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not MIN_ITEMS_PER_MINUTE <= items_per_minute <= MAX_ITEMS_PER_MINUTE:
            raise ValueError(
                f"items_per_minute must be between {MIN_ITEMS_PER_MINUTE} "
                f"and {MAX_ITEMS_PER_MINUTE}"
            )

        self.items_per_minute = items_per_minute
        self.interval_seconds = 60.0 / items_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._jitter_picker = jitter_picker
        self._next_slot_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Reserve the next start time and return the actual wait in seconds."""
        async with self._lock:
            now = self._clock()
            slot_at = max(now, self._next_slot_at)
            wait_seconds = max(0.0, slot_at - now)
            jitter_limit = min(
                MAX_JITTER_SECONDS,
                self.interval_seconds * 0.25,
            )
            self._next_slot_at = (
                slot_at
                + self.interval_seconds
                + self._jitter_picker(0.0, jitter_limit)
            )

        if wait_seconds > 0:
            await self._sleeper(wait_seconds)
        return wait_seconds
