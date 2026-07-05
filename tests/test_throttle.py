"""Courtesy policy tests: per-host lock, spacing, rate limit, jitter, backoff."""
from __future__ import annotations

import random
import threading
import time

from app.throttle import CourtesyPolicy, Throttle, backoff_delay, jittered


class FakeClock:
    """Deterministic clock: ``sleep`` simply advances time."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def make_throttle(clock: FakeClock, **overrides) -> Throttle:
    policy = CourtesyPolicy(**overrides)
    return Throttle(policy, clock=clock.now, sleep=clock.sleep)


def test_host_spacing_end_to_start():
    clock = FakeClock()
    throttle = make_throttle(clock, host_spacing_s=5.0, host_max_checks_per_min=1000)

    with throttle.slot("srv1"):
        clock.sleep(1.0)  # the check itself takes 1 s → ends at t=1
    with throttle.slot("srv1"):
        # must not start before last_finished (1.0) + spacing (5.0)
        assert clock.now() >= 6.0

    # different host: no spacing applies
    with throttle.slot("srv2"):
        assert clock.now() < 12.0


def test_rate_limit_sliding_window():
    clock = FakeClock()
    throttle = make_throttle(clock, host_spacing_s=0.0, host_max_checks_per_min=3)

    for _ in range(3):
        with throttle.slot("srv"):
            pass
    assert clock.now() == 0.0  # first three start immediately

    with throttle.slot("srv"):
        # fourth start must wait until the window frees: first start (0) + 60 s
        assert clock.now() >= 60.0


def test_host_names_are_case_insensitive():
    clock = FakeClock()
    throttle = make_throttle(clock, host_spacing_s=5.0, host_max_checks_per_min=1000)

    with throttle.slot("SRV.example.LOCAL"):
        clock.sleep(1.0)
    with throttle.slot("srv.example.local"):
        assert clock.now() >= 6.0


def test_same_host_checks_never_overlap_with_real_threads():
    policy = CourtesyPolicy(
        global_concurrency=10, host_spacing_s=0.05, host_max_checks_per_min=1000
    )
    throttle = Throttle(policy)
    spans: list[tuple[float, float]] = []
    guard = threading.Lock()

    def worker() -> None:
        with throttle.slot("ftp.acme.local"):
            start = time.monotonic()
            time.sleep(0.03)
            end = time.monotonic()
            with guard:
                spans.append((start, end))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    spans.sort()
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert next_start >= prev_end, "dos sesiones simultáneas contra el mismo host"
        assert next_start - prev_end >= 0.03, "no se respetó el espaciado entre chequeos"


def test_global_concurrency_cap_across_hosts():
    policy = CourtesyPolicy(global_concurrency=2, host_spacing_s=0.0, host_max_checks_per_min=1000)
    throttle = Throttle(policy)
    counters = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def worker(index: int) -> None:
        with throttle.slot(f"host-{index}"):
            with guard:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            time.sleep(0.03)
            with guard:
                counters["active"] -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counters["peak"] <= 2
    assert counters["peak"] >= 1


def test_jitter_stays_within_bounds_and_varies():
    values = [jittered(60.0, 0.10, rng=random.Random(seed)) for seed in range(200)]
    assert all(54.0 <= value <= 66.0 for value in values)
    assert len({round(value, 6) for value in values}) > 1


def test_backoff_progression_and_cap():
    assert backoff_delay(60, 1, 300) == 120
    assert backoff_delay(60, 2, 300) == 240
    assert backoff_delay(60, 3, 300) == 300  # 480 capped
    assert backoff_delay(60, 10, 300) == 300


def test_backoff_never_faster_than_base_interval():
    # A cap below the base interval must not make the monitor check *faster*
    # during an outage.
    assert backoff_delay(600, 1, 300) == 600
    # And n=0 (defensive) still slows down.
    assert backoff_delay(60, 0, 300) == 120
