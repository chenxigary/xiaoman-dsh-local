# xiaoman v3 adapter baseline

## Authority and drift rule

Xiaoman v3 is the authority for the reusable voice runtime. DSH production now
consumes the loopback-only `xiaoman.voice-runtime.v1` HTTP boundary; it does
not import or load the copied MLX STT/TTS providers. [`source-lock.json`](source-lock.json)
pins the exact v3 baseline revision, source hash, local hash, adaptation mode,
and runtime status for every derived file.

All copied STT/TTS providers are now **explicit local fallback only**. Set
`DSH_VOICE_RUNTIME_MODE=local` to use them for rollback; runtime failures never
trigger an automatic fallback or a second MLX model load. `audio/bus.py`,
`tts/text_segmentation.py`, and `vad/energy_vad.py` remain compatibility-test-only.
Change voice algorithms in v3, not here. DSH continues to own browser
AudioWorklet capture, Silero `/api/vad`, sentence assembly, playback,
AgentLoop, Codex, and React UI.

The dependency lock records the current `mlx-audio` split: v3 is pinned to
0.4.7 and is authoritative. DSH's 0.5.0 pin is retained solely for the
explicit rollback path; it no longer creates ambiguity in the default runtime.

This is the isolated migration namespace for the voice boundary. When the
bridge runs with `bridge/` as its working directory, import it as
`xiaoman_v3_adapters`; repository-level tests may import the same files as
`bridge.xiaoman_v3_adapters`.

```python
from xiaoman_v3_adapters.cancel import CancellationToken
from xiaoman_v3_adapters.stt import MacSTTProvider, LegacyWhisperProvider
from xiaoman_v3_adapters.tts import Qwen3TTS, TTSProvider
from xiaoman_v3_adapters.vad import EnergyVADAdapter, VADEvent
```

Provider rules:

- STT accepts mono PCM at any positive input rate and normalizes to 16 kHz.
  `MacSTTProvider` lazily imports `mlx_audio`; `LegacyWhisperProvider` lazily
  imports the existing `speech_to_speech` handler.
- TTS accepts a shared `cancel=` token. Qwen/OmniVoice check it before model
  work and between stream chunks. A native call already in flight is allowed
  to finish and its result must be discarded by the caller's generation check.
- `EnergyVADAdapter.feed()` returns `VADProcessResult(audio, events)`. Events
  are `speech_start`, `speech_end` (soft/final flags), and `speech_reopen`.
- `AudioBus` keeps required browser output separate from optional avatar work;
  `set_generation()` invalidates stale packets and interrupts every sink.

No provider imports the historical source workspace or any source-relative
model/cache path. Model weights remain an external deployment concern.
