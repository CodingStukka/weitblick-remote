"""Fetches remote clipboard data one request at a time (answers carry no id, so they pair up in
order) and caches it until the remote clipboard changes.

Thread safe. `fetch` blocks for at most its timeout, so call it from a worker thread: the answers
arrive through `deliver`, which the main loop calls (RemoteClipboardDelegate).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

_TOO_LARGE = "too large"  # asking again would not help
_REFUSED = "refused"  # the server could not provide it this time; the next fetch asks again


class RemoteDataFetcher:
    #: An unanswered request blocks the next ones (its late answer must not be taken for theirs)
    #: until this long after it was sent.
    ABANDON_AFTER = 15.0

    def __init__(self, max_size: int, send: Callable[[int], bool]):
        """`send` asks the server for a format; data larger than `max_size` counts as a failure."""
        self._condition = threading.Condition()
        self._max_size = max_size
        self._send = send
        # Guarded by _condition.
        self._generation = 0
        self._outcomes: dict[int, bytes | str] = {}
        self._in_flight: tuple[int, int, float] | None = None  # format id, generation, sent at

    @property
    def generation(self) -> int:
        """Identifies the current remote clipboard for `discard`."""
        with self._condition:
            return self._generation

    def reset(self) -> None:
        """The remote clipboard changed: cached and pending data belong to the old one."""
        with self._condition:
            self._generation += 1
            self._outcomes = {}
            self._condition.notify_all()

    def discard(self, generation: int) -> None:
        """Frees the cache if it still belongs to `generation` (nobody needs that clipboard any more)."""
        with self._condition:
            if generation == self._generation:
                self._outcomes = {}

    def cached_data(self, format_id: int) -> bytes | None:
        """Already fetched data, without asking the server."""
        with self._condition:
            outcome = self._outcomes.get(format_id)
            return outcome if isinstance(outcome, bytes) else None

    def fetch(self, format_id: int, timeout: float) -> bytes | None:
        """Data of the current remote clipboard in `format_id`; None on failure, timeout or a
        clipboard change while waiting."""
        deadline = time.monotonic() + timeout
        with self._condition:
            wanted = self._generation
            while self._generation == wanted:
                outcome = self._outcomes.get(format_id)
                if isinstance(outcome, bytes):
                    return outcome
                if outcome == _TOO_LARGE:
                    return None
                if outcome == _REFUSED:
                    del self._outcomes[format_id]
                    return None
                flight = self._in_flight
                if flight is not None and time.monotonic() - flight[2] > self.ABANDON_AFTER:
                    self._in_flight = None
                if self._in_flight is None:
                    sent_at = time.monotonic()
                    self._in_flight = (format_id, self._generation, sent_at)
                    self._condition.release()
                    try:
                        sent = self._send(format_id)
                    finally:
                        self._condition.acquire()
                    if not sent:
                        if self._in_flight is not None and self._in_flight[2] == sent_at:
                            self._in_flight = None
                        self._condition.notify_all()
                        return None
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._condition.wait(remaining):
                    return None
            return None

    def deliver(self, data: bytes | None) -> None:
        """The server's answer to the request in flight."""
        with self._condition:
            flight = self._in_flight
            if flight is not None:
                self._in_flight = None
                format_id, generation, _sent_at = flight
                if generation == self._generation:
                    if data is not None:
                        self._outcomes[format_id] = data if len(data) <= self._max_size else _TOO_LARGE
                    else:
                        self._outcomes[format_id] = _REFUSED
                        log.info("server refused format %d", format_id)
            self._condition.notify_all()
