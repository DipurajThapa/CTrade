"""Async token bucket.

A fourteen-day unattended run is exactly the situation where an over-eager
client gets an app id throttled or blocked, so every outbound message passes
through this. The bucket refills continuously rather than on a fixed window,
which avoids the burst-at-the-boundary behaviour of naive window counters.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None
                               else max(1, rate_per_minute // 6))
        self._tokens = self._capacity
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        return self._tokens

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity,
                           self._tokens + elapsed * self._rate_per_second)

    async def acquire(self, cost: float = 1.0) -> None:
        """Block until ``cost`` tokens are available, then consume them."""
        if cost > self._capacity:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity {self._capacity}")
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                wait = deficit / self._rate_per_second
            await asyncio.sleep(min(wait, 5.0))
