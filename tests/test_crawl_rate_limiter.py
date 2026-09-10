import asyncio

import pytest

from base.base_crawler import AbstractCrawler
from tools.crawl_rate_limiter import ContentRateLimiter


class SlotOrderProbe:
    content_request_slot = AbstractCrawler.content_request_slot

    def __init__(self, events):
        self.events = events

    async def wait_for_content_slot(self, _content_id: str = "") -> None:
        self.events.append("rate_limit")


@pytest.mark.asyncio
async def test_concurrent_callers_receive_evenly_spaced_slots():
    slept_for = []

    async def fake_sleep(delay: float) -> None:
        slept_for.append(delay)

    limiter = ContentRateLimiter(
        5,
        clock=lambda: 100.0,
        sleeper=fake_sleep,
        jitter_picker=lambda _minimum, _maximum: 0.0,
    )

    waits = await asyncio.gather(*(limiter.acquire() for _ in range(5)))

    assert waits == [0.0, 12.0, 24.0, 36.0, 48.0]
    assert slept_for == [12.0, 24.0, 36.0, 48.0]


@pytest.mark.asyncio
async def test_positive_jitter_never_makes_the_rate_faster():
    async def fake_sleep(_delay: float) -> None:
        return None

    limiter = ContentRateLimiter(
        5,
        clock=lambda: 0.0,
        sleeper=fake_sleep,
        jitter_picker=lambda _minimum, maximum: maximum,
    )

    waits = [await limiter.acquire() for _ in range(3)]

    assert waits == [0.0, 15.0, 30.0]


@pytest.mark.asyncio
async def test_expired_slot_starts_immediately_and_reschedules_from_now():
    current_time = 0.0

    async def fake_sleep(delay: float) -> None:
        nonlocal current_time
        current_time += delay

    limiter = ContentRateLimiter(
        5,
        clock=lambda: current_time,
        sleeper=fake_sleep,
        jitter_picker=lambda _minimum, _maximum: 0.0,
    )

    first_wait = await limiter.acquire()
    current_time = 30.0
    second_wait = await limiter.acquire()
    third_wait = await limiter.acquire()

    assert first_wait == 0.0
    assert second_wait == 0.0
    assert third_wait == 12.0
    assert current_time == 42.0


@pytest.mark.parametrize("items_per_minute", [0, 6])
def test_rejects_rates_outside_supported_range(items_per_minute):
    with pytest.raises(ValueError):
        ContentRateLimiter(items_per_minute)


@pytest.mark.asyncio
async def test_concurrency_capacity_is_acquired_before_rate_limit():
    events = []
    probe = SlotOrderProbe(events)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    task_started = asyncio.Event()

    async def run_request() -> None:
        task_started.set()
        async with probe.content_request_slot(semaphore, "content-1"):
            events.append("request")

    request_task = asyncio.create_task(run_request())
    await task_started.wait()
    await asyncio.sleep(0)

    assert events == []

    semaphore.release()
    await request_task

    assert events == ["rate_limit", "request"]
    assert not semaphore.locked()
