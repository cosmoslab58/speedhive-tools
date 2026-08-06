"""Proactive client-side rate limiting.

`HTTPXRetryTransport` reacts to 429/502/503/504 after the fact. That is enough
for the per-organization sync, which issues a few thousand requests. It is not
enough for a bulk crawl of the global event feed, where a backfill of one or two
million requests fired as fast as the network allows is impolite at best and
gets the client blocked at worst.

`RateLimiter` supplies the missing half: a token bucket that paces requests to a
chosen rate, plus an optional ceiling on how many may be in flight at once. It
is entirely opt-in, and a client constructed without one behaves exactly as it
did before:

    from speedhive.client import Client
    from speedhive.ratelimit import RateLimiter

    client = Client(
        base_url="https://api2.mylaps.com",
        rate_limiter=RateLimiter(requests_per_second=6.0, max_concurrency=4),
    )

One limiter instance is shared by every request made through the client it is
attached to, including the retries the transport issues internally, and it is
safe to share across threads.

The clock and the sleep function are injectable so the pacing can be tested
without spending real seconds.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager


class RateLimiter:
    """A token bucket, plus an optional in-flight ceiling.

    `requests_per_second` is the sustained rate. `burst` is how many requests
    may go out back to back after an idle period, defaulting to one second's
    worth. `max_concurrency`, when set, caps how many requests may be in flight
    at once; that matters independently of the rate because a slow endpoint
    otherwise lets an unbounded number of workers pile up on the server.
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        burst: float | None = None,
        max_concurrency: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if burst is None:
            burst = max(1.0, float(requests_per_second))
        if burst < 1:
            raise ValueError("burst must be at least 1")
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")

        self.requests_per_second = float(requests_per_second)
        self.burst = float(burst)
        self.max_concurrency = max_concurrency

        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = self.burst
        self._updated = monotonic()
        self._granted = 0
        self._waited = 0.0

        self._semaphore = (
            threading.BoundedSemaphore(max_concurrency) if max_concurrency else None
        )
        self._async_semaphore: asyncio.Semaphore | None = None

    # -- pacing ------------------------------------------------------------

    def _reserve(self) -> float:
        """Claim one token and return how long the caller must wait for it.

        The bucket is allowed to go negative, which is what makes this correct
        under concurrency: each caller reserves its own slot in the queue and
        then waits out the deficit, rather than several of them racing on the
        same token.
        """
        with self._lock:
            now = self._monotonic()
            elapsed = now - self._updated
            if elapsed > 0:
                self._tokens = min(
                    self.burst, self._tokens + elapsed * self.requests_per_second
                )
                self._updated = now

            wait = 0.0
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.requests_per_second

            self._tokens -= 1.0
            self._granted += 1
            self._waited += wait
            return wait

    def acquire(self) -> float:
        """Block until one request may go out. Returns the seconds slept."""
        wait = self._reserve()
        if wait > 0:
            self._sleep(wait)
        return wait

    async def acquire_async(self) -> float:
        """Async counterpart of `acquire`. Returns the seconds slept."""
        wait = self._reserve()
        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    # -- concurrency -------------------------------------------------------

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one of the in-flight slots for the duration of the block."""
        if self._semaphore is None:
            yield
            return
        self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()

    @asynccontextmanager
    async def slot_async(self):
        """Async counterpart of `slot`."""
        if self.max_concurrency is None:
            yield
            return
        semaphore = self._get_async_semaphore()
        async with semaphore:
            yield

    def _get_async_semaphore(self) -> asyncio.Semaphore:
        # Built lazily: an asyncio.Semaphore binds to the running loop, and the
        # limiter is usually constructed before there is one.
        with self._lock:
            if self._async_semaphore is None:
                self._async_semaphore = asyncio.Semaphore(self.max_concurrency)
            return self._async_semaphore

    # -- observability -----------------------------------------------------

    @property
    def granted(self) -> int:
        """Requests the limiter has released, retries included."""
        return self._granted

    @property
    def waited_seconds(self) -> float:
        """Total time spent waiting on the bucket, summed over all callers."""
        return self._waited

    def stats(self) -> dict[str, float]:
        return {
            "requests_per_second": self.requests_per_second,
            "burst": self.burst,
            "max_concurrency": self.max_concurrency or 0,
            "granted": self._granted,
            "waited_seconds": round(self._waited, 3),
        }

    def __repr__(self) -> str:
        return (
            f"RateLimiter(requests_per_second={self.requests_per_second}, "
            f"burst={self.burst}, max_concurrency={self.max_concurrency})"
        )
