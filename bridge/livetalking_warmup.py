"""Per-session Wav2Lip warm-up for the DSH LiveTalking overlay.

LiveTalking warms the shared model while the Avatar server starts. On a
16-GB unified-memory machine, loading llama.cpp, TTS, and ASR afterwards can
page those weights out again. The first speaking frame would then pay the
page-in cost after WebRTC media had already started, freezing video while the
audio continuity queue kept playing.

This overlay moves one representative Wav2Lip inference into session
construction. A cold page-in may delay the HTTP offer, but no media clock has
started yet, so the user never sees a mid-stream Avatar freeze.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable


logger = logging.getLogger(__name__)
_WARMUP_LOCK = threading.Lock()


def _runtime_info(message: str, *args: Any) -> None:
    """Use LiveTalking's configured logger when running inside its process."""

    try:
        from utils.logger import logger as livetalking_logger  # type: ignore
    except ImportError:
        livetalking_logger = logger
    livetalking_logger.info(message, *args)


def warm_wav2lip_session(
    avatar_session: Any,
    *,
    numpy_module: Any | None = None,
) -> float | None:
    """Run one representative inference and return its elapsed milliseconds.

    Non-Wav2Lip sessions are left untouched. The real ``inference_batch``
    path is intentional: it exercises image preparation, MPS model execution,
    and the device-to-CPU copy that the first speaking frame needs.
    """

    opt = getattr(avatar_session, "opt", None)
    if str(getattr(opt, "model", "")).strip().lower() != "wav2lip":
        return None
    inference_batch = getattr(avatar_session, "inference_batch", None)
    if not callable(inference_batch):
        return None

    if numpy_module is None:
        import numpy as numpy_module

    batch_size = max(1, int(getattr(avatar_session, "batch_size", 1)))
    mel = numpy_module.ones(
        (batch_size, 80, 16),
        dtype=numpy_module.float32,
    )
    started = time.perf_counter()
    with _WARMUP_LOCK:
        inference_batch(0, mel)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    avatar_session._xiaoman_session_warmup_ms = elapsed_ms
    _runtime_info(
        "Wav2Lip session warm-up completed before media start in %.1fms",
        elapsed_ms,
    )
    return elapsed_ms


def install_session_warmup(
    session_manager_class: Any,
    *,
    warmup_fn: Callable[[Any], float | None] = warm_wav2lip_session,
) -> bool:
    """Wrap ``SessionManager.init_builder`` once, preserving its public API."""

    if getattr(session_manager_class, "_xiaoman_session_warmup_installed", False):
        return False

    original_init_builder = session_manager_class.init_builder

    def patched_init_builder(self: Any, build_session_fn: Callable[..., Any]) -> Any:
        def warmed_builder(sessionid: str, params: dict[str, Any]) -> Any:
            avatar_session = build_session_fn(sessionid, params)
            try:
                warmup_fn(avatar_session)
            except Exception:
                # Warm-up is a latency optimization, not a new availability
                # dependency. Preserve LiveTalking's original failure point
                # and error handling if a backend cannot be primed here.
                logger.exception("Wav2Lip session warm-up failed; continuing")
            return avatar_session

        return original_init_builder(self, warmed_builder)

    session_manager_class.init_builder = patched_init_builder
    session_manager_class._xiaoman_session_warmup_installed = True
    return True


__all__ = ["install_session_warmup", "warm_wav2lip_session"]
