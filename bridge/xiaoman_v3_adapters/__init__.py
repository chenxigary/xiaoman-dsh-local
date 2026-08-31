"""Isolated xiaoman v3 voice adapters.

This package is deliberately self-contained.  It is a migration boundary for
the small, model-facing pieces that can run in the DSH bridge; it must never
import from the historical macOS workspace.  Heavy model packages are loaded
inside provider methods so protocol and unit tests work without downloading
weights.
"""

from importlib import import_module


_EXPORTS = {
    "ASRResult": (".stt", "ASRResult"),
    "CancellationRequested": (".cancel", "CancellationRequested"),
    "CancellationToken": (".cancel", "CancellationToken"),
    "EnergyVADAdapter": (".vad", "EnergyVADAdapter"),
    "LegacyWhisperProvider": (".stt", "LegacyWhisperProvider"),
    "MacSTTProvider": (".stt", "MacSTTProvider"),
    "ProviderRegistry": (".registry", "ProviderRegistry"),
    "ProviderSelectionError": (".registry", "ProviderSelectionError"),
    "STTProvider": (".stt", "STTProvider"),
    "STTResult": (".stt", "STTResult"),
    "TTSProvider": (".tts", "TTSProvider"),
    "TTSResult": (".tts", "TTSResult"),
    "VADConfig": (".vad", "VADConfig"),
    "VADEvent": (".vad", "VADEvent"),
    "VADEventStream": (".vad", "VADEventStream"),
    "VADProcessResult": (".vad", "VADProcessResult"),
    "VADState": (".vad", "VADState"),
    "VoiceProfile": (".tts", "VoiceProfile"),
    "load_provider_config": (".registry", "load_provider_config"),
}


def __getattr__(name: str):
    """Preserve the public API without eagerly loading quarantined modules."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})

__all__ = [
    *_EXPORTS,
]
