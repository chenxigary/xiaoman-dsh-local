"""Energy-based Voice Activity Detection for xiaoman-v3.

The VAD is deliberately small and deterministic because it runs in the audio
capture path on the Mac.  ``process_chunk`` accepts arbitrary-sized float32
PCM chunks and analyzes them in ``frame_ms`` frames; callers therefore do not
have to produce exactly 30 ms packets.

There are two endpoint behaviours borrowed from the original voice project:

* ``short_segment_merge_ms`` holds a valid-looking but too-short fragment and
  stitches it to the next fragment when the audio-clock gap is within the
  configured window.  Fragments shorter than 100 ms of active speech are
  treated as noise and are never held.
* ``speculative_reopen_ms`` makes the normal ``min_silence_ms`` endpoint a
  *soft* endpoint.  A new speech fragment that reaches
  ``min_speech_continuation_ms`` inside the window reopens the same buffered
  utterance.  If no such fragment arrives, the utterance is committed once
  the window expires.

The existing 1200 ms endpoint meaning is retained: after 1200 ms of
continuous silence ``is_speaking`` becomes false and the turn is soft-ended.
With the default 2500 ms reopen grace, ``process_chunk`` returns the utterance
after at most approximately 3700 ms of streamed silence (plus one input
frame).  Set ``speculative_reopen_ms=0`` for the old immediate-return
behaviour.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
from typing import Optional

import numpy as np


logger = logging.getLogger(__name__)

# A tiny fragment is much more likely to be a click/noise burst than a useful
# speech segment.  This mirrors the upstream implementation's 100 ms floor.
_MIN_MERGEABLE_SPEECH_MS = 100


@dataclass
class VADConfig:
    """Configuration for :class:`VADState`.

    Durations are measured on the received audio clock, not by counting calls
    to ``process_chunk``.  This keeps the API compatible with callers that
    send 30 ms frames while also behaving correctly for larger/smaller chunks.
    """

    sample_rate: int = 16000
    # Energy threshold (absolute RMS floor).  The adaptive threshold also
    # follows the loudest RMS observed in the current session.
    energy_threshold: float = 0.015
    # Minimum active speech duration to start/accept a new utterance.
    min_speech_ms: int = 300
    # Existing endpoint semantics: continuous silence of this length
    # soft-ends an active utterance.
    min_silence_ms: int = 1200
    # Audio retained before a confirmed speech start.
    speech_pad_ms: int = 200
    # Analysis frame size.  Input chunks are split as needed.
    frame_ms: int = 30
    # Hysteresis for speech that resumes inside a speculative reopen window.
    # 192 ms is the value used by the original start-voice configuration.
    # Set to 0 to require min_speech_ms for a resumed fragment.
    min_speech_continuation_ms: int = 192
    # Hold adjacent sub-minimum segments for this long so they can be joined.
    # Set to 0 to disable this addition and retain the old discard behaviour.
    short_segment_merge_ms: int = 800
    # Delay committing a soft endpoint so a resumed utterance can reopen it.
    # Set to 0 to return immediately at min_silence_ms (legacy behaviour).
    speculative_reopen_ms: int = 2500
    # Safety cap.  A continuous microphone stream must never make the VAD
    # retain an unbounded utterance.  A cap hit forces a segment boundary.
    max_buffered_ms: int = 30000


@dataclass
class _PendingShortSegment:
    """A sub-minimum segment held for possible short-segment stitching."""

    audio: np.ndarray
    start_sample: int
    end_sample: int
    active_speech_samples: int


class VADState:
    """Stateful energy VAD with bounded buffering and reopenable endpoints.

    ``process_chunk`` returns at most one completed utterance per call.  If a
    very large input contains several completed utterances, additional ones
    are queued internally and returned by subsequent calls.  The queue is
    output-only; active audio remains bounded by ``max_buffered_ms``.
    """

    def __init__(self, config: Optional[VADConfig] = None):
        self.config = config or VADConfig()
        self._validate_config(self.config)

        self._samples_per_frame = max(
            1, int(round(self.config.sample_rate * self.config.frame_ms / 1000))
        )
        self._min_speech_samples = self._ms_to_samples(self.config.min_speech_ms)
        self._min_silence_samples = self._ms_to_samples(self.config.min_silence_ms)
        self._speech_pad_samples = self._ms_to_samples(self.config.speech_pad_ms)
        self._short_merge_samples = self._ms_to_samples(
            self.config.short_segment_merge_ms
        )
        self._speculative_reopen_samples = self._ms_to_samples(
            self.config.speculative_reopen_ms
        )
        self._max_buffered_samples = self._ms_to_samples(self.config.max_buffered_ms)

        continuation_ms = self.config.min_speech_continuation_ms
        if continuation_ms <= 0:
            continuation_ms = self.config.min_speech_ms
        continuation_ms = min(
            self.config.min_speech_ms,
            max(_MIN_MERGEABLE_SPEECH_MS, continuation_ms),
        )
        self._min_continuation_samples = self._ms_to_samples(continuation_ms)

        self._ready: deque[np.ndarray] = deque()
        self._events: deque[str] = deque()
        self._max_rms = 0.01
        self._total_samples = 0
        self._reset_segment_state(clear_pending_short=True)

    @staticmethod
    def _validate_config(config: VADConfig) -> None:
        if config.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if config.frame_ms <= 0:
            raise ValueError("frame_ms must be greater than 0")
        if config.energy_threshold < 0:
            raise ValueError("energy_threshold must be non-negative")
        for name in (
            "min_speech_ms",
            "min_silence_ms",
            "speech_pad_ms",
            "min_speech_continuation_ms",
            "short_segment_merge_ms",
            "speculative_reopen_ms",
        ):
            if getattr(config, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if config.max_buffered_ms <= 0:
            raise ValueError("max_buffered_ms must be greater than 0")

    def _ms_to_samples(self, duration_ms: int) -> int:
        return int(round(self.config.sample_rate * duration_ms / 1000))

    @staticmethod
    def _empty_audio() -> np.ndarray:
        return np.empty(0, dtype=np.float32)

    @staticmethod
    def _concat(parts: list[np.ndarray] | tuple[np.ndarray, ...]) -> np.ndarray:
        non_empty = [part for part in parts if len(part)]
        if not non_empty:
            return np.empty(0, dtype=np.float32)
        if len(non_empty) == 1:
            return np.asarray(non_empty[0], dtype=np.float32).copy()
        return np.concatenate(non_empty).astype(np.float32, copy=False)

    def _reset_segment_state(self, *, clear_pending_short: bool) -> None:
        """Clear utterance state without resetting the session audio clock."""

        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        self._is_speaking = False
        self._active_start_sample: int | None = None
        self._active_speech_samples = 0
        self._silence_samples = 0

        self._pre_buffer: deque[np.ndarray] = deque()
        self._pre_buffer_samples = 0

        self._candidate_audio: list[np.ndarray] = []
        self._candidate_pre_roll = self._empty_audio()
        self._candidate_start_sample: int | None = None
        self._candidate_active_samples = 0
        self._candidate_silence_samples = 0

        self._soft_end = False
        self._soft_end_sample: int | None = None
        self._reopen_audio: list[np.ndarray] = []
        self._reopen_active_samples = 0

        if clear_pending_short:
            self._pending_short: _PendingShortSegment | None = None

    def reset(self):
        """Reset all state, including pending segments and queued output."""

        self._ready.clear()
        self._events.clear()
        self._total_samples = 0
        self._max_rms = 0.01
        self._reset_segment_state(clear_pending_short=True)

    @property
    def is_speaking(self) -> bool:
        """Whether a speech segment is currently confirmed and active.

        A soft-ended segment is intentionally reported as not speaking while
        it remains reopenable.  This matches the endpoint signal while the
        buffered audio is still retained internally.
        """

        return self._is_speaking

    def _append_pre_buffer(self, frame: np.ndarray) -> None:
        if self._speech_pad_samples <= 0:
            return
        stored = np.asarray(frame, dtype=np.float32).copy()
        self._pre_buffer.append(stored)
        self._pre_buffer_samples += len(stored)
        while self._pre_buffer and self._pre_buffer_samples > self._speech_pad_samples:
            removed = self._pre_buffer.popleft()
            self._pre_buffer_samples -= len(removed)

    def _clear_candidate(self) -> None:
        self._candidate_audio.clear()
        self._candidate_pre_roll = self._empty_audio()
        self._candidate_start_sample = None
        self._candidate_active_samples = 0
        self._candidate_silence_samples = 0

    def _candidate_segment(self) -> tuple[np.ndarray, int, int]:
        if self._candidate_start_sample is None:
            return self._empty_audio(), self._total_samples, self._total_samples
        segment = self._concat([self._candidate_pre_roll, *self._candidate_audio])
        start = self._candidate_start_sample - len(self._candidate_pre_roll)
        return segment, start, start + len(segment)

    def _start_candidate(self, frame: np.ndarray, frame_start: int) -> None:
        self._candidate_pre_roll = self._concat(list(self._pre_buffer))
        self._candidate_start_sample = frame_start
        self._candidate_audio = [np.asarray(frame, dtype=np.float32).copy()]
        self._candidate_active_samples = len(frame)
        self._candidate_silence_samples = 0

    def _append_candidate_voice(self, frame: np.ndarray) -> None:
        self._candidate_audio.append(np.asarray(frame, dtype=np.float32).copy())
        self._candidate_active_samples += len(frame)
        self._candidate_silence_samples = 0

    def _append_candidate_silence(self, frame: np.ndarray) -> None:
        self._candidate_audio.append(np.asarray(frame, dtype=np.float32).copy())
        self._candidate_silence_samples += len(frame)

    def _can_merge_pending(self, segment_start: int) -> bool:
        if self._pending_short is None or self._short_merge_samples <= 0:
            return False
        gap = max(0, segment_start - self._pending_short.end_sample)
        return gap <= self._short_merge_samples

    def _stitch_pending(
        self,
        segment: np.ndarray,
        segment_start: int,
        segment_end: int,
        active_speech_samples: int,
    ) -> tuple[np.ndarray, int, int, int] | None:
        """Return a stitched segment, trimming pre-roll overlap if needed."""

        pending = self._pending_short
        if pending is None or not self._can_merge_pending(segment_start):
            return None

        if segment_start < pending.end_sample:
            trim = min(len(segment), pending.end_sample - segment_start)
            segment = segment[trim:]
            segment_start = pending.end_sample
        gap_samples = max(0, segment_start - pending.end_sample)
        parts = [
            pending.audio,
            np.zeros(gap_samples, dtype=np.float32),
            segment,
        ]
        merged = self._concat(parts)
        merged_start = pending.start_sample
        merged_end = max(segment_end, pending.end_sample + gap_samples + len(segment))
        merged_active = pending.active_speech_samples + active_speech_samples
        self._pending_short = None
        return merged, merged_start, merged_end, merged_active

    def _start_active_segment(
        self,
        segment: np.ndarray,
        start_sample: int,
        active_speech_samples: int,
    ) -> None:
        self._buffer = [np.asarray(segment, dtype=np.float32).copy()]
        self._buffered_samples = len(segment)
        self._active_start_sample = start_sample
        self._active_speech_samples = active_speech_samples
        self._silence_samples = 0
        self._is_speaking = True
        self._soft_end = False
        self._soft_end_sample = None
        self._reopen_audio.clear()
        self._reopen_active_samples = 0
        self._events.append("start")
        self._clear_candidate()
        self._pre_buffer.clear()
        self._pre_buffer_samples = 0

    def _queue_current_buffer(self) -> None:
        if self._buffered_samples:
            self._ready.append(self._concat(self._buffer))
            self._events.append("commit")
        self._reset_segment_state(clear_pending_short=True)

    def _hold_short_segment(
        self,
        segment: np.ndarray,
        start_sample: int,
        end_sample: int,
        active_speech_samples: int,
    ) -> None:
        """Hold a sub-minimum segment if it is useful enough to merge."""

        if (
            self._short_merge_samples <= 0
            or active_speech_samples
            < self._ms_to_samples(_MIN_MERGEABLE_SPEECH_MS)
        ):
            self._reset_segment_state(clear_pending_short=False)
            return

        self._pending_short = _PendingShortSegment(
            audio=np.asarray(segment, dtype=np.float32).copy(),
            start_sample=start_sample,
            end_sample=end_sample,
            active_speech_samples=active_speech_samples,
        )
        logger.debug(
            "VAD holding short segment: active=%dms, merge_window=%dms",
            round(active_speech_samples / self.config.sample_rate * 1000),
            self.config.short_segment_merge_ms,
        )
        self._reset_segment_state(clear_pending_short=False)

    def _discard_expired_pending(self, reference_sample: int) -> None:
        pending = self._pending_short
        if pending is None or self._short_merge_samples <= 0:
            return
        if reference_sample - pending.end_sample > self._short_merge_samples:
            logger.debug("VAD discarded expired short segment")
            self._pending_short = None

    def _soft_end_current(self, end_sample: int) -> None:
        """Mark the 1200 ms endpoint while retaining audio for reopen."""

        self._is_speaking = False
        self._soft_end = True
        self._soft_end_sample = end_sample
        self._reopen_audio.clear()
        self._reopen_active_samples = 0
        self._events.append("soft_end")
        if self._speculative_reopen_samples <= 0:
            self._queue_current_buffer()

    def _process_soft_end(self, frame: np.ndarray, frame_start: int, is_voice: bool) -> None:
        if self._soft_end_sample is None:
            self._queue_current_buffer()
            self._process_idle_frame(frame, frame_start, is_voice)
            return

        if (
            frame_start - self._soft_end_sample > self._speculative_reopen_samples
        ):
            # The current frame belongs after the old turn.  Commit first,
            # then feed the frame into the fresh idle state.
            self._queue_current_buffer()
            self._process_idle_frame(frame, frame_start, is_voice)
            return

        self._reopen_audio.append(np.asarray(frame, dtype=np.float32).copy())
        if is_voice:
            self._reopen_active_samples += len(frame)

        if self._reopen_active_samples >= self._min_continuation_samples:
            resumed = self._concat(self._reopen_audio)
            self._buffer.append(resumed)
            self._buffered_samples += len(resumed)
            self._active_speech_samples += self._reopen_active_samples
            self._is_speaking = True
            self._soft_end = False
            self._soft_end_sample = None
            self._silence_samples = 0
            self._reopen_audio.clear()
            self._reopen_active_samples = 0
            self._events.append("reopen")
            if self._buffered_samples >= self._max_buffered_samples:
                self._queue_current_buffer()

    def _finish_candidate(self) -> None:
        segment, segment_start, segment_end = self._candidate_segment()
        active_speech_samples = self._candidate_active_samples
        pending = self._pending_short
        stitched = self._stitch_pending(
            segment,
            segment_start,
            segment_end,
            active_speech_samples,
        )

        if stitched is not None:
            merged, merged_start, merged_end, merged_active = stitched
            if merged_active >= self._min_speech_samples:
                # The second short fragment just ended, so the merged result
                # is valid but still receives the normal speculative endpoint.
                self._start_active_segment(merged, merged_start, merged_active)
                self._soft_end_current(merged_end)
                return
            self._hold_short_segment(
                merged,
                merged_start,
                merged_end,
                merged_active,
            )
            return

        if pending is not None:
            # A fragment below the 100 ms floor cannot rescue the held one.
            self._reset_segment_state(clear_pending_short=False)
            return

        self._hold_short_segment(
            segment,
            segment_start,
            segment_end,
            active_speech_samples,
        )

    def _force_commit_candidate(self) -> None:
        """Apply the hard memory cap to an unusually long unconfirmed run."""

        segment, segment_start, _segment_end = self._candidate_segment()
        if self._pending_short is not None:
            stitched = self._stitch_pending(
                segment,
                segment_start,
                self._total_samples,
                self._candidate_active_samples,
            )
            if stitched is not None:
                segment, segment_start, _segment_end, active = stitched
            else:
                active = self._candidate_active_samples
        else:
            active = self._candidate_active_samples
        self._start_active_segment(segment, segment_start, active)
        self._queue_current_buffer()

    def _candidate_should_activate(self) -> bool:
        if self._pending_short is not None:
            if (
                self._candidate_active_samples
                >= self._ms_to_samples(_MIN_MERGEABLE_SPEECH_MS)
            ):
                return (
                    self._pending_short.active_speech_samples
                    + self._candidate_active_samples
                    >= self._min_speech_samples
                )
            return False
        return self._candidate_active_samples >= self._min_speech_samples

    def _candidate_hit_memory_cap(self) -> bool:
        segment, _start, _end = self._candidate_segment()
        return len(segment) >= self._max_buffered_samples

    def _activate_candidate(self) -> None:
        segment, segment_start, _segment_end = self._candidate_segment()
        stitched = self._stitch_pending(
            segment,
            segment_start,
            self._total_samples,
            self._candidate_active_samples,
        )
        if stitched is not None:
            segment, segment_start, _end, active = stitched
        else:
            active = self._candidate_active_samples
        self._start_active_segment(segment, segment_start, active)
        if self._buffered_samples >= self._max_buffered_samples:
            self._queue_current_buffer()

    def _process_idle_frame(self, frame: np.ndarray, frame_start: int, is_voice: bool) -> None:
        self._discard_expired_pending(frame_start)

        if self._candidate_start_sample is None:
            if is_voice:
                self._start_candidate(frame, frame_start)
                if self._candidate_should_activate():
                    self._activate_candidate()
                elif self._candidate_hit_memory_cap():
                    self._force_commit_candidate()
            else:
                self._append_pre_buffer(frame)
            return

        if is_voice:
            self._append_candidate_voice(frame)
            if self._candidate_should_activate():
                self._activate_candidate()
            elif self._candidate_hit_memory_cap():
                self._force_commit_candidate()
            return

        self._append_candidate_silence(frame)
        if self._candidate_silence_samples >= self._min_silence_samples:
            self._finish_candidate()
            return

        segment, _start, _end = self._candidate_segment()
        if len(segment) >= self._max_buffered_samples:
            self._force_commit_candidate()

    def _process_active_frame(self, frame: np.ndarray, is_voice: bool) -> None:
        stored = np.asarray(frame, dtype=np.float32).copy()
        self._buffer.append(stored)
        self._buffered_samples += len(stored)

        if is_voice:
            self._active_speech_samples += len(stored)
            self._silence_samples = 0
        else:
            self._silence_samples += len(stored)
            if self._silence_samples >= self._min_silence_samples:
                if self._active_speech_samples >= self._min_speech_samples:
                    self._soft_end_current(self._total_samples)
                else:
                    segment = self._concat(self._buffer)
                    self._hold_short_segment(
                        segment,
                        self._active_start_sample or 0,
                        self._total_samples,
                        self._active_speech_samples,
                    )
                return

        if self._buffered_samples >= self._max_buffered_samples:
            logger.debug("VAD forced endpoint at max_buffered_ms=%d", self.config.max_buffered_ms)
            self._queue_current_buffer()

    def _process_frame(self, frame: np.ndarray) -> None:
        frame_start = self._total_samples
        self._total_samples += len(frame)

        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        if rms > self._max_rms:
            self._max_rms = rms
        threshold = max(self.config.energy_threshold, self._max_rms * 0.1)
        is_voice = rms > threshold

        if self._soft_end:
            self._process_soft_end(frame, frame_start, is_voice)
        elif self._is_speaking:
            self._process_active_frame(frame, is_voice)
        else:
            self._process_idle_frame(frame, frame_start, is_voice)

    def process_chunk(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """Process float32 PCM and return one completed utterance if ready.

        ``audio`` may contain one capture packet or any number of packets.  It
        is split into analysis frames internally, and endpoint durations are
        computed from sample counts.  A zero-length chunk is a no-op except
        that it can drain an already-completed result.
        """

        array = np.asarray(audio, dtype=np.float32)
        if array.ndim != 1:
            raise ValueError("audio must be a one-dimensional PCM array")

        for start in range(0, len(array), self._samples_per_frame):
            self._process_frame(array[start : start + self._samples_per_frame])

        if self._ready:
            return self._ready.popleft()
        return None

    def get_buffered_audio(self) -> Optional[np.ndarray]:
        """Flush a completed/active turn, normally on disconnect.

        A confirmed active or soft-ended turn is returned and fully reset.  A
        sub-minimum pending short fragment is discarded, matching the normal
        merge-window expiry policy.  Queued completed output is returned
        first.
        """

        if self._ready:
            return self._ready.popleft()

        if self._is_speaking or self._soft_end:
            audio = self._concat(self._buffer)
            self.reset()
            return audio if len(audio) else None

        if self._pending_short is not None:
            self.reset()
        return None

    def snapshot_audio(self) -> Optional[np.ndarray]:
        """Copy the active/soft-ended turn without changing VAD state.

        The gateway uses this at the 1200 ms soft endpoint to start speculative
        ASR while retaining the same audio for a possible reopen/merge.
        """

        if not (self._is_speaking or self._soft_end):
            return None
        audio = self._concat([*self._buffer, *self._reopen_audio])
        return audio if len(audio) else None

    def take_events(self) -> list[str]:
        """Return and clear endpoint events since the previous call."""

        events = list(self._events)
        self._events.clear()
        return events
