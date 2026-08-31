"""Thread-safe OmniVoice MLX provider for xiaoman-v3.

The MLX-Audio OmniVoice implementation performs reference-audio tokenization
inside ``generate`` when ``ref_audio`` is supplied.  That is convenient for a
demo, but it makes every product turn pay the voice-clone cost again.  This
provider moves that operation into the one-time model warm-up path and sends
only ``ref_tokens`` and ``ref_text`` to subsequent generations.
"""

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
RefPromptBuilder = Callable[..., Any]


class OmniVoiceTTS(TTSProvider):
    """OmniVoice MLX text-to-speech with one-time voice-prompt caching.

    All model loading, reference-prompt construction, inference, and unload
    operations share ``_model_lock``.  This is important because cancelling an
    ``asyncio.to_thread`` caller does not stop the underlying worker thread;
    the next request must wait for the old MLX operation instead of entering
    the same model concurrently.

    ``model_loader`` and ``ref_prompt_builder`` are optional test seams.  They
    are not needed in production and allow unit tests to verify lifecycle and
    concurrency without loading or downloading a model.
    """

    def __init__(
        self,
        model_name: str = "mlx-community/OmniVoice-bf16",
        ref_audio_path: str | Path | None = None,
        ref_text: str | None = None,
        ref_text_path: str | Path | None = None,
        num_steps: int = 32,
        instruct: str = "女，青年，中音调",
        language: str = "chinese",
        guidance_scale: float = 2.0,
        class_temperature: float = 0.0,
        position_temperature: float = 5.0,
        layer_penalty_factor: float = 5.0,
        t_shift: float = 0.1,
        ref_audio_max_duration_s: float = 15.0,
        model_loader: ModelLoader | None = None,
        ref_prompt_builder: RefPromptBuilder | None = None,
    ):
        if not isinstance(num_steps, int) or isinstance(num_steps, bool) or num_steps < 1:
            raise ValueError("num_steps must be a positive integer")
        if not language:
            raise ValueError("language must not be empty")

        self.model_name = model_name
        self.num_steps = num_steps
        self.instruct = instruct
        self.language = language
        self.guidance_scale = float(guidance_scale)
        self.class_temperature = float(class_temperature)
        self.position_temperature = float(position_temperature)
        self.layer_penalty_factor = float(layer_penalty_factor)
        self.t_shift = float(t_shift)
        self.ref_audio_max_duration_s = float(ref_audio_max_duration_s)

        self.ref_audio_path = str(ref_audio_path) if ref_audio_path else None
        self.ref_text = ref_text.strip() if ref_text else None
        if ref_text_path and not self.ref_text:
            p = Path(ref_text_path)
            if p.exists():
                self.ref_text = p.read_text(encoding="utf-8").strip()
                logger.info("Loaded ref text from %s", p)

        self._model: Any | None = None
        self._ref_tokens: Any | None = None
        self._sample_rate = 24000
        self._last_error: str | None = None
        self._model_loader = model_loader
        self._ref_prompt_builder = ref_prompt_builder
        self._model_lock = threading.RLock()

    @property
    def provider_name(self) -> str:
        return "omnivoice"

    @property
    def sample_rate(self) -> int:
        with self._model_lock:
            return self._sample_rate

    @property
    def loaded(self) -> bool:
        """Whether the MLX model object is resident in this provider."""

        with self._model_lock:
            return self._model is not None

    @property
    def ref_cache_required(self) -> bool:
        return self.ref_audio_path is not None

    @property
    def ref_cache_ready(self) -> bool:
        """Whether the configured reference voice is encoded and reusable."""

        with self._model_lock:
            return not self.ref_cache_required or self._ref_tokens is not None

    def _format_error(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {exc}"

    def _load_model(self) -> Any:
        loader = self._model_loader
        if loader is None:
            from mlx_audio.tts import load_model

            loader = load_model

        # mlx-audio 0.4.7 accepts the model identifier through model_path.
        return loader(model_path=self.model_name)

    def _build_ref_prompt(self, model: Any) -> Any:
        if self.ref_audio_path is None:
            return None

        ref_path = Path(self.ref_audio_path)
        if not ref_path.exists():
            raise FileNotFoundError(
                f"OmniVoice reference audio not found: {ref_path}"
            )
        if not self.ref_text:
            raise ValueError(
                "OmniVoice voice cloning requires ref_text matching ref_audio"
            )

        tokenizer = getattr(model, "audio_tokenizer", None)
        if tokenizer is None:
            raise RuntimeError(
                "OmniVoice model has no audio_tokenizer; cannot build ref_tokens"
            )

        builder = self._ref_prompt_builder
        if builder is None:
            from mlx_audio.tts.models.omnivoice.utils import (
                create_voice_clone_prompt,
            )

            builder = create_voice_clone_prompt

        # This is the only place ref_audio is ever passed.  The generated
        # request path below intentionally has no ref_audio key.
        tokens = builder(
            str(ref_path),
            tokenizer=tokenizer,
            ref_text=self.ref_text,
            max_duration_s=self.ref_audio_max_duration_s,
        )
        if tokens is None:
            raise RuntimeError("OmniVoice create_voice_clone_prompt returned no tokens")

        shape = getattr(tokens, "shape", None)
        size = getattr(tokens, "size", None)
        if shape is not None and any(int(dim) == 0 for dim in shape):
            raise RuntimeError("OmniVoice reference prompt is empty")
        if size is not None and int(size) == 0:
            raise RuntimeError("OmniVoice reference prompt is empty")
        return tokens

    def _ensure_ref_tokens_locked(self) -> None:
        if self._ref_tokens is not None or not self.ref_cache_required:
            return
        if self._model is None:
            raise RuntimeError("OmniVoice model must be loaded before ref_tokens")

        self._ref_tokens = self._build_ref_prompt(self._model)
        logger.info("OmniVoice reference prompt cached from %s", self.ref_audio_path)

    def _ensure_model_locked(self) -> None:
        """Load once, then ensure the reference prompt is cached once."""

        try:
            if self._model is None:
                logger.info("Loading OmniVoice model: %s", self.model_name)
                started = time.perf_counter()
                self._model = self._load_model()
                self._sample_rate = int(getattr(self._model, "sample_rate", 24000))
                logger.info(
                    "OmniVoice model loaded in %.1fs (sample_rate=%d)",
                    time.perf_counter() - started,
                    self._sample_rate,
                )

            # This is intentionally called even when the model was already
            # loaded, so a failed first cache attempt can be retried safely.
            self._ensure_ref_tokens_locked()
            self._last_error = None
        except Exception as exc:
            self._last_error = self._format_error(exc)
            raise

    def _ensure_model(self) -> None:
        """Thread-safe compatibility entry point used by callers/tests."""

        with self._model_lock:
            self._ensure_model_locked()

    def prewarm(self) -> Mapping[str, Any]:
        """Load the model and build the reference prompt before first speech."""

        with self._model_lock:
            self._ensure_model_locked()
            return dict(self._health_locked())

    def _health_locked(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "model": self.model_name,
            "loaded": self._model is not None,
            "ref_cache_required": self.ref_cache_required,
            "ref_cache_ready": (
                not self.ref_cache_required or self._ref_tokens is not None
            ),
            "sample_rate": self._sample_rate,
            "num_steps": self.num_steps,
            "last_error": self._last_error,
        }

    def health(self) -> Mapping[str, Any]:
        """Return a lock-consistent status snapshot without loading a model."""

        with self._model_lock:
            return dict(self._health_locked())

    def selfcheck(self, *, prewarm: bool = False) -> Mapping[str, Any]:
        """Report configuration/readiness, optionally performing warm-up.

        The default is intentionally lightweight and does not download a
        model.  ``prewarm=True`` is the explicit operational path when a
        caller wants to make the provider ready before serving speech.
        """

        errors: list[str] = []
        if self.ref_audio_path is not None:
            if not Path(self.ref_audio_path).exists():
                errors.append(f"reference audio not found: {self.ref_audio_path}")
            if not self.ref_text:
                errors.append("reference text is missing")

        if prewarm:
            try:
                self.prewarm()
            except Exception as exc:
                errors.append(self._format_error(exc))

        with self._model_lock:
            status = self._health_locked()
        status["ok"] = not errors and (
            not self.ref_cache_required or status["ref_cache_ready"]
        )
        status["errors"] = errors
        return status

    def _normalise_profile(
        self, profile: VoiceProfile | None
    ) -> tuple[str, Optional[str]]:
        if profile is None:
            return self.instruct, None
        return profile.instruct or self.instruct, profile.name

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

    def _collect_audio_locked(
        self,
        results: Any,
        text: str,
        started: float,
        turn_id: str | None,
        cancel: CancelInput = None,
    ) -> TTSResult:
        audio_chunks: list[np.ndarray] = []
        sample_rate = self._sample_rate
        for result in self._iter_results(results):
            raise_if_cancelled(cancel)
            raw_audio = getattr(result, "audio", None)
            if raw_audio is None:
                continue
            audio = np.asarray(raw_audio, dtype=np.float32)
            if audio.size == 0:
                continue
            audio_chunks.append(audio.reshape(-1))
            result_rate = getattr(result, "sample_rate", None)
            if result_rate:
                sample_rate = int(result_rate)

        if not audio_chunks:
            raise RuntimeError(
                f"OmniVoice generated no audio for text: {text[:80]!r}"
            )

        audio = np.concatenate(audio_chunks)
        if audio.size == 0:
            raise RuntimeError(
                f"OmniVoice generated empty audio for text: {text[:80]!r}"
            )
        if not np.all(np.isfinite(audio)):
            raise RuntimeError("OmniVoice generated non-finite audio samples")

        generation_time = time.perf_counter() - started
        duration = float(audio.size / sample_rate)
        logger.info(
            "OmniVoice generated %.2fs audio in %.2fs (RTF=%.2f) for: %s",
            duration,
            generation_time,
            duration / generation_time if generation_time > 0 else 0.0,
            text[:50],
        )
        return TTSResult(
            audio=audio,
            sample_rate=sample_rate,
            duration_sec=duration,
            generation_time_sec=generation_time,
            text=text,
            turn_id=turn_id,
        )

    def generate(
        self,
        text: str,
        output_path: Optional[str] = None,
        *,
        profile: VoiceProfile | None = None,
        turn_id: str | None = None,
        cancel: CancelInput = None,
    ) -> TTSResult:
        """Generate one segment using cached voice tokens.

        The entire MLX call remains under one lock.  If an async caller is
        cancelled while its worker thread is still in this method, a later
        worker waits here rather than touching the same MLX model concurrently.
        """

        if not isinstance(text, str) or not text.strip():
            raise ValueError("OmniVoice text must be a non-empty string")
        raise_if_cancelled(cancel)

        with self._model_lock:
            raise_if_cancelled(cancel)
            self._ensure_model_locked()
            raise_if_cancelled(cancel)
            instruct, _profile_name = self._normalise_profile(profile)
            started = time.perf_counter()

            gen_kwargs: dict[str, Any] = {
                "text": text,
                "language": self.language,
                "instruct": instruct,
                "num_steps": self.num_steps,
                "guidance_scale": self.guidance_scale,
                "class_temperature": self.class_temperature,
                "position_temperature": self.position_temperature,
                "layer_penalty_factor": self.layer_penalty_factor,
                "t_shift": self.t_shift,
            }
            if self.ref_cache_required:
                # Do not add ref_audio here.  Passing it would make MLX-Audio
                # re-run create_voice_clone_prompt on every request.
                gen_kwargs["ref_tokens"] = self._ref_tokens
                gen_kwargs["ref_text"] = self.ref_text

            results = self._model.generate(**gen_kwargs)
            result = self._collect_audio_locked(
                results,
                text,
                started,
                turn_id,
                cancel,
            )

            if output_path:
                self._save_wav(result.audio, result.sample_rate, output_path)
            return result

    def synthesize(
        self,
        text: str,
        profile: VoiceProfile | None = None,
        turn_id: str | None = None,
        cancel: CancelInput = None,
    ) -> TTSResult:
        """Provider-boundary name; ``generate`` remains for v3 gateway compatibility."""

        return self.generate(
            text,
            profile=profile,
            turn_id=turn_id,
            cancel=cancel,
        )

    def generate_to_pcm16(
        self,
        text: str,
        *,
        profile: VoiceProfile | None = None,
        turn_id: str | None = None,
        cancel: CancelInput = None,
    ) -> tuple[bytes, int]:
        """Generate audio and return normalized audio as signed PCM16 bytes."""

        result = self.synthesize(
            text,
            profile=profile,
            turn_id=turn_id,
            cancel=cancel,
        )
        audio_int16 = np.clip(result.audio * 32767.0, -32768, 32767).astype(np.int16)
        return audio_int16.tobytes(), result.sample_rate

    @staticmethod
    def _save_wav(audio: np.ndarray, sample_rate: int, path: str) -> None:
        from scipy.io import wavfile

        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        audio_int16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        wavfile.write(str(output), sample_rate, audio_int16)
        logger.info("Saved audio to %s", output)

    def unload(self) -> None:
        """Release the model and its tokenizer-derived reference cache."""

        with self._model_lock:
            self._model = None
            self._ref_tokens = None
            self._last_error = None
            logger.info("OmniVoice model unloaded")
