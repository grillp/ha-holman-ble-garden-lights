"""Unit tests for the framework-agnostic DebouncedCommandWorker.

These tests don't touch Home Assistant or Bluetooth at all: they exercise
the coalescing/cooldown/retry-surfacing logic in isolation, with a fake
`send` coroutine and a fake `sleep` so cooldown timing is deterministic
and instant instead of real wall-clock delays.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.holman_garden_lights.worker import DebouncedCommandWorker


def _task_factory(coro):
    """Real asyncio task creation - fine for these plain-asyncio tests."""
    return asyncio.ensure_future(coro)


class _FakeClock:
    """Records sleep() calls instead of actually waiting."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Yield control once so other tasks can run, without a real delay.
        await asyncio.sleep(0)


async def test_single_request_sends_once():
    """A single request results in exactly one send with the right args."""
    calls: list[tuple[bool, int]] = []

    async def send(on: bool, pct: int) -> None:
        calls.append((on, pct))

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send, cooldown=1.2, create_task=_task_factory, sleep=clock.sleep
    )

    await worker.request(on=True, pct=80)

    assert calls == [(True, 80)]
    assert clock.sleeps == [1.2]


async def test_rapid_requests_coalesce_to_final_target():
    """Several requests queued before the worker runs collapse to one send."""
    calls: list[tuple[bool, int]] = []
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def send(on: bool, pct: int) -> None:
        calls.append((on, pct))
        send_started.set()
        await release_send.wait()

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send, cooldown=0.0, create_task=_task_factory, sleep=clock.sleep
    )

    # Fire the first request but don't await it yet - it starts the worker,
    # which immediately picks up (True, 10) and blocks inside `send`.
    first = asyncio.ensure_future(worker.request(on=True, pct=10))
    await send_started.wait()

    # Queue several more targets while the worker is still busy sending the
    # first one. Only the last of these should ever reach `send`.
    second = asyncio.ensure_future(worker.request(on=True, pct=50))
    third = asyncio.ensure_future(worker.request(on=False, pct=0))
    fourth = asyncio.ensure_future(worker.request(on=True, pct=90))
    await asyncio.sleep(0)  # let them all register as pending/waiters

    release_send.set()
    await asyncio.gather(first, second, third, fourth)

    # First send (the one already in flight) plus exactly one more for the
    # coalesced final target - not one send per request.
    assert calls == [(True, 10), (True, 90)]


async def test_on_success_callback_runs_after_successful_send():
    """on_success is invoked with the sent target once send succeeds."""
    seen: list[tuple[bool, int]] = []

    async def send(on: bool, pct: int) -> None:
        return None

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send,
        cooldown=0.0,
        create_task=_task_factory,
        sleep=clock.sleep,
        on_success=lambda on, pct: seen.append((on, pct)),
    )

    await worker.request(on=True, pct=42)

    assert seen == [(True, 42)]


async def test_permanent_failure_is_surfaced_to_caller():
    """If send() raises, the awaiting caller sees that exception."""

    class BoomError(Exception):
        pass

    async def send(on: bool, pct: int) -> None:
        raise BoomError("unreachable")

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send, cooldown=0.0, create_task=_task_factory, sleep=clock.sleep
    )

    with pytest.raises(BoomError):
        await worker.request(on=True, pct=50)


async def test_failure_is_surfaced_to_all_coalesced_waiters():
    """Superseded requests fail together with the final one, not silently."""

    class BoomError(Exception):
        pass

    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def send(on: bool, pct: int) -> None:
        send_started.set()
        await release_send.wait()
        raise BoomError("unreachable")

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send, cooldown=0.0, create_task=_task_factory, sleep=clock.sleep
    )

    first = asyncio.ensure_future(worker.request(on=True, pct=10))
    await send_started.wait()
    second = asyncio.ensure_future(worker.request(on=True, pct=20))
    await asyncio.sleep(0)

    release_send.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(r, BoomError) for r in results)


async def test_stop_cancels_pending_waiters():
    """stop() releases anyone still waiting instead of hanging forever."""
    release_send = asyncio.Event()
    send_started = asyncio.Event()

    async def send(on: bool, pct: int) -> None:
        send_started.set()
        await release_send.wait()

    clock = _FakeClock()
    worker = DebouncedCommandWorker(
        send=send, cooldown=0.0, create_task=_task_factory, sleep=clock.sleep
    )

    pending = asyncio.ensure_future(worker.request(on=True, pct=10))
    await send_started.wait()
    queued = asyncio.ensure_future(worker.request(on=True, pct=20))
    await asyncio.sleep(0)

    worker.stop()

    with pytest.raises(asyncio.CancelledError):
        await queued

    # Unblock the in-flight send so its task can finish tearing down cleanly.
    release_send.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
