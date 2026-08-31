"""Speech-to-text provider boundary and macOS/legacy adapters."""

from .providers import (
    ASRResult,
    LegacyWhisperProvider,
    MacSTTProvider,
    STTProvider,
    STTResult,
)

__all__ = [
    "ASRResult",
    "LegacyWhisperProvider",
    "MacSTTProvider",
    "STTProvider",
    "STTResult",
]
