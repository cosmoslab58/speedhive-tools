"""The retry transports honour `Retry-After` instead of guessing.

Every sleep here is captured rather than performed, so the suite still costs
no real time even though it exercises the full retry loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from speedhive.client import (
    DEFAULT_MAX_RETRY_AFTER,
    AsyncHTTPXRetryTransport,
    HTTPXRetryTransport,
    parse_retry_after,
)

REQUEST = httpx.Request("GET", "https://api.example.com/v2/events/1/sessions")


def reply(status: int, retry_after: str | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(status, headers=headers, request=REQUEST)


def drive(monkeypatch, replies, **kwargs):
    """Run the sync transport over a canned reply sequence.

    Returns `(response, slept)` where `slept` is every delay the transport
    asked for, in order.
    """
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    pending = iter(replies)
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, request: next(pending)
    )
    response = HTTPXRetryTransport(**kwargs).handle_request(REQUEST)
    return response, slept


def drive_async(monkeypatch, replies, **kwargs):
    """Async counterpart of `drive`."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    pending = iter(replies)

    async def handle(self, request):
        return next(pending)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    transport = AsyncHTTPXRetryTransport(**kwargs)
    response = asyncio.run(transport.handle_async_request(REQUEST))
    return response, slept


# -- header parsing --------------------------------------------------------


def test_parses_delta_seconds():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("  30  ") == 30.0
    assert parse_retry_after("0") == 0.0


def test_parses_an_http_date():
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=45), usegmt=True)
    assert parse_retry_after(header, now=now) == pytest.approx(45.0)


def test_a_date_already_past_means_no_wait_rather_than_a_negative_one():
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(now - timedelta(minutes=5), usegmt=True)
    assert parse_retry_after(header, now=now) == 0.0


def test_a_negative_delta_is_clamped_not_slept_backwards():
    assert parse_retry_after("-30") == 0.0


def test_unusable_headers_answer_none_so_the_caller_falls_back():
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("nan") is None


def test_a_naive_http_date_is_read_as_utc():
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    # `-0000` is the obsolete "no timezone stated" form and parses naive.
    assert parse_retry_after("Thu, 06 Aug 2026 12:01:00 -0000", now=now) == pytest.approx(60.0)


# -- sync transport --------------------------------------------------------


def test_429_waits_exactly_as_long_as_the_server_asked(monkeypatch):
    response, slept = drive(monkeypatch, [reply(429, "7"), reply(200)])
    assert response.status_code == 200
    assert slept == [7.0]


def test_429_honours_the_http_date_form(monkeypatch):
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    response, slept = drive(monkeypatch, [reply(429, header), reply(200)])
    assert response.status_code == 200
    assert slept[0] == pytest.approx(90.0, abs=2.0)


def test_without_the_header_the_existing_backoff_still_applies(monkeypatch):
    response, slept = drive(
        monkeypatch, [reply(429), reply(503), reply(429), reply(200)], backoff_factor=1.0
    )
    assert response.status_code == 200
    assert slept == [1.0, 2.0, 4.0]


def test_an_unparseable_header_falls_back_to_backoff(monkeypatch):
    _response, slept = drive(monkeypatch, [reply(429, "later"), reply(200)], backoff_factor=1.5)
    assert slept == [1.5]


def test_the_server_schedule_is_followed_on_every_retry(monkeypatch):
    response, slept = drive(
        monkeypatch,
        [reply(429, "2"), reply(429, "5"), reply(429, "11"), reply(200)],
        backoff_factor=1.0,
    )
    assert response.status_code == 200
    # Not 1, 2, 4: the server said 2, 5, 11 and it gets what it asked for.
    assert slept == [2.0, 5.0, 11.0]


def test_retry_after_applies_to_other_retryable_statuses_too(monkeypatch):
    _response, slept = drive(monkeypatch, [reply(503, "4"), reply(200)])
    assert slept == [4.0]


def test_an_absurd_retry_after_is_capped(monkeypatch):
    _response, slept = drive(monkeypatch, [reply(429, "86400"), reply(200)])
    assert slept == [DEFAULT_MAX_RETRY_AFTER]


def test_the_cap_is_configurable(monkeypatch):
    _response, slept = drive(monkeypatch, [reply(429, "600"), reply(200)], max_retry_after=30.0)
    assert slept == [30.0]


def test_a_transport_error_has_no_header_and_uses_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    pending = iter([httpx.ConnectError("boom", request=REQUEST), reply(200)])

    def handle(self, request):
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    response = HTTPXRetryTransport(backoff_factor=0.5).handle_request(REQUEST)
    assert response.status_code == 200
    assert slept == [0.5]


def test_the_retry_budget_still_runs_out(monkeypatch):
    response, slept = drive(monkeypatch, [reply(429, "3")] * 4, max_retries=3)
    # The last 429 is handed back rather than retried a fourth time.
    assert response.status_code == 429
    assert slept == [3.0, 3.0, 3.0]


# -- async transport -------------------------------------------------------


def test_async_429_honours_retry_after(monkeypatch):
    response, slept = drive_async(monkeypatch, [reply(429, "9"), reply(200)])
    assert response.status_code == 200
    assert slept == [9.0]


def test_async_falls_back_to_backoff_without_the_header(monkeypatch):
    response, slept = drive_async(
        monkeypatch, [reply(429), reply(429), reply(200)], backoff_factor=1.0
    )
    assert response.status_code == 200
    assert slept == [1.0, 2.0]


def test_async_honours_the_http_date_form(monkeypatch):
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    _response, slept = drive_async(monkeypatch, [reply(429, header), reply(200)])
    assert slept[0] == pytest.approx(20.0, abs=2.0)
