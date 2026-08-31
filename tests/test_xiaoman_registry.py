import json
import tempfile
import unittest
from pathlib import Path

from bridge.character_registry import normalize_character, state_media
from bridge.xiaoman_v3_adapters.registry import ProviderRegistry, ProviderSelectionError, load_provider_config


class XiaomanRegistryTests(unittest.TestCase):
    def test_character_is_allowlisted_and_unknown_falls_back(self) -> None:
        self.assertEqual(normalize_character("xiaoman"), "xiaoman")
        self.assertEqual(normalize_character("unknown"), "default")

    def test_verified_idle_media_is_returned_and_missing_state_falls_back(self) -> None:
        idle = state_media("xiaoman", "idle")
        thinking = state_media("xiaoman", "thinking")
        self.assertTrue(any(entry["name"] == "idle.mp4" for entry in idle))
        self.assertEqual(thinking, idle)
        self.assertEqual(state_media("default", "idle"), [])

    def test_registry_reads_explicit_selection_without_loading_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            path.write_text(json.dumps({"stt": {"default": "legacy-whisper"}, "tts": {"default": "omnivoice"}}), encoding="utf-8")
            registry = ProviderRegistry(load_provider_config(path))
            self.assertEqual(registry.stt_name(), "legacy-whisper")
            self.assertEqual(registry.tts_name(), "omnivoice")
            self.assertIn("mac-mlx-whisper", registry.health()["stt"]["supported"])

    def test_registry_rejects_unknown_provider(self) -> None:
        registry = ProviderRegistry({"stt": {"default": "funasr"}, "tts": {"default": "qwen3"}})
        with self.assertRaises(ProviderSelectionError):
            registry.stt_name()

    def test_default_omnivoice_uses_provider_specific_model_and_balanced_steps(self) -> None:
        registry = ProviderRegistry(load_provider_config())
        provider = registry.create_tts(
            "omnivoice",
            model_name="must-not-shadow-the-provider-specific-model",
            ref_audio="assets/xiaoman/voice/ref.wav",
            ref_text="参考文本",
            language="zh",
        )
        self.assertEqual(provider.model_name, "mlx-community/OmniVoice-bf16")
        self.assertEqual(provider.language, "chinese")
        self.assertEqual(provider.num_steps, 16)


if __name__ == "__main__":
    unittest.main()
