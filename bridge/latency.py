"""Small, dependency-free latency event recorder for the voice path.

The bridge intentionally emits one-line JSON records through its normal
logger instead of introducing a metrics service.  This keeps the local
baseline observable while preserving the existing HTTP contract and makes the
events easy to grep, ship, or parse later.

Configuration is read from ``bridge-config.json``::

    "latency": {
      "enabled": true,
      "sample_rate": 1.0
    }

The recorder never includes audio or user text.  Callers may add safe scalar
fields (for example byte counts, character counts, and HTTP status) to an
event.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


@dataclass(frozen=True)
class LatencyConfig:
    """Runtime switches for structured latency events."""

    enabled: bool = True
    sample_rate: float = 1.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "LatencyConfig":
        if not value:
            return cls()
        enabled = value.get("enabled", True)
        sample_rate = value.get("sample_rate", 1.0)
        try:
            rate = float(sample_rate)
        except (TypeError, ValueError):
            rate = 1.0
        return cls(
            enabled=bool(enabled),
            sample_rate=max(0.0, min(1.0, rate)),
        )


def new_trace_id() -> str:
    """Return a short, log-safe correlation id for one voice request."""

    return uuid.uuid4().hex


class LatencyRecorder:
    """Emit sampled, structured timing events through a standard logger."""

    def __init__(
        self,
        config: LatencyConfig | None = None,
        *,
        logger: logging.Logger | None = None,
        clock: Any = time.perf_counter,
        wall_clock: Any = time.time,
        random_fn: Any = random.random,
    ) -> None:
        self.config = config or LatencyConfig()
        self.logger = logger or logging.getLogger("voice.latency")
        self._clock = clock
        self._wall_clock = wall_clock
        self._random = random_fn

    def start(
        self,
        operation: str,
        *,
        trace_id: str | None = None,
        **fields: Any,
    ) -> "LatencySpan":
        """Start a span; it is cheap even when event emission is disabled."""

        sampled = self.config.enabled and self._random() < self.config.sample_rate
        return LatencySpan(
            recorder=self,
            operation=operation,
            trace_id=trace_id or new_trace_id(),
            started=self._clock(),
            wall_started=self._wall_clock(),
            sampled=sampled,
            fields=fields,
        )

    def emit(self, payload: Mapping[str, Any]) -> None:
        """Write one JSON object; logging handlers add timestamps/levels."""

        if not self.config.enabled:
            return
        self.logger.info(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


class LatencySpan:
    """A request span with optional named stage measurements."""

    def __init__(
        self,
        *,
        recorder: LatencyRecorder,
        operation: str,
        trace_id: str,
        started: float,
        wall_started: float,
        sampled: bool,
        fields: Mapping[str, Any],
    ) -> None:
        self.recorder = recorder
        self.operation = operation
        self.trace_id = trace_id
        self.started = started
        self.wall_started = wall_started
        self.sampled = sampled
        self.fields = dict(fields)
        self.stages: dict[str, float] = {}
        self.finished = False

    def mark(self, stage: str, started: float | None = None) -> float:
        """Record elapsed milliseconds for a stage and return that value."""

        origin = self.recorder._clock() if started is None else started
        # A stage started explicitly is measured until now; without an
        # explicit origin it is an elapsed offset from the whole span.
        elapsed_ms = (
            (self.recorder._clock() - origin) * 1000.0
            if started is not None
            else (origin - self.started) * 1000.0
        )
        value = round(max(0.0, elapsed_ms), 3)
        self.stages[stage] = value
        return value

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure a block without changing its exception behavior."""

        started = self.recorder._clock()
        try:
            yield
        finally:
            self.mark(name, started)

    def finish(self, *, status: str = "ok", **fields: Any) -> dict[str, Any]:
        """Finish and emit the span once; return the event for tests/callers."""

        if self.finished:
            return {}
        self.finished = True
        duration_ms = round(max(0.0, (self.recorder._clock() - self.started) * 1000.0), 3)
        payload: dict[str, Any] = {
            "event": "voice.latency",
            "operation": self.operation,
            "trace_id": self.trace_id,
            "status": status,
            "duration_ms": duration_ms,
            "stages_ms": dict(self.stages),
            "timestamp": self.wall_started,
            **self.fields,
            **fields,
        }
        if self.sampled:
            self.recorder.emit(payload)
        return payload
