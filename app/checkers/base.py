"""Common checker interface.

A concrete checker only implements :meth:`_execute`, which either returns one
``TargetResult`` per verified target or raises (``CheckError`` for classified
connection-level failures, anything else gets classified centrally). The base
class owns timing, exception mapping and the UP/DEGRADED/DOWN decision so that
logic exists exactly once.

Status semantics (RF-2):
- DOWN     — could not connect or authenticate.
- DEGRADED — connected and authenticated, but a target failed or latency is
             above the configured threshold.
- UP       — everything checked out.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from time import perf_counter

from app.errors import ErrorType, classify_exception, truncate
from app.models import CheckResult, ConnectionConfig, Status, TargetResult


class BaseChecker(ABC):
    def check(self, cfg: ConnectionConfig, secret: str | None = None) -> CheckResult:
        start = perf_counter()
        try:
            target_results = self._execute(cfg, secret)
        except Exception as exc:
            error_type, message = classify_exception(exc)
            return CheckResult(
                status=Status.DOWN,
                latency_ms=None,
                error_type=error_type,
                error_msg=message,
            )
        latency_ms = (perf_counter() - start) * 1000.0

        failed = [t for t in target_results if not t.ok]
        if failed:
            first = failed[0]
            suffix = f" (+{len(failed) - 1} objetivos más)" if len(failed) > 1 else ""
            return CheckResult(
                status=Status.DEGRADED,
                latency_ms=latency_ms,
                error_type=first.error_type,
                error_msg=truncate(f"objetivo '{first.target}': {first.message}{suffix}"),
                targets=target_results,
            )
        if cfg.degraded_ms is not None and latency_ms > cfg.degraded_ms:
            return CheckResult(
                status=Status.DEGRADED,
                latency_ms=latency_ms,
                error_type=ErrorType.LATENCY,
                error_msg=f"latencia {latency_ms:.0f} ms supera el umbral de {cfg.degraded_ms} ms",
                targets=target_results,
            )
        return CheckResult(
            status=Status.UP,
            latency_ms=latency_ms,
            targets=target_results,
        )

    @abstractmethod
    def _execute(self, cfg: ConnectionConfig, secret: str | None) -> list[TargetResult]:
        """Connect, authenticate, verify each target, close cleanly.

        Must never leave sessions open (RF-2): every code path closes the
        connection, including error paths.
        """
