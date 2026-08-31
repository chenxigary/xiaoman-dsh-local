from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bridge.xiaoman_v3_adapters.audio import AudioBus, AudioPacket
from bridge.xiaoman_v3_adapters.cancel import (
    CancellationRequested,
    CancellationToken,
)
from bridge.xiaoman_v3_adapters.stt import (
    LegacyWhisperProvider,
    MacSTTProvider,
    STTProvider,
)
from bridge.xiaoman_v3_adapters.tts import OmniVoiceTTS, Qwen3TTS, TTSProvider
from bridge.xiaoman_v3_adapters.vad import EnergyVADAdapter, VADConfig


class MacSTTProviderTests(unittest.TestCase):
    def test_lazy_model_and_sample_rate_normalization(self):
        calls: list[str] = []

        class Model:
            def generate(self, audio):
                self.audio = audio
                return SimpleNamespace(text="你好", language="zh")

        model = Model()
        provider = MacSTTProvider(
            model_loader=lambda name: calls.append(name) or model,
        )
        self.assertIsInstance(provider, STTProvider)
        self.assertFalse(provider.loaded)
        result = provider.transcribe(np.ones(800, dtype=np.float32), 8_000)
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.duration_sec, 0.1)
        self.assertEqual(len(model.audio), 1_600)
        self.assertEqual(len(calls), 1)
        provider.transcribe(np.ones(1_600, dtype=np.float32), 16_000)
        self.assertEqual(len(calls), 1)

    def test_cancellation_is_observed_before_model_load(self):
        calls: list[str] = []
        token = CancellationToken()
        token.cancel()
        provider = MacSTTProvider(
            model_loader=lambda name: calls.append(name),
        )
        with self.assertRaises(CancellationRequested):
            provider.transcribe(np.ones(160, dtype=np.float32), cancel=token)
        self.assertEqual(calls, [])


class LegacyWhisperProviderTests(unittest.TestCase):
    def test_legacy_handler_is_hidden_behind_the_same_result_shape(self):
        class VADAudio:
            def __init__(self, *, audio):
                self.audio = audio

        messages = ModuleType("speech_to_speech.pipeline.messages")
        messages.VADAudio = VADAudio
        pipeline = ModuleType("speech_to_speech.pipeline")
        speech_to_speech = ModuleType("speech_to_speech")
        old_modules = {
            name: sys.modules.get(name)
            for name in (
                "speech_to_speech",
                "speech_to_speech.pipeline",
                "speech_to_speech.pipeline.messages",
            )
        }
        sys.modules.update(
            {
                "speech_to_speech": speech_to_speech,
                "speech_to_speech.pipeline": pipeline,
                "speech_to_speech.pipeline.messages": messages,
            }
        )
        try:
            class Handler:
                def process(self, item):
                    self.audio = item.audio
                    return iter(
                        [SimpleNamespace(text="旧 Whisper", language_code="zh")]
                    )

            provider = LegacyWhisperProvider(handler_factory=Handler)
            result = provider.transcribe(np.ones(1_600, dtype=np.float32))
            self.assertEqual(result.text, "旧 Whisper")
            self.assertEqual(result.language, "zh")
            self.assertTrue(provider.health()["loaded"])
        finally:
            for name, value in old_modules.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


class QwenTTSProviderTests(unittest.TestCase):
    def test_stream_cancellation_stops_between_chunks(self):
        class Model:
            sample_rate = 24_000
            yielded = 0

            def generate(self, **kwargs):
                self.kwargs = kwargs
                self.yielded += 1
                yield SimpleNamespace(audio=np.ones(24, dtype=np.float32))
                self.yielded += 1
                yield SimpleNamespace(audio=np.ones(24, dtype=np.float32))

        model = Model()
        provider = Qwen3TTS(model_loader=lambda **_: model)
        self.assertIsInstance(provider, TTSProvider)
        token = CancellationToken()
        stream = provider.stream("你好。", turn_id="t1", cancel=token)
        first = next(stream)
        self.assertEqual(first.chunk_index, 0)
        self.assertEqual(model.yielded, 1)
        token.cancel()
        with self.assertRaises(CancellationRequested):
            next(stream)
        self.assertTrue(model.kwargs["stream"])

    def test_stream_does_not_invent_final_metadata(self):
        class Model:
            sample_rate = 24_000

            def generate(self, **kwargs):
                del kwargs
                yield SimpleNamespace(audio=np.ones(24, dtype=np.float32))
                yield SimpleNamespace(audio=np.ones(24, dtype=np.float32))

        provider = Qwen3TTS(model_loader=lambda **_: Model())
        chunks = list(provider.stream("你好。", turn_id="t1"))
        self.assertEqual([chunk.is_final_chunk for chunk in chunks], [False, False])


class OmniVoiceProviderTests(unittest.TestCase):
    def test_reference_prompt_is_cached_and_not_sent_as_audio_per_turn(self):
        class Model:
            sample_rate = 24_000

            def __init__(self):
                self.audio_tokenizer = object()
                self.calls = []

            def generate(self, **kwargs):
                self.calls.append(kwargs)
                return [
                    SimpleNamespace(
                        audio=np.ones(24, dtype=np.float32),
                        sample_rate=self.sample_rate,
                    )
                ]

        model = Model()
        prompt_calls = []
        with tempfile.TemporaryDirectory() as directory:
            ref_audio = Path(directory) / "ref.wav"
            ref_audio.write_bytes(b"test")
            provider = OmniVoiceTTS(
                model_name="test/omnivoice",
                ref_audio_path=ref_audio,
                ref_text="参考文本",
                model_loader=lambda **_: model,
                ref_prompt_builder=lambda path, **kwargs: prompt_calls.append(
                    (path, kwargs)
                )
                or np.ones((1, 2), dtype=np.int32),
            )
            provider.generate("第一句。")
            provider.generate("第二句。")
        self.assertEqual(len(prompt_calls), 1)
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(all("ref_audio" not in call for call in model.calls))
        self.assertTrue(all(call["ref_tokens"] is not None for call in model.calls))


class VADEventTests(unittest.TestCase):
    @staticmethod
    def audio(duration_ms: int, amplitude: float = 0.0) -> np.ndarray:
        return np.full(duration_ms, amplitude, dtype=np.float32)

    def make_vad(self, **overrides):
        config = {
            "sample_rate": 1_000,
            "frame_ms": 100,
            "energy_threshold": 0.015,
            "min_speech_ms": 200,
            "min_silence_ms": 200,
            "speech_pad_ms": 0,
            "min_speech_continuation_ms": 100,
            "short_segment_merge_ms": 0,
            "speculative_reopen_ms": 300,
            "max_buffered_ms": 2_000,
        }
        config.update(overrides)
        return EnergyVADAdapter(VADConfig(**config))

    def test_soft_end_and_final_commit_are_distinct_events(self):
        vad = self.make_vad(speculative_reopen_ms=0)
        start = vad.feed(self.audio(200, 0.5))
        self.assertEqual([event.kind for event in start.events], ["speech_start"])
        end = vad.feed(self.audio(200))
        self.assertEqual(
            [event.kind for event in end.events],
            ["speech_end", "speech_end"],
        )
        self.assertTrue(end.events[0].soft)
        self.assertTrue(end.events[1].final)
        self.assertEqual(len(end.events[0].audio), 400)
        self.assertEqual(len(end.audio), 400)
        self.assertEqual(end.events[1].as_dict()["event"], "speech_end")

    def test_reopen_event_keeps_one_turn(self):
        vad = self.make_vad()
        vad.feed(self.audio(200, 0.5))
        soft = vad.feed(self.audio(200))
        self.assertTrue(soft.events[0].soft)
        resumed = vad.feed(self.audio(100, 0.5))
        self.assertEqual([event.kind for event in resumed.events], ["speech_reopen"])
        self.assertTrue(vad.is_speaking)


class AudioBusTests(unittest.IsolatedAsyncioTestCase):
    class Sink:
        def __init__(self, name: str, required: bool):
            self.name = name
            self.required = required
            self.packets = []
            self.generations = []
            self.closed = False

        async def emit(self, packet):
            self.packets.append(packet)

        async def interrupt(self, generation):
            self.generations.append(generation)

        async def close(self):
            self.closed = True

    @staticmethod
    def packet(generation: int) -> AudioPacket:
        return AudioPacket(
            session_id="s",
            turn_id="t",
            generation=generation,
            seq=0,
            pts_ms=0,
            sample_rate=24_000,
            channels=1,
            wav_bytes=b"RIFF",
            duration_ms=1,
        )

    async def test_generation_drops_stale_packets_and_closes(self):
        bus = AudioBus()
        browser = self.Sink("browser", True)
        avatar = self.Sink("avatar", False)
        bus.register(browser)
        bus.register(avatar)
        await bus.set_generation(2)
        self.assertFalse(await bus.publish(self.packet(1)))
        current = self.packet(2)
        self.assertTrue(await bus.publish(current))
        await bus.drain_optional()
        self.assertIs(browser.packets[0], current)
        self.assertIs(avatar.packets[0], current)
        await bus.close()
        self.assertTrue(browser.closed)
        self.assertTrue(avatar.closed)


if __name__ == "__main__":
    unittest.main()
