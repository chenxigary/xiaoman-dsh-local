"""Provider-neutral VAD event stream over the migrated energy VAD."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

from .energy_vad import VADConfig, VADState


@dataclass(frozen=True, slots=True)
class VADEvent:
    """One normalized state transition emitted by :class:`VADEventStream`.

    ``kind`` is intentionally wire-friendly and stable:

    * ``speech_start`` — a candidate has met the minimum speech duration;
    * ``speech_end`` — a soft endpoint or final commit (see flags);
    * ``speech_reopen`` — speech resumed inside the speculative grace window.

    The event's ``audio`` is a defensive snapshot for endpoint events and is
    ``None`` for starts/reopens.  Callers that do not need audio can ignore it.
    """

    kind: str
    sample_index: int
    audio: np.ndarray | None = None
    soft: bool = False
    final: bool = False
    reopened: bool = False

    @property
    def is_speech_start(self) -> bool:
        return self.kind == "speech_start"

    @property
    def is_speech_end(self) -> bool:
        return self.kind == "speech_end"

    def as_dict(self) -> dict[str, object]:
        """Return the compact JSON shape used by websocket adapters."""

        payload: dict[str, object] = {
            "event": self.kind,
            "sample_index": self.sample_index,
        }
        if self.soft:
            payload["soft"] = True
        if self.final:
            payload["final"] = True
        if self.reopened:
            payload["reopened"] = True
        return payload


@dataclass(frozen=True, slots=True)
class VADProcessResult:
    """Output of one audio feed operation."""

    audio: np.ndarray | None
    events: tuple[VADEvent, ...]


EventCallback = Callable[[VADEvent], None]


class VADEventStream:
    """Turn the verified stateful VAD into a callback/event adapter.

    The wrapped ``VADState`` remains available through ``state`` for callers
    that need ``snapshot_audio`` or tuning internals.  Event callbacks are
    invoked synchronously, in audio-clock order, after each input chunk.
    """

    def __init__(
        self,
        state: VADState | None = None,
        *,
        on_event: EventCallback | None = None,
    ) -> None:
        self.state = state or VADState()
        self._on_event = on_event

    @property
    def config(self) -> VADConfig:
        return self.state.config

    @property
    def is_speaking(self) -> bool:
        return self.state.is_speaking

    @property
    def audio_clock_samples(self) -> int:
        return int(getattr(self.state, "_total_samples", 0))

    def reset(self) -> None:
        self.state.reset()

    def _event_for_raw(
        self,
        raw: str,
        completed: np.ndarray | None,
    ) -> VADEvent | None:
        sample_index = self.audio_clock_samples
        if raw == "start":
            return VADEvent("speech_start", sample_index)
        if raw == "soft_end":
            # With speculative reopen disabled, the legacy state machine
            # commits immediately after queuing ``soft_end`` and resets its
            # buffer before returning.  In that mode ``completed`` is the
            # only available endpoint snapshot.
            snapshot = self.state.snapshot_audio()
            if snapshot is None:
                snapshot = completed
            return VADEvent(
                "speech_end",
                sample_index,
                audio=snapshot,
                soft=True,
                final=False,
            )
        if raw == "reopen":
            return VADEvent(
                "speech_reopen",
                sample_index,
                reopened=True,
            )
        if raw == "commit":
            return VADEvent(
                "speech_end",
                sample_index,
                audio=completed,
                soft=False,
                final=True,
            )
        return None

    def process_chunk(self, audio: np.ndarray) -> VADProcessResult:
        completed = self.state.process_chunk(audio)
        raw_events = self.state.take_events()
        events: list[VADEvent] = []
        completed_attached = False
        for raw in raw_events:
            event = self._event_for_raw(
                raw,
                completed
                if raw in {"soft_end", "commit"} and not completed_attached
                else None,
            )
            if event is None:
                continue
            if raw == "commit" and completed is not None and not completed_attached:
                completed_attached = True
            events.append(event)
            if self._on_event is not None:
                self._on_event(event)

        # ``process_chunk`` can contain a very large packet with a start and
        # commit in one call.  The copied VAD emits an explicit ``start`` in
        # its activation helper; this fallback protects custom VADState
        # subclasses that predate that event.
        if not events and completed is not None:
            event = VADEvent(
                "speech_end",
                self.audio_clock_samples,
                audio=completed,
                final=True,
            )
            events.append(event)
            if self._on_event is not None:
                self._on_event(event)

        return VADProcessResult(
            audio=completed,
            events=tuple(events),
        )

    # ``feed`` is the bridge-friendly spelling used by websocket/session code.
    feed = process_chunk


class EnergyVADAdapter(VADEventStream):
    """Named adapter for dependency injection at the STT/VAD boundary."""

    def __init__(
        self,
        config: VADConfig | None = None,
        *,
        state: VADState | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        if state is not None and config is not None:
            raise ValueError("pass either config or state, not both")
        super().__init__(state or VADState(config), on_event=on_event)


__all__ = [
    "EnergyVADAdapter",
    "VADEvent",
    "VADEventStream",
    "VADProcessResult",
]
