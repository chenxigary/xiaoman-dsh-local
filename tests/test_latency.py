import json
import logging
import unittest
from io import StringIO

from bridge.latency import LatencyConfig, LatencyRecorder


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class LatencyRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = StringIO()
        self.logger = logging.getLogger(f"test.voice.latency.{id(self)}")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.StreamHandler(self.stream)
        self.logger.addHandler(handler)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.logger.handlers.clear()

    def test_emits_one_structured_event_with_stage_timings(self) -> None:
        recorder = LatencyRecorder(
            LatencyConfig(enabled=True, sample_rate=1.0),
            logger=self.logger,
            clock=self.clock,
            wall_clock=lambda: 1700000000.0,
            random_fn=lambda: 0.0,
        )
        span = recorder.start("stt", trace_id="trace-test", audio_bytes=80)
        self.clock.value += 0.012
        span.mark("request_body")
        stage_start = self.clock()
        self.clock.value += 0.034
        span.mark("decode", stage_start)
        self.clock.value += 0.004
        event = span.finish(status="ok")

        record = json.loads(self.stream.getvalue())
        self.assertEqual(record, event)
        self.assertEqual(record["event"], "voice.latency")
        self.assertEqual(record["operation"], "stt")
        self.assertEqual(record["trace_id"], "trace-test")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["stages_ms"]["request_body"], 12.0)
        self.assertEqual(record["stages_ms"]["decode"], 34.0)
        self.assertEqual(record["duration_ms"], 50.0)

    def test_disabled_and_unsampled_recorders_are_quiet(self) -> None:
        for config, random_fn in (
            (LatencyConfig(enabled=False), lambda: 0.0),
            (LatencyConfig(enabled=True, sample_rate=0.0), lambda: 1.0),
        ):
            recorder = LatencyRecorder(config, logger=self.logger, random_fn=random_fn)
            event = recorder.start("tts").finish()
            self.assertEqual(event["operation"], "tts")
            self.assertEqual(self.stream.getvalue(), "")

    def test_config_clamps_bad_sample_rate(self) -> None:
        self.assertEqual(LatencyConfig.from_mapping({"sample_rate": 4}).sample_rate, 1.0)
        self.assertEqual(LatencyConfig.from_mapping({"sample_rate": -1}).sample_rate, 0.0)
        self.assertEqual(LatencyConfig.from_mapping({"sample_rate": "bad"}).sample_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
