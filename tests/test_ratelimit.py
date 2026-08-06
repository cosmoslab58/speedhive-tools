"""Pacing tests for RateLimiter, on a fake clock so they cost no real time."""

from __future__ import annotations

import threading

import pytest

from speedhive.client import AsyncHTTPXRetryTransport, Client, HTTPXRetryTransport
from speedhive.ratelimit import RateLimiter


class FakeClock:
    """A monotonic clock that only advances when something sleeps on it."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(rate=10.0, **kwargs):
    clock = FakeClock()
    limiter = RateLimiter(rate, monotonic=clock.monotonic, sleep=clock.sleep, **kwargs)
    return limiter, clock


def test_burst_defaults_to_one_second_of_capacity():
    limiter, clock = make_limiter(rate=5.0)
    assert limiter.burst == 5.0
    for _ in range(5):
        assert limiter.acquire() == 0.0
    assert clock.sleeps == []


def test_paces_once_the_burst_is_spent():
    limiter, clock = make_limiter(rate=10.0, burst=1)
    assert limiter.acquire() == 0.0
    # Bucket is empty and the clock has not moved, so the next one waits a
    # full inter-request gap.
    assert limiter.acquire() == pytest.approx(0.1)
    assert limiter.acquire() == pytest.approx(0.1)
    assert clock.now == pytest.approx(0.2)


def test_sustained_rate_matches_the_configured_rate():
    limiter, clock = make_limiter(rate=8.0, burst=1)
    for _ in range(80):
        limiter.acquire()
    # 80 requests at 8/s, minus the one the initial token paid for.
    assert clock.now == pytest.approx(79 / 8.0)


def test_idle_time_refills_the_bucket_but_not_past_the_burst():
    limiter, clock = make_limiter(rate=10.0, burst=3)
    for _ in range(3):
        limiter.acquire()
    clock.now += 100.0  # a long idle stretch
    for _ in range(3):
        assert limiter.acquire() == 0.0
    assert limiter.acquire() > 0.0


def test_reservations_do_not_collide_across_threads():
    # Every caller must get its own slot in the queue. If concurrent callers
    # raced on the same token they would all compute the same short wait and
    # the real rate would exceed the configured one.
    # A frozen clock makes the expected reservations exact: nothing refills
    # while the threads are running, so the 50 waits must be 0/rate .. 49/rate.
    limiter = RateLimiter(10.0, burst=1, monotonic=lambda: 0.0, sleep=lambda _s: None)
    waits = []
    lock = threading.Lock()

    def worker():
        wait = limiter.acquire()
        with lock:
            waits.append(wait)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert limiter.granted == 50
    assert sorted(round(w, 6) for w in waits) == [round(n / 10.0, 6) for n in range(50)]


def test_concurrency_slot_is_bounded():
    limiter = RateLimiter(1000.0, max_concurrency=2)
    started = threading.Semaphore(0)
    release = threading.Event()
    inside = []
    lock = threading.Lock()

    def worker():
        with limiter.slot():
            with lock:
                inside.append(1)
            started.release()
            release.wait(timeout=5)
            with lock:
                inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    started.acquire(timeout=5)
    started.acquire(timeout=5)
    with lock:
        assert len(inside) == 2  # the other two are blocked on the semaphore
    release.set()
    for thread in threads:
        thread.join(timeout=5)


def test_no_limiter_means_no_slot_and_no_wait():
    limiter = RateLimiter(5.0)
    with limiter.slot():
        pass  # max_concurrency unset: the slot is a no-op, not a block


def test_stats_report_what_the_limiter_did():
    limiter, _clock = make_limiter(rate=4.0, burst=1)
    for _ in range(4):
        limiter.acquire()
    stats = limiter.stats()
    assert stats["granted"] == 4
    assert stats["waited_seconds"] == pytest.approx(0.75)
    assert limiter.waited_seconds == pytest.approx(0.75)


def test_rejects_nonsense_configuration():
    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(5.0, burst=0)
    with pytest.raises(ValueError):
        RateLimiter(5.0, max_concurrency=0)


def test_transport_defaults_to_no_limiter():
    transport = HTTPXRetryTransport()
    assert transport.rate_limiter is None
    assert AsyncHTTPXRetryTransport().rate_limiter is None


def test_client_passes_the_limiter_down_to_both_transports():
    limiter = RateLimiter(6.0, max_concurrency=4)
    client = Client(base_url="https://example.com", rate_limiter=limiter)
    sync_transport = client.get_httpx_client()._transport
    async_transport = client.get_async_httpx_client()._transport
    assert sync_transport.rate_limiter is limiter
    assert async_transport.rate_limiter is limiter
