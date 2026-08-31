"""Provider-neutral STT adapters for the DSH bridge.

``MacSTTProvider`` is the native Apple-silicon path: it lazily loads the same
MLX-Audio Whisper model validated by the upstream v3 project.  It does not
import that project; the only runtime dependency is the installed
``mlx_audio`` package.

``LegacyWhisperProvider`` keeps the existing ``speech_to_speech`` bridge
handler behind the same boundary.  Its import is lazy so a macOS install does
not need the legacy package merely to import this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable, Mapping

import numpy as np

from ..cancel import CancelInput, raise_if_cancelled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class STTResult:
    """Normalized result returned by every STT provider."""

    text: str
    language: str
    duration_sec: float
    processing_time_sec: float


# The upstream v3 implementation calls this shape ASRResult.  Keep the alias
# so code migrated from ``voice.asr.whisper_asr`` can move without translation.
ASRResult = STTResult


class STTProvider(ABC):
    """Small synchronous boundary shared by local STT backends."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable provider identifier for health/diagnostic output."""

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Provider input sample rate after adapter normalization."""

    @abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
        cancel: CancelInput = None,
    ) -> STTResult:
        """Transcribe one mono PCM segment."""

    def load(self) -> None:
        """Eagerly prepare model state; default providers are lazy."""

    def prewarm(self) -> Mapping[str, Any]:
        self.load()
        return self.health()

    @abstractmethod
    def health(self) -> Mapping[str, Any]:
        """Return a serialization-friendly status snapshot."""

    @abstractmethod
    def unload(self) -> None:
        """Release model state and invalidate provider caches."""


def _resample_to_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Normalize mono PCM and resample without importing a heavy DSP stack."""

    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    array = np.asarray(audio, dtype=np.float32)
    if array.ndim == 2:
        # Accept either (frames, channels) or (channels, frames) only when one
        # dimension is unambiguously a channel dimension.
        if array.shape[1] <= 8:
            array = array.mean(axis=1)
        elif array.shape[0] <= 8:
            array = array.mean(axis=0)
        else:
            raise ValueError("audio must be mono or have a small channel dimension")
    if array.ndim != 1:
        raise ValueError("audio must be a one-dimensional PCM array")
    if array.size == 0:
        raise ValueError("STT audio is empty")
    array = np.ascontiguousarray(array, dtype=np.float32)
    if sample_rate != 16_000:
        output_size = max(1, int(round(array.size * 16_000 / sample_rate)))
        old_x = np.linspace(0.0, 1.0, num=array.size, endpoint=False)
        new_x = np.linspace(0.0, 1.0, num=output_size, endpoint=False)
        array = np.interp(new_x, old_x, array).astype(np.float32)
    peak = float(np.abs(array).max())
    if peak > 1.0:
        array = array / peak
    return np.ascontiguousarray(array, dtype=np.float32)


class MacSTTProvider(STTProvider):
    """MLX-Audio Whisper provider for Apple-silicon macOS.

    ``model_loader`` is a test seam.  In production it resolves lazily to
    ``mlx_audio.stt.load`` and therefore does not download anything at import
    time.  Cancellation is cooperative: a token is checked before and after
    the native call, but cannot safely interrupt that call itself.
    """

    def __init__(
        self,
        model_name: str = "mlx-community/whisper-large-v3-turbo-asr-fp16",
        *,
        model_loader: Callable[[str], Any] | None = None,
    ) -> None:
        if not model_name:
            raise ValueError("model_name must not be empty")
        self.model_name = model_name
        self._model_loader = model_loader
        self._model: Any | None = None
        self._model_lock = threading.RLock()
        self._last_error: str | None = None

    @property
    def provider_name(self) -> str:
        return "mac-mlx-whisper"

    @property
    def sample_rate(self) -> int:
        return 16_000

    @property
    def loaded(self) -> bool:
        with self._model_lock:
            return self._model is not None

    def _load_model(self) -> Any:
        if self._model_loader is not None:
            return self._model_loader(self.model_name)
        from mlx_audio.stt import load as load_stt

        return load_stt(self.model_name)

    def load(self) -> None:
        with self._model_lock:
            if self._model is not None:
                return
            try:
                started = time.perf_counter()
                self._model = self._load_model()
                self._last_error = None
                logger.info(
                    "Loaded MLX Whisper model %s in %.1fs",
                    self.model_name,
                    time.perf_counter() - started,
                )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
        cancel: CancelInput = None,
    ) -> STTResult:
        raise_if_cancelled(cancel)
        normalized = _resample_to_16k(audio, sample_rate)
        started = time.perf_counter()
        with self._model_lock:
            raise_if_cancelled(cancel)
            self.load()
            result = self._model.generate(normalized)
            raise_if_cancelled(cancel)

        text = str(getattr(result, "text", result)).strip()
        language = getattr(result, "language", None)
        if not isinstance(language, str) or not language:
            language = getattr(result, "language_code", "zh")
        if not isinstance(language, str) or not language:
            language = "zh"
        elapsed = time.perf_counter() - started
        return STTResult(
            text=text,
            language=language,
            duration_sec=float(normalized.size / 16_000),
            processing_time_sec=elapsed,
        )

    def health(self) -> Mapping[str, Any]:
        with self._model_lock:
            return {
                "provider": self.provider_name,
                "model": self.model_name,
                "loaded": self._model is not None,
                "sample_rate": self.sample_rate,
                "last_error": self._last_error,
            }

    def unload(self) -> None:
        with self._model_lock:
            self._model = None
            self._last_error = None


LegacyHandlerFactory = Callable[[], Any]


class LegacyWhisperProvider(STTProvider):
    """Adapter for the existing ``speech_to_speech`` Whisper handler.

    The handler is intentionally injected/lazy.  This keeps the adapter
    importable on macOS even when only MLX is installed, and makes migration
    tests independent from a model download.
    """

    def __init__(
        self,
        *,
        setup_kwargs: Mapping[str, Any] | None = None,
        handler_factory: LegacyHandlerFactory | None = None,
        handler: Any | None = None,
    ) -> None:
        self.setup_kwargs = dict(setup_kwargs or {})
        self._handler_factory = handler_factory
        self._handler = handler
        self._lock = threading.RLock()
        self._last_error: str | None = None

    @property
    def provider_name(self) -> str:
        return "legacy-whisper"

    @property
    def sample_rate(self) -> int:
        return 16_000

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._handler is not None

    def _default_handler_factory(self) -> Any:
        from queue import Queue
        from threading import Event

        from speech_to_speech.STT.whisper_stt_handler import WhisperSTTHandler

        return WhisperSTTHandler(
            Event(),
            queue_in=Queue(),
            queue_out=Queue(),
            setup_args=(),
            setup_kwargs=dict(self.setup_kwargs),
        )

    def load(self) -> None:
        with self._lock:
            if self._handler is not None:
                return
            try:
                factory = self._handler_factory or self._default_handler_factory
                self._handler = factory()
                self._last_error = None
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise

    @staticmethod
    def _first_result(items: Any) -> Any:
        if items is None:
            return None
        if hasattr(items, "text"):
            return items
        try:
            return next(iter(items), None)
        except TypeError:
            return items

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
        cancel: CancelInput = None,
    ) -> STTResult:
        raise_if_cancelled(cancel)
        normalized = _resample_to_16k(audio, sample_rate)
        started = time.perf_counter()
        with self._lock:
            raise_if_cancelled(cancel)
            self.load()
            try:
                from speech_to_speech.pipeline.messages import VADAudio

                item = self._first_result(
                    self._handler.process(VADAudio(audio=normalized))
                )
            except IndexError:
                # The legacy handler can index past a one-token Whisper result
                # for silence/very short input.  Empty speech is not a bridge
                # failure; preserve continuous listening.
                logger.warning("Legacy Whisper returned a degenerate result")
                item = None
            raise_if_cancelled(cancel)

        text = str(getattr(item, "text", "") or "").strip()
        language = getattr(item, "language_code", None)
        if not isinstance(language, str) or not language:
            language = getattr(item, "language", "zh")
        if not isinstance(language, str) or not language:
            language = "zh"
        return STTResult(
            text=text,
            language=language,
            duration_sec=float(normalized.size / 16_000),
            processing_time_sec=time.perf_counter() - started,
        )

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "provider": self.provider_name,
                "loaded": self._handler is not None,
                "sample_rate": self.sample_rate,
                "last_error": self._last_error,
            }

    def unload(self) -> None:
        with self._lock:
            handler = self._handler
            self._handler = None
            self._last_error = None
        close = getattr(handler, "close", None)
        if callable(close):
            close()


__all__ = [
    "ASRResult",
    "LegacyWhisperProvider",
    "MacSTTProvider",
    "STTProvider",
    "STTResult",
]
