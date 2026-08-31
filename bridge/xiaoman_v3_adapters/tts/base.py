"""Small provider boundary shared by v3 text-to-speech backends.

The gateway should depend on this boundary rather than on a concrete model
implementation.  A provider may expose a real streaming implementation later;
the default ``stream`` method deliberately keeps the first provider usable as
a one-result adapter while preserving the same call shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

import numpy as np

from ..cancel import CancelInput, raise_if_cancelled


@dataclass
class TTSResult:
    """One synthesized PCM segment represented as normalized float samples."""

    audio: np.ndarray
    sample_rate: int
    duration_sec: float
    generation_time_sec: float
    text: str
    turn_id: Optional[str] = None
    is_streaming_chunk: bool = False
    is_final_chunk: bool = True
    chunk_index: int = 0


@dataclass(frozen=True)
class VoiceProfile:
    """Provider-neutral voice selection data.

    ``instruct`` is optional because providers do not share the same voice
    control vocabulary.  A provider must validate or translate it rather than
    silently sending unsupported controls to its model.
    """

    name: str = "default"
    instruct: Optional[str] = None


class TTSProvider(ABC):
    """Minimal synchronous boundary for a local TTS provider.

    The synchronous methods are intentional: the v3 gateway can run them in
    its worker thread, and a provider can keep one lock around model state.
    ``stream`` is an iterator so a later provider can yield sentence/audio
    chunks without changing the gateway-facing boundary.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable provider identifier for health and diagnostics."""

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        profile: Optional[VoiceProfile] = None,
        turn_id: Optional[str] = None,
        cancel: CancelInput = None,
    ) -> TTSResult:
        """Synthesize one text segment."""

    def stream(
        self,
        text: str,
        profile: Optional[VoiceProfile] = None,
        turn_id: Optional[str] = None,
        cancel: CancelInput = None,
    ) -> Iterator[TTSResult]:
        """Yield synthesized chunks; the default provider yields one result."""

        raise_if_cancelled(cancel)
        yield self.synthesize(
            text,
            profile=profile,
            turn_id=turn_id,
            cancel=cancel,
        )

    @abstractmethod
    def prewarm(self) -> Mapping[str, Any]:
        """Load model state and prepare any reusable voice prompt."""

    @abstractmethod
    def health(self) -> Mapping[str, Any]:
        """Return a lightweight, serialization-friendly status snapshot."""

    @abstractmethod
    def unload(self) -> None:
        """Release model state and invalidate provider caches."""
