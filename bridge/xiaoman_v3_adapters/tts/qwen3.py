"""Qwen3-TTS MLX provider with native PCM streaming."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np

from ..cancel import CancelInput, raise_if_cancelled
from .base import TTSProvider, TTSResult, VoiceProfile

logger = logging.getLogger(__name__)
ModelLoader = Callable[..., Any]


class Qwen3TTS(TTSProvider):
    """Qwen3-TTS Base model using the configured reference voice."""

    def __init__(
        self,
        model_name: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        ref_audio_path: str | Path | None = None,
        ref_text: str | None = None,
        ref_text_path: str | Path | None = None,
        language: str = "zh",
        streaming_interval: float = 0.32,
        max_tokens: int = 2048,
        temperature: float = 0.9,
        top_k: int = 50,
        top_p: float = 1.0,
        repetition_penalty: float = 1.05,
        model_loader: ModelLoader | None = None,
    ) -> None:
        if not language:
            raise ValueError("language must not be empty")
        if streaming_interval <= 0:
            raise ValueError("streaming_interval must be positive")
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.model_name = model_name
        self.ref_audio_path = str(ref_audio_path) if ref_audio_path else None
        self.ref_text = ref_text.strip() if ref_text else None
        if ref_text_path and not self.ref_text:
            path = Path(ref_text_path)
            if path.exists():
                self.ref_text = path.read_text(encoding="utf-8").strip()
        self.language = language
        self.streaming_interval = float(streaming_interval)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.top_k = int(top_k)
        self.top_p = float(top_p)
        self.repetition_penalty = float(repetition_penalty)
        self._model_loader = model_loader
        self._model: Any | None = None
        self._sample_rate = 24000
        self._last_error: str | None = None
        self._model_lock = threading.RLock()

    @property
    def provider_name(self) -> str:
        return "qwen3"

    @property
    def sample_rate(self) -> int:
        with self._model_lock:
            return self._sample_rate

    @property
    def loaded(self) -> bool:
        with self._model_lock:
            return self._model is not None

    @property
    def ref_cache_ready(self) -> bool:
        # mlx-audio caches reference codes during the first generation. At
        # startup we can still validate the source contract without generating.
        return (
            self.ref_audio_path is None
            or (Path(self.ref_audio_path).is_file() and bool(self.ref_text))
        )

    def _load_model(self) -> Any:
        loader = self._model_loader
        if loader is None:
            from mlx_audio.tts import load_model

            loader = load_model
        return loader(model_path=self.model_name)

    def _ensure_model_locked(self) -> None:
        try:
            if self._model is None:
                started = time.perf_counter()
                self._model = self._load_model()
                self._sample_rate = int(getattr(self._model, "sample_rate", 24000))
                logger.info(
                    "Qwen3-TTS model loaded in %.1fs (sample_rate=%d)",
                    time.perf_counter() - started,
                    self._sample_rate,
                )
            if self.ref_audio_path is not None:
                if not Path(self.ref_audio_path).is_file():
                    raise FileNotFoundError(
                        f"Qwen3-TTS reference audio not found: {self.ref_audio_path}"
                    )
                if not self.ref_text:
                    raise ValueError(
                        "Qwen3-TTS voice cloning requires ref_text matching ref_audio"
                    )
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise

    def prewarm(self) -> Mapping[str, Any]:
        with self._model_lock:
            self._ensure_model_locked()
            return dict(self._health_locked())

    def _health_locked(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "loaded": self._model is not None,
            "ref_cache_required": self.ref_audio_path is not None,
            "ref_cache_ready": self.ref_cache_ready,
            "sample_rate": self._sample_rate,
            "streaming": True,
            "streaming_interval": self.streaming_interval,
            "last_error": self._last_error,
        }

    def health(self) -> Mapping[str, Any]:
        with self._model_lock:
            return dict(self._health_locked())

    def _generate_kwargs(self, text: str, *, stream: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "text": text,
            "lang_code": self.language,
            "stream": stream,
            "streaming_interval": self.streaming_interval,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
            "verbose": False,
        }
        if self.ref_audio_path is not None:
            kwargs["ref_audio"] = self.ref_audio_path
            kwargs["ref_text"] = self.ref_text
        return kwargs

    @staticmethod
    def _iter_results(results: Any) -> Iterable[Any]:
        if results is None:
            return ()
        if hasattr(results, "audio"):
            return (results,)
        try:
            return iter(results)
        except TypeError:
            return (results,)

    def _result_from_raw(
        self,
        raw: Any,
        text: str,
        started: float,
        turn_id: str | None,
        index: int,
        *,
        streaming: bool,
        final: bool | None = None,
    ) -> TTSResult | None:
        audio = getattr(raw, "audio", raw)
        if audio is None:
            return None
        array = np.asarray(audio, dtype=np.float32).reshape(-1)
        if array.size == 0:
            return None
        if not np.all(np.isfinite(array)):
            raise RuntimeError("Qwen3-TTS generated non-finite audio samples")
        rate = int(getattr(raw, "sample_rate", self._sample_rate) or self._sample_rate)
        self._sample_rate = rate
        return TTSResult(
            audio=array,
            sample_rate=rate,
            duration_sec=float(array.size / rate),
            generation_time_sec=time.perf_counter() - started,
            text=text,
            turn_id=turn_id,
            is_streaming_chunk=bool(getattr(raw, "is_streaming_chunk", streaming)),
            is_final_chunk=(
                bool(getattr(raw, "is_final_chunk", not streaming))
                if final is None
                else final
            ),
            chunk_index=index,
        )

    def stream(
        self,
        text: str,
        profile: Optional[VoiceProfile] = None,
        turn_id: Optional[str] = None,
        cancel: CancelInput = None,
    ):
        del profile
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Qwen3-TTS text must be a non-empty string")
        raise_if_cancelled(cancel)
        with self._model_lock:
            raise_if_cancelled(cancel)
            self._ensure_model_locked()
            raise_if_cancelled(cancel)
            started = time.perf_counter()
            results = self._model.generate(**self._generate_kwargs(text, stream=True))
            iterator = iter(self._iter_results(results))
            index = 0
            while True:
                raise_if_cancelled(cancel)
                try:
                    current = next(iterator)
                except StopIteration:
                    break
                result = self._result_from_raw(
                    current,
                    text,
                    started,
                    turn_id,
                    index,
                    streaming=True,
                )
                if result is not None:
                    yield result
                index += 1

    def synthesize(
        self,
        text: str,
        profile: Optional[VoiceProfile] = None,
        turn_id: Optional[str] = None,
        cancel: CancelInput = None,
    ) -> TTSResult:
        del profile
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Qwen3-TTS text must be a non-empty string")
        raise_if_cancelled(cancel)
        with self._model_lock:
            raise_if_cancelled(cancel)
            self._ensure_model_locked()
            raise_if_cancelled(cancel)
            started = time.perf_counter()
            results = self._model.generate(**self._generate_kwargs(text, stream=False))
            chunks: list[TTSResult] = []
            for index, raw in enumerate(self._iter_results(results)):
                raise_if_cancelled(cancel)
                result = self._result_from_raw(
                    raw, text, started, turn_id, index, streaming=False
                )
                if result is not None:
                    chunks.append(result)
            if not chunks:
                raise RuntimeError(f"Qwen3-TTS generated no audio for: {text[:80]!r}")
            audio = np.concatenate([chunk.audio for chunk in chunks])
            rate = chunks[-1].sample_rate
            return TTSResult(
                audio=audio,
                sample_rate=rate,
                duration_sec=float(audio.size / rate),
                generation_time_sec=time.perf_counter() - started,
                text=text,
                turn_id=turn_id,
            )

    def unload(self) -> None:
        with self._model_lock:
            self._model = None
            self._last_error = None


__all__ = ["Qwen3TTS"]
