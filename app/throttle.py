"""Courtesy policy (RF-2): the monitor must never be the problem.

Guarantees, in order of acquisition inside :meth:`Throttle.slot`:

1. **Per-host serialization** — a per-host lock ensures at most one monitor
   session against a given host at any time, no matter how many connections
   point to it.
2. **Per-host spacing** — at least ``host_spacing_s`` seconds between the *end*
   of one check and the *start* of the next (end→start is stricter than
   start→start).
3. **Per-host rate limit** — sliding 60 s window capped at
   ``host_max_checks_per_min`` check starts.
4. **Global concurrency** — a semaphore caps simultaneous checks across all
   hosts. It is acquired *last* so a check waiting out host courtesy never
   wastes a global slot.

``clock``/``sleep`` are injectable so tests can verify timing without real
waits. The module also provides the jitter and backoff helpers used by the
scheduler.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

_RATE_WINDOW_S = 60.0


@dataclass(frozen=True)
class CourtesyPolicy:
    global_concurrency: int = 10
    host_spacing_s: float = 5.0
    host_max_checks_per_min: int = 6
    backoff_cap_s: float = 300.0
    jitter_ratio: float = 0.10


def jittered(interval_s: float, ratio: float = 0.10, rng: random.Random | None = None) -> float:
    """Interval with ±ratio uniform jitter, so checks never synchronize."""
    r: random.Random = rng if rng is not None else random  # type: ignore[assignment]
    return interval_s * r.uniform(1.0 - ratio, 1.0 + ratio)


def backoff_delay(interval_s: float, failures_after_confirm: int, cap_s: float) -> float:
    """Delay before the next check once DOWN is confirmed (interval × 2ⁿ, capped).

    Never returns less than the base interval, even if the cap is misconfigured
    below it: during an outage we only ever slow down, never speed up.
    """
    n = max(1, failures_after_confirm)
    return min(interval_s * (2**n), max(cap_s, interval_s))


class _HostState:
    __slots__ = ("lock", "last_finished", "recent_starts")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_finished: float | None = None
        self.recent_starts: deque[float] = deque()


class Throttle:
    def __init__(
        self,
        policy: CourtesyPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.policy = policy or CourtesyPolicy()
        self._clock = clock
        self._sleep = sleep
        self._global = threading.BoundedSemaphore(self.policy.global_concurrency)
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()

    def _state(self, host: str) -> _HostState:
        key = host.strip().lower()
        with self._hosts_lock:
            return self._hosts.setdefault(key, _HostState())

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        """Acquire the right to run one check against ``host``."""
        state = self._state(host)
        with state.lock:
            self._wait_courtesy(state)
            self._global.acquire()
            state.recent_starts.append(self._clock())
            try:
                yield
            finally:
                self._global.release()
                state.last_finished = self._clock()

    def _wait_courtesy(self, state: _HostState) -> None:
        """Sleep (holding the host lock) until spacing and rate limit allow a start."""
        while True:
            now = self._clock()
            wait = 0.0
            if state.last_finished is not None:
                wait = max(wait, state.last_finished + self.policy.host_spacing_s - now)
            expiry = now - _RATE_WINDOW_S
            while state.recent_starts and state.recent_starts[0] <= expiry:
                state.recent_starts.popleft()
            if len(state.recent_starts) >= self.policy.host_max_checks_per_min:
                wait = max(wait, state.recent_starts[0] + _RATE_WINDOW_S - now)
            if wait <= 0:
                return
            self._sleep(wait)
