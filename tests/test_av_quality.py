import math
import unittest

from bridge.av_quality import (
    AVQualityThresholds,
    TimedValue,
    estimate_lip_lag,
    estimate_lip_onset,
    evaluate_av_quality,
    frame_gap_metrics,
    nearest_skew_metrics,
    paired_clock_gap_metrics,
    playback_idle_ready,
)


class AVQualityMetricsTest(unittest.TestCase):
    def test_playback_idle_rejects_render_flicker_during_active_turn(self) -> None:
        self.assertFalse(playback_idle_ready(
            is_speaking=False,
            seen_speaking=True,
            continuity={"active": True, "queued_audio_ms": 4460},
        ))

    def test_playback_idle_requires_inactive_empty_continuity_queue(self) -> None:
        self.assertFalse(playback_idle_ready(
            is_speaking=False,
            seen_speaking=True,
            continuity={"active": False, "queued_audio_ms": 40},
        ))
        self.assertTrue(playback_idle_ready(
            is_speaking=False,
            seen_speaking=True,
            continuity={"active": False, "queued_audio_ms": 0},
        ))

    def test_playback_idle_keeps_legacy_speaking_edge_fallback(self) -> None:
        self.assertTrue(playback_idle_ready(
            is_speaking=False,
            seen_speaking=True,
            continuity=None,
        ))

    def test_frame_gaps_count_real_stalls(self) -> None:
        metrics = frame_gap_metrics([0, 20, 40, 60, 320, 340], stall_ms=80)
        self.assertEqual(metrics["frames"], 6)
        self.assertEqual(metrics["gap_max_ms"], 260)
        self.assertEqual(metrics["gap_max_start_ms"], 60)
        self.assertEqual(metrics["gap_max_end_ms"], 320)
        self.assertEqual(metrics["stall_events"], 1)

    def test_nearest_av_skew_uses_paired_capture_clock(self) -> None:
        metrics = nearest_skew_metrics([10, 30, 50, 70], [0, 40, 80])
        self.assertEqual(metrics["samples"], 4)
        self.assertEqual(metrics["abs_max_ms"], 10)
        self.assertEqual(metrics["abs_p95_ms"], 10)

    def test_paired_clock_gap_separates_arrival_stall_from_media_pts(self) -> None:
        metrics = paired_clock_gap_metrics(
            [1000.0, 1020.0, 1220.0, 1240.0],
            [0.0, 20.0, 40.0, 60.0],
        )

        self.assertEqual(metrics["frames"], 4)
        self.assertEqual(metrics["arrival_gap_max_ms"], 200)
        self.assertEqual(metrics["media_gap_at_arrival_max_ms"], 20)
        self.assertEqual(metrics["excess_gap_at_arrival_max_ms"], 180)
        self.assertEqual(metrics["arrival_gap_max_start_monotonic_ms"], 1020)
        self.assertEqual(metrics["arrival_gap_max_end_monotonic_ms"], 1220)
        self.assertEqual(metrics["media_gap_max_ms"], 20)

    def test_lip_lag_recovers_delayed_visual_response(self) -> None:
        # Rich, deterministic envelope avoids an ambiguous periodic maximum.
        audio = [
            TimedValue(float(time_ms), 1.0 + math.sin(time_ms / 91) + 0.4 * math.sin(time_ms / 37))
            for time_ms in range(0, 4000, 20)
        ]
        mouth = [TimedValue(item.timestamp_ms + 140, item.value * 2.0 + 3.0) for item in audio]
        result = estimate_lip_lag(audio, mouth, max_lag_ms=300, step_ms=20)
        self.assertEqual(result["lag_ms"], 140)
        self.assertGreater(float(result["correlation"]), 0.99)

    def test_lip_onset_uses_pre_speech_motion_baseline(self) -> None:
        audio = [
            TimedValue(float(time_ms), 600.0 if 1000 <= time_ms < 2400 else 0.0)
            for time_ms in range(0, 3000, 20)
        ]
        mouth = [
            TimedValue(float(time_ms), 1.8 if 1140 <= time_ms < 2540 else 0.05)
            for time_ms in range(0, 3000, 40)
        ]
        result = estimate_lip_onset(audio, mouth, audio_active_threshold=200.0)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(float(result["onset_offset_ms"]), 120.0, delta=40.0)

    def test_lip_onset_without_visible_motion_is_incomplete(self) -> None:
        audio = [
            TimedValue(float(time_ms), 600.0 if time_ms >= 1000 else 0.0)
            for time_ms in range(0, 2400, 20)
        ]
        mouth = [TimedValue(float(time_ms), 0.05) for time_ms in range(0, 2400, 40)]
        result = estimate_lip_onset(audio, mouth, audio_active_threshold=200.0)
        self.assertFalse(result["available"])
        self.assertIsNone(result["onset_offset_ms"])

    def test_live_quality_report_fails_when_speech_has_no_visible_mouth_response(self) -> None:
        audio_times = [float(value) for value in range(0, 3000, 20)]
        video_times = [float(value) for value in range(0, 3000, 40)]
        envelope = [
            TimedValue(value, 600.0 if value >= 1000.0 else 0.0)
            for value in audio_times
        ]
        motion = [TimedValue(value, 0.05) for value in video_times]
        report = evaluate_av_quality(
            audio_frame_times_ms=audio_times,
            video_frame_times_ms=video_times,
            audio_envelope=envelope,
            mouth_motion=motion,
            continuity={"underflow_events": 0, "inserted_silence_ms": 0},
            audio_active_threshold=200.0,
        )
        self.assertEqual(report["status"], "fail")
        onset_check = next(
            item for item in report["checks"]
            if item["name"] == "lip_onset_offset_abs"
        )
        self.assertEqual(onset_check["status"], "fail")
        self.assertEqual(
            onset_check["reason"],
            "no sustained mouth activity near speech onset",
        )

    def test_quality_report_fails_stall_lag_and_avatar_underflow(self) -> None:
        audio_times = [float(value) for value in range(0, 1000, 20)]
        video_times = [float(value) for value in range(0, 400, 40)] + [700.0, 740.0, 780.0]
        envelope = [TimedValue(value, math.sin(value / 73)) for value in audio_times]
        motion = [TimedValue(item.timestamp_ms + 300, item.value) for item in envelope]
        report = evaluate_av_quality(
            audio_frame_times_ms=audio_times,
            video_frame_times_ms=video_times,
            audio_envelope=envelope,
            mouth_motion=motion,
            continuity={"underflow_events": 1, "inserted_silence_ms": 80},
            thresholds=AVQualityThresholds(max_abs_lip_lag_ms=200),
        )
        self.assertEqual(report["status"], "fail")
        failed = {item["name"] for item in report["checks"] if item["status"] == "fail"}
        self.assertTrue({"video_gap_max", "lip_lag_abs", "avatar_underflow_events", "avatar_inserted_silence"} <= failed)

    def test_missing_media_is_incomplete_not_pass(self) -> None:
        report = evaluate_av_quality(
            audio_frame_times_ms=[],
            video_frame_times_ms=[],
            audio_envelope=[],
            mouth_motion=[],
            continuity=None,
        )
        self.assertEqual(report["status"], "incomplete")
        self.assertNotIn("pass", {item["status"] for item in report["checks"]})


if __name__ == "__main__":
    unittest.main()
