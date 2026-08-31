"""Explicit, lazy provider selection for the migrated xiaoman namespace.

The baseline bridge keeps its existing FunASR/Qwen handler defaults.  A
bridge config can opt into ``mac-mlx-whisper``, ``legacy-whisper``, ``qwen3``
or ``omnivoice`` here without importing/download­ing a model at module import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "assets" / "xiaoman" / "config" / "providers.json"
SUPPORTED_STT = frozenset({"mac-mlx-whisper", "legacy-whisper"})
SUPPORTED_TTS = frozenset({"qwen3", "omnivoice"})


class ProviderSelectionError(ValueError):
    """A configured adapter name is not in the migrated allowlist."""


def load_provider_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        return {"namespace": "xiaoman_v3_adapters", "stt": {}, "tts": {}, "vad": {}}
    with candidate.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ProviderSelectionError(f"provider config must be an object: {candidate}")
    return value


class ProviderRegistry:
    """Resolve provider names and lazily instantiate migrated adapters."""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or load_provider_config())

    def stt_name(self, override: str | None = None) -> str:
        value = override or self._nested_name("stt", "default")
        if value not in SUPPORTED_STT:
            raise ProviderSelectionError(f"unsupported xiaoman STT provider: {value}")
        return value

    def tts_name(self, override: str | None = None) -> str:
        value = override or self._nested_name("tts", "default")
        if value not in SUPPORTED_TTS:
            raise ProviderSelectionError(f"unsupported xiaoman TTS provider: {value}")
        return value

    def create_stt(self, selection: str | None = None, **overrides: Any) -> Any:
        name = self.stt_name(selection)
        from .stt import LegacyWhisperProvider, MacSTTProvider

        if name == "mac-mlx-whisper":
            model_name = str(overrides.get("model_name") or self.config.get("stt", {}).get("mac_model") or "mlx-community/whisper-large-v3-turbo-asr-fp16")
            return MacSTTProvider(model_name=model_name)
        setup_kwargs = dict(overrides)
        setup_kwargs.pop("provider", None)
        setup_kwargs.pop("backend", None)
        return LegacyWhisperProvider(setup_kwargs=setup_kwargs)

    def create_tts(self, selection: str | None = None, **overrides: Any) -> Any:
        name = self.tts_name(selection)
        from .tts import OmniVoiceTTS, Qwen3TTS

        configured = dict(self.config.get("tts", {}))
        configured.update(overrides)
        ref_audio = configured.get("ref_audio") or configured.get("ref_audio_path")
        ref_text = configured.get("ref_text")
        if name == "qwen3":
            return Qwen3TTS(
                model_name=str(configured.get("qwen3_model") or configured.get("model_name") or "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"),
                ref_audio_path=ref_audio,
                ref_text=ref_text,
                language=str(configured.get("qwen3_language") or configured.get("language") or "zh"),
                streaming_interval=float(configured.get("streaming_interval") or 0.32),
                max_tokens=int(configured.get("max_tokens") or 2048),
            )
        return OmniVoiceTTS(
            model_name=str(configured.get("omnivoice_model") or configured.get("model_name") or "mlx-community/OmniVoice-bf16"),
            ref_audio_path=ref_audio,
            ref_text=ref_text,
            language=str(configured.get("omnivoice_language") or configured.get("language") or "chinese"),
            num_steps=int(configured.get("omnivoice_num_steps") or configured.get("num_steps") or 16),
        )

    def health(self) -> dict[str, Any]:
        return {
            "namespace": "xiaoman_v3_adapters",
            "stt": {"configured": self._nested_name("stt", "default"), "supported": sorted(SUPPORTED_STT)},
            "tts": {"configured": self._nested_name("tts", "default"), "supported": sorted(SUPPORTED_TTS)},
        }

    def _nested_name(self, key: str, nested: str) -> str:
        value = self.config.get(key, {})
        if not isinstance(value, Mapping):
            return ""
        return str(value.get(nested) or "")


__all__ = ["DEFAULT_CONFIG_PATH", "ProviderRegistry", "ProviderSelectionError", "load_provider_config"]
