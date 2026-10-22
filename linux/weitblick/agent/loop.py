"""Coroutines on the GLib main loop. Tool calls are `async def`s whose awaits wait for GLib
timeouts, callbacks and worker threads without blocking the loop (PyGObject 3.48 has no asyncio
integration, and the app is a GTK main-loop app anyway).

    async def tool():
        await sleep(0.02)                         # a GLib timeout
        answer = await from_callback(lambda done: page.wait_until_connected(90, done))
        png = await run_in_thread(encode, pixels) # slow work off the loop
        return answer

    spawn(tool()).add_done_callback(lambda task: print(task.result()))

Only Future objects may be awaited. Everything resumes on the main loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Coroutine

from gi.repository import GLib

log = logging.getLogger(__name__)


class Future:
    """A value that arrives later. Resolve it on the main loop (run_in_thread does that for you)."""

    def __init__(self) -> None:
        self._done = False
        self._value: Any = None
        self._error: BaseException | None = None
        self._callbacks: list[Callable[[Future], None]] = []

    @property
    def done(self) -> bool:
        return self._done

    def set_result(self, value: Any = None) -> None:
        """Resolves the future; later calls are ignored (a timeout and an answer may race)."""
        if self._done:
            return
        self._done, self._value = True, value
        self._fire()

    def set_exception(self, error: BaseException) -> None:
        if self._done:
            return
        self._done, self._error = True, error
        self._fire()

    def result(self) -> Any:
        if not self._done:
            raise RuntimeError("the future is not done yet")
        if self._error is not None:
            raise self._error
        return self._value

    def exception(self) -> BaseException | None:
        return self._error

    def add_done_callback(self, callback: Callable[[Future], None]) -> None:
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def _fire(self) -> None:
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)

    def __await__(self):
        if not self._done:
            yield self
        return self.result()


class Task(Future):
    """Runs a coroutine; resolves with its return value (or its exception)."""

    def __init__(self, coroutine: Coroutine) -> None:
        super().__init__()
        self._coroutine = coroutine
        self._step(None, None)

    def _step(self, value: Any, error: BaseException | None) -> None:
        try:
            awaited = self._coroutine.throw(error) if error is not None else self._coroutine.send(value)
        except StopIteration as stop:
            self.set_result(stop.value)
            return
        except BaseException as failure:  # noqa: BLE001 - handed to whoever awaits the task
            self.set_exception(failure)
            return
        if not isinstance(awaited, Future):
            self._step(None, TypeError(f"only Future objects can be awaited here, not {awaited!r}"))
            return
        awaited.add_done_callback(self._wake)

    def _wake(self, future: Future) -> None:
        # Resume from the main loop, never inside the code that resolved the future.
        def resume() -> bool:
            if future._error is not None:
                self._step(None, future._error)
            else:
                self._step(future._value, None)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(resume)


def spawn(coroutine: Coroutine) -> Task:
    return Task(coroutine)


def sleep(seconds: float) -> Future:
    future = Future()

    def fire() -> bool:
        future.set_result(None)
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(max(0, int(seconds * 1000)), fire)
    return future


def from_callback(start: Callable[[Callable[..., None]], None]) -> Future:
    """Wraps a callback API: `start(done)` begins the work and calls `done(value)` once later. With
    several callback arguments the future's value is their tuple."""
    future = Future()

    def done(*values: Any) -> None:
        future.set_result(values[0] if len(values) == 1 else (values or None))
    start(done)
    return future


def run_in_thread(function: Callable[..., Any], *args: Any) -> Future:
    """Runs `function(*args)` on a worker thread; the future resolves on the main loop."""
    future = Future()

    def work() -> None:
        try:
            value, error = function(*args), None
        except BaseException as failure:  # noqa: BLE001
            value, error = None, failure

        def deliver() -> bool:
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(deliver)

    threading.Thread(target=work, name="weitblick-agent-work", daemon=True).start()
    return future


class SerialQueue:
    """Runs coroutines one after another (tool calls on one session must not mix their input)."""

    def __init__(self) -> None:
        self._tail: Future | None = None

    async def run(self, make: Callable[[], Coroutine]) -> Any:
        previous = self._tail
        mine = Future()
        self._tail = mine
        try:
            if previous is not None:
                try:
                    await previous
                except BaseException:  # noqa: BLE001 - an earlier call's failure is its own
                    pass
            return await spawn(make())
        finally:
            mine.set_result(None)
