"""Deterministic continuity and audio-to-mouth synchronization metrics.

The functions in this module intentionally have no media/runtime dependencies.
The live WebRTC probe records timestamped audio energy and mouth-region motion;
unit tests can feed the same analyzer synthetic traces.  A missing trace is
reported as ``incomplete`` rather than being mistaken for a pass.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass
import math
from statistics import fmean
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TimedValue:
    timestamp_ms: float
    value: float


@dataclass(frozen=True)
class AVQualityThresholds:
    max_audio_gap_ms: float = 100.0
    max_video_gap_ms: float = 200.0
    max_av_delivery_skew_p95_ms: float = 120.0
    max_abs_lip_lag_ms: float = 240.0
    min_lip_correlation: float = 0.12
    max_underflow_events: int = 0
    max_inserted_silence_ms: float = 0.0


def playback_idle_ready(
    *,
    is_speaking: object,
    seen_speaking: bool,
    continuity: dict[str, object] | None,
) -> bool:
    """Return true only after a complete logical turn has drained.

    LiveTalking's render-level ``is_speaking`` can flicker false between
    inference batches.  When continuity telemetry exists it is authoritative:
    the turn must be inactive and its queued audio must be empty.  Older
    runtimes without continuity telemetry retain the legacy speaking-edge
    fallback.
    """

    if not seen_speaking or is_speaking is True:
        return False
    if continuity is None:
        return True
    active = continuity.get("active")
    if active is True:
        return False
    if active is not None and active is not False:
        return False
    queued_audio_ms = continuity.get("queued_audio_ms")
    if isinstance(queued_audio_ms, (int, float)):
        return queued_audio_ms <= 0
    return True


def percentile(values: Sequence[float], q: float) -> float | None:
    """Return a linearly interpolated percentile without a NumPy dependency."""

    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return None
    if len(finite) == 1:
        return finite[0]
    rank = min(1.0, max(0.0, q)) * (len(finite) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return finite[lower]
    fraction = rank - lower
    return finite[lower] * (1.0 - fraction) + finite[upper] * fraction


def frame_gap_metrics(timestamps_ms: Iterable[float], stall_ms: float) -> dict[str, float | int | None]:
    timestamps = sorted(float(value) for value in timestamps_ms if math.isfinite(float(value)))
    spans = [
        (later - earlier, earlier, later)
        for earlier, later in zip(timestamps, timestamps[1:])
        if later >= earlier
    ]
    gaps = [span[0] for span in spans]
    largest = max(spans, default=None)
    return {
        "frames": len(timestamps),
        "gap_p95_ms": percentile(gaps, 0.95),
        "gap_max_ms": None if largest is None else largest[0],
        "gap_max_start_ms": None if largest is None else largest[1],
        "gap_max_end_ms": None if largest is None else largest[2],
        "stall_events": sum(gap > stall_ms for gap in gaps),
    }


def paired_clock_gap_metrics(
    arrival_timestamps_ms: Sequence[float],
    media_timestamps_ms: Sequence[float],
) -> dict[str, float | int | None]:
    """Compare receiver arrival gaps with the decoded frame's media clock.

    A large arrival gap paired with a normal media-PTS increment means frames
    were delayed after their media timeline was produced.  Absolute arrival
    bounds use ``time.monotonic_ns`` and can be compared directly with the
    sender telemetry emitted by the local LiveTalking process.
    """

    count = min(len(arrival_timestamps_ms), len(media_timestamps_ms))
    spans: list[tuple[float, float, float, float, float]] = []
    for index in range(1, count):
        arrival_start = float(arrival_timestamps_ms[index - 1])
        arrival_end = float(arrival_timestamps_ms[index])
        media_start = float(media_timestamps_ms[index - 1])
        media_end = float(media_timestamps_ms[index])
        arrival_gap = arrival_end - arrival_start
        media_gap = media_end - media_start
        if arrival_gap < 0 or media_gap < 0:
            continue
        spans.append(
            (arrival_gap, media_gap, arrival_gap - media_gap, arrival_start, arrival_end)
        )
    largest_arrival = max(spans, key=lambda item: item[0], default=None)
    largest_excess = max(spans, key=lambda item: item[2], default=None)
    return {
        "frames": count,
        "arrival_gap_max_ms": None if largest_arrival is None else largest_arrival[0],
        "media_gap_at_arrival_max_ms": (
            None if largest_arrival is None else largest_arrival[1]
        ),
        "excess_gap_at_arrival_max_ms": (
            None if largest_arrival is None else largest_arrival[2]
        ),
        "arrival_gap_max_start_monotonic_ms": (
            None if largest_arrival is None else largest_arrival[3]
        ),
        "arrival_gap_max_end_monotonic_ms": (
            None if largest_arrival is None else largest_arrival[4]
        ),
        "media_gap_max_ms": max((item[1] for item in spans), default=None),
        "excess_gap_max_ms": None if largest_excess is None else largest_excess[2],
    }


def nearest_skew_metrics(
    reference_ms: Iterable[float],
    comparison_ms: Iterable[float],
) -> dict[str, float | int | None]:
    """Measure distance from each reference frame to its nearest peer frame."""

    reference = sorted(float(value) for value in reference_ms if math.isfinite(float(value)))
    comparison = sorted(float(value) for value in comparison_ms if math.isfinite(float(value)))
    if not reference or not comparison:
        return {"samples": 0, "abs_p50_ms": None, "abs_p95_ms": None, "abs_max_ms": None}
    skews: list[float] = []
    for value in reference:
        index = bisect_left(comparison, value)
        candidates = comparison[max(0, index - 1) : min(len(comparison), index + 1)]
        skews.append(min(abs(peer - value) for peer in candidates))
    return {
        "samples": len(skews),
        "abs_p50_ms": percentile(skews, 0.50),
        "abs_p95_ms": percentile(skews, 0.95),
        "abs_max_ms": max(skews),
    }


def _interpolate(samples: Sequence[TimedValue], timestamp_ms: float) -> float | None:
    if not samples or timestamp_ms < samples[0].timestamp_ms or timestamp_ms > samples[-1].timestamp_ms:
        return None
    times = [item.timestamp_ms for item in samples]
    index = bisect_left(times, timestamp_ms)
    if index == 0:
        return samples[0].value
    if index == len(samples):
        return samples[-1].value
    right = samples[index]
    left = samples[index - 1]
    width = right.timestamp_ms - left.timestamp_ms
    if width <= 0:
        return right.value
    fraction = (timestamp_ms - left.timestamp_ms) / width
    return left.value + (right.value - left.value) * fraction


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 8:
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_power = sum((a - left_mean) ** 2 for a in left)
    right_power = sum((b - right_mean) ** 2 for b in right)
    denominator = math.sqrt(left_power * right_power)
    return numerator / denominator if denominator > 1e-12 else None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def estimate_lip_onset(
    audio_envelope: Sequence[TimedValue],
    mouth_motion: Sequence[TimedValue],
    *,
    audio_active_threshold: float,
) -> dict[str, float | int | None | bool | str]:
    """Measure sustained mouth-motion onset relative to received speech.

    The motion threshold is learned from the pre-speech baseline, so idle head
    movement does not become a false lip onset.  Positive offsets mean the
    visible mouth follows audio; negative offsets mean it leads.
    """

    active_audio = [
        item
        for item in sorted(audio_envelope, key=lambda sample: sample.timestamp_ms)
        if item.value >= audio_active_threshold
    ]
    motion = sorted(mouth_motion, key=lambda item: item.timestamp_ms)
    if not active_audio:
        return {
            "available": False,
            "reason": "no active speech audio samples",
            "onset_offset_ms": None,
        }
    if len(motion) < 5:
        return {
            "available": False,
            "reason": "not enough mouth-motion samples",
            "onset_offset_ms": None,
        }

    audio_onset_ms = active_audio[0].timestamp_ms
    baseline = [
        item.value
        for item in motion
        if max(0.0, audio_onset_ms - 2000.0)
        <= item.timestamp_ms
        <= audio_onset_ms - 80.0
    ]
    if len(baseline) < 4:
        return {
            "available": False,
            "reason": "not enough pre-speech mouth-motion baseline",
            "audio_onset_ms": audio_onset_ms,
            "onset_offset_ms": None,
        }
    baseline_median = _median(baseline)
    baseline_mad = _median([abs(value - baseline_median) for value in baseline])
    motion_threshold = baseline_median + max(0.35, baseline_mad * 5.0)
    candidates = [
        item
        for item in motion
        if audio_onset_ms - 400.0 <= item.timestamp_ms <= audio_onset_ms + 1200.0
    ]
    mouth_onset_ms: float | None = None
    for index in range(max(0, len(candidates) - 2)):
        window = candidates[index : index + 3]
        if sum(item.value >= motion_threshold for item in window) >= 2:
            mouth_onset_ms = window[0].timestamp_ms
            break
    return {
        "available": mouth_onset_ms is not None,
        "reason": None if mouth_onset_ms is not None else "no sustained mouth activity near speech onset",
        "audio_onset_ms": audio_onset_ms,
        "mouth_onset_ms": mouth_onset_ms,
        "onset_offset_ms": (
            None if mouth_onset_ms is None else mouth_onset_ms - audio_onset_ms
        ),
        "motion_threshold": motion_threshold,
        "baseline_median": baseline_median,
        "baseline_mad": baseline_mad,
    }


def estimate_lip_lag(
    audio_envelope: Sequence[TimedValue],
    mouth_motion: Sequence[TimedValue],
    *,
    max_lag_ms: int = 1000,
    step_ms: int = 20,
) -> dict[str, float | int | None]:
    """Cross-correlate audio energy with mouth motion.

    Positive ``lag_ms`` means the visual mouth response follows the audio.
    Both input series must use the same capture clock.  The search samples
    ``audio(t)`` against ``mouth(t + lag)`` over the shared interval.
    """

    audio = sorted(audio_envelope, key=lambda item: item.timestamp_ms)
    mouth = sorted(mouth_motion, key=lambda item: item.timestamp_ms)
    if len(audio) < 8 or len(mouth) < 8 or step_ms <= 0:
        return {"lag_ms": None, "correlation": None, "samples": 0}

    best: tuple[float, int, int] | None = None
    for lag in range(-max_lag_ms, max_lag_ms + 1, step_ms):
        start = max(audio[0].timestamp_ms, mouth[0].timestamp_ms - lag)
        stop = min(audio[-1].timestamp_ms, mouth[-1].timestamp_ms - lag)
        if stop - start < step_ms * 8:
            continue
        left: list[float] = []
        right: list[float] = []
        cursor = start
        while cursor <= stop:
            audio_value = _interpolate(audio, cursor)
            mouth_value = _interpolate(mouth, cursor + lag)
            if audio_value is not None and mouth_value is not None:
                left.append(audio_value)
                right.append(mouth_value)
            cursor += step_ms
        correlation = _correlation(left, right)
        if correlation is None:
            continue
        candidate = (correlation, -abs(lag), lag)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return {"lag_ms": None, "correlation": None, "samples": 0}

    lag = best[2]
    start = max(audio[0].timestamp_ms, mouth[0].timestamp_ms - lag)
    stop = min(audio[-1].timestamp_ms, mouth[-1].timestamp_ms - lag)
    return {
        "lag_ms": float(lag),
        "correlation": float(best[0]),
        "samples": max(0, math.floor((stop - start) / step_ms) + 1),
    }


def evaluate_av_quality(
    *,
    audio_frame_times_ms: Sequence[float],
    video_frame_times_ms: Sequence[float],
    audio_envelope: Sequence[TimedValue],
    mouth_motion: Sequence[TimedValue],
    continuity: dict[str, object] | None,
    thresholds: AVQualityThresholds | None = None,
    audio_active_threshold: float | None = None,
) -> dict[str, object]:
    """Return a machine-readable pass/fail/incomplete A/V quality report."""

    limits = thresholds or AVQualityThresholds()
    audio_gaps = frame_gap_metrics(audio_frame_times_ms, limits.max_audio_gap_ms)
    video_gaps = frame_gap_metrics(video_frame_times_ms, limits.max_video_gap_ms)
    av_skew = nearest_skew_metrics(audio_frame_times_ms, video_frame_times_ms)
    lip_sync: dict[str, object] = dict(estimate_lip_lag(audio_envelope, mouth_motion))
    if audio_active_threshold is not None:
        lip_sync.update(
            estimate_lip_onset(
                audio_envelope,
                mouth_motion,
                audio_active_threshold=audio_active_threshold,
            )
        )
    checks: list[dict[str, object]] = []

    def maximum_check(name: str, value: float | int | None, limit: float | int, unit: str) -> None:
        status = "incomplete" if value is None else ("pass" if value <= limit else "fail")
        checks.append({"name": name, "status": status, "value": value, "limit": limit, "unit": unit})

    maximum_check("audio_gap_max", audio_gaps["gap_max_ms"], limits.max_audio_gap_ms, "ms")
    maximum_check("video_gap_max", video_gaps["gap_max_ms"], limits.max_video_gap_ms, "ms")
    maximum_check("av_delivery_skew_p95", av_skew["abs_p95_ms"], limits.max_av_delivery_skew_p95_ms, "ms")
    if audio_active_threshold is not None:
        onset_offset = lip_sync.get("onset_offset_ms")
        onset_value = None if onset_offset is None else abs(float(onset_offset))
        if onset_value is not None:
            onset_status = "pass" if onset_value <= limits.max_abs_lip_lag_ms else "fail"
        elif lip_sync.get("reason") == "no sustained mouth activity near speech onset":
            # The capture contains active speech and enough video samples, so
            # an unmoving mouth is a measured quality regression, not missing
            # infrastructure/evidence.
            onset_status = "fail"
        else:
            onset_status = "incomplete"
        checks.append({
            "name": "lip_onset_offset_abs",
            "status": onset_status,
            "value": onset_value,
            "limit": limits.max_abs_lip_lag_ms,
            "unit": "ms",
            "reason": lip_sync.get("reason"),
        })
    correlation = lip_sync["correlation"]
    checks.append({
        "name": "lip_correlation",
        "status": "incomplete" if correlation is None else ("pass" if correlation >= limits.min_lip_correlation else "fail"),
        "value": correlation,
        "limit": limits.min_lip_correlation,
        "unit": "pearson_r_min",
    })
    lip_lag = lip_sync["lag_ms"]
    if audio_active_threshold is None:
        maximum_check(
            "lip_lag_abs",
            None if lip_lag is None else abs(float(lip_lag)),
            limits.max_abs_lip_lag_ms,
            "ms",
        )
    else:
        checks.append({
            "name": "lip_lag_abs",
            "status": "pass",
            "value": None if lip_lag is None else abs(float(lip_lag)),
            "limit": limits.max_abs_lip_lag_ms,
            "unit": "ms",
            "required": False,
            "note": (
                "diagnostic only for live speech: audio energy and frame-to-frame "
                "mouth motion do not encode the same phoneme timeline"
            ),
        })

    underflows = continuity.get("underflow_events") if continuity else None
    inserted = continuity.get("inserted_silence_ms") if continuity else None
    maximum_check(
        "avatar_underflow_events",
        underflows if isinstance(underflows, (int, float)) else None,
        limits.max_underflow_events,
        "events",
    )
    maximum_check(
        "avatar_inserted_silence",
        inserted if isinstance(inserted, (int, float)) else None,
        limits.max_inserted_silence_ms,
        "ms",
    )

    statuses = {str(check["status"]) for check in checks}
    status = "fail" if "fail" in statuses else ("incomplete" if "incomplete" in statuses else "pass")
    return {
        "schema": "xiaoman.av-quality/v1",
        "status": status,
        "thresholds": asdict(limits),
        "metrics": {
            "audio": audio_gaps,
            "video": video_gaps,
            "av_delivery_skew": av_skew,
            "lip_sync": lip_sync,
            "continuity": continuity,
        },
        "checks": checks,
    }


__all__ = [
    "AVQualityThresholds",
    "TimedValue",
    "estimate_lip_onset",
    "estimate_lip_lag",
    "evaluate_av_quality",
    "frame_gap_metrics",
    "nearest_skew_metrics",
    "paired_clock_gap_metrics",
    "percentile",
    "playback_idle_ready",
]
