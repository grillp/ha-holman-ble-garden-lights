"""Framework-agnostic debounce/coalescing worker for BLE commands.

Extracted from ``HolmanLight`` so the debounce-and-retry scheduling logic
(which caller wins when several requests race, when the cooldown runs, how
waiters are resolved) can be unit-tested without a real Home Assistant
entity, a real event loop task API, or real ``asyncio.sleep`` timing.

This module knows nothing about Home Assistant or Bluetooth: it only knows
how to coalesce ``(on, pct)`` targets and run a caller-supplied ``send``
coroutine for the latest one, with a cooldown between sends.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

_LOGGER = logging.getLogger(__name__)

SendFn = Callable[[bool, int], Awaitable[None]]
TaskFactory = Callable[[Coroutine[Any, Any, None]], "asyncio.Task[None]"]
SleepFn = Callable[[float], Awaitable[None]]
StateCallback = Callable[[bool, int], None]


class DebouncedCommandWorker:
    """Coalesces rapid on/off/brightness requests into a single BLE send.

    Callers call :meth:`request` and await its result. Only the *latest*
    queued target survives if several requests arrive before the worker
    gets to send; superseded requests resolve together with the final one
    (or fail together, if the final one fails). A cooldown runs after every
    send before the worker checks for newly-queued work.
    """

    def __init__(
        self,
        *,
        send: SendFn,
        cooldown: float,
        create_task: TaskFactory,
        sleep: SleepFn = asyncio.sleep,
        on_success: StateCallback | None = None,
    ) -> None:
        """Set up the worker.

        send: coroutine called with (on, pct) to perform one BLE send.
        cooldown: seconds to wait after a send before checking for more work.
        create_task: creates the worker's background task. Callers that are
            Home Assistant entities should supply something backed by
            ``ConfigEntry.async_create_background_task`` rather than
            ``hass.async_create_task``, so a stuck ``send`` can never block
            HA startup (see light.py for why this matters).
        sleep: injectable in place of ``asyncio.sleep`` so cooldown timing
            can be tested deterministically.
        on_success: optional callback invoked with (on, pct) after a
            successful send, before waiters are resolved.
        """
        self._send = send
        self._cooldown = cooldown
        self._create_task = create_task
        self._sleep = sleep
        self._on_success = on_success

        self._pending: tuple[bool, int] | None = None
        self._waiters: list[asyncio.Future[None]] = []
        # Waiters for the batch currently being sent. Tracked separately
        # from self._waiters (which only holds *newly queued* requests) so
        # that if the worker task is cancelled mid-send, stop()/finally can
        # still resolve them instead of leaving those callers awaiting a
        # future that will now never be touched.
        self._in_flight_waiters: list[asyncio.Future[None]] = []
        self._worker: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether a worker task is currently active."""
        return self._worker is not None and not self._worker.done()

    async def request(self, *, on: bool, pct: int) -> None:
        """Queue a target state and wait for the worker to resolve it."""
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending = (on, pct)
        self._waiters.append(fut)

        if not self.is_running:
            self._worker = self._create_task(self._async_run())

        await fut

    def stop(self) -> None:
        """Cancel the worker task and release anyone still waiting on it."""
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        for fut in (*self._in_flight_waiters, *self._waiters):
            if not fut.done():
                fut.cancel()
        self._in_flight_waiters = []
        self._waiters.clear()
        self._pending = None

    async def _async_run(self) -> None:
        """Send the latest pending target, then cool down; repeat if more queued."""
        try:
            while self._pending is not None:
                # Consume target atomically so new requests can queue behind it.
                on, pct = self._pending
                self._pending = None
                self._in_flight_waiters, self._waiters = self._waiters, []

                _LOGGER.debug("Deploying state update: on=%s level=%s%%", on, pct)
                error: Exception | None = None
                try:
                    await self._send(on, pct)
                except Exception as err:  # noqa: BLE001 - surfaced to waiters below
                    error = err

                if error is None and self._on_success is not None:
                    self._on_success(on, pct)

                for fut in self._in_flight_waiters:
                    if fut.done():
                        continue
                    if error is None:
                        fut.set_result(None)
                    else:
                        fut.set_exception(error)
                self._in_flight_waiters = []

                # Physical cooldown before the controller sees another connection.
                await self._sleep(self._cooldown)
        finally:
            # If cancelled mid-flight (including mid-send, before the loop
            # above gets to resolve self._in_flight_waiters), don't leave
            # any caller hanging.
            for fut in (*self._in_flight_waiters, *self._waiters):
                if not fut.done():
                    fut.cancel()
            self._in_flight_waiters = []
            self._waiters.clear()
            self._pending = None
