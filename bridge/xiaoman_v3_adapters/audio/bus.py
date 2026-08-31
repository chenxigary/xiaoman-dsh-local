"""Generation-aware audio fan-out.

The browser sink is normally marked as required while avatar/diagnostic sinks are
best-effort.  Best-effort sinks are always scheduled in the background so a slow
or failed avatar can never delay direct browser audio.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioPacket:
    """One immutable TTS audio packet shared by every consumer."""

    session_id: str
    turn_id: str
    generation: int
    seq: int
    pts_ms: int
    sample_rate: int
    channels: int
    wav_bytes: bytes
    duration_ms: int
    # Raw signed little-endian PCM for low-latency browser playback. WAV stays
    # available for LiveTalking and legacy clients that require a container.
    pcm_bytes: bytes | None = None
    format: str = "wav"
    start: bool = False
    end: bool = False
    streaming: bool = False


@runtime_checkable
class AudioSink(Protocol):
    """Consumer contract used by :class:`AudioBus`."""

    name: str
    required: bool

    async def emit(self, packet: AudioPacket) -> None: ...

    async def interrupt(self, generation: int) -> None: ...

    async def close(self) -> None: ...


class AudioBus:
    """Fan out one TTS packet with generation-based stale-data rejection."""

    def __init__(self, *, optional_timeout_sec: float = 0.25):
        self._generation = 0
        self._optional_timeout_sec = optional_timeout_sec
        self._sinks: dict[str, AudioSink] = {}
        self._optional_tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    def register(self, sink: AudioSink) -> None:
        if self._closed:
            raise RuntimeError("audio bus is closed")
        self._sinks[sink.name] = sink

    def unregister(self, name: str) -> AudioSink | None:
        return self._sinks.pop(name, None)

    async def set_generation(self, generation: int) -> None:
        """Advance the generation and ask all sinks to discard older work."""
        if generation < self._generation:
            return
        self._generation = generation
        results = await asyncio.gather(
            *(sink.interrupt(generation) for sink in tuple(self._sinks.values())),
            return_exceptions=True,
        )
        for sink, result in zip(tuple(self._sinks.values()), results):
            if isinstance(result, Exception):
                logger.warning("Audio sink %s interrupt failed: %s", sink.name, result)

    async def publish_required(self, packet: AudioPacket) -> bool:
        """Publish immediately to required sinks only.

        The browser is the required sink.  Keeping this path separate from
        optional Avatar fan-out lets callers add optional look-ahead without
        delaying the first browser PCM packet.
        """

        if self._closed or packet.generation != self._generation:
            return False

        for sink in tuple(self._sinks.values()):
            if sink.required:
                await sink.emit(packet)
        return True

    async def publish_optional(
        self, packet: AudioPacket, *, wait: bool = False
    ) -> bool:
        """Fan out to optional sinks without affecting required sinks.

        Normal packets are scheduled and return immediately.  A caller may
        use ``wait=True`` for a final boundary so an enqueue rejection is
        observable before sending ``reply_end``; this still does not run on
        the browser's per-packet path.
        """

        if self._closed or packet.generation != self._generation:
            return False

        optional_sinks = tuple(
            sink for sink in self._sinks.values() if not sink.required
        )
        if wait:
            results = await asyncio.gather(
                *(self._emit_optional(sink, packet) for sink in optional_sinks),
                return_exceptions=True,
            )
            return all(result is True for result in results)

        for sink in optional_sinks:
            task = asyncio.create_task(self._emit_optional(sink, packet))
            self._optional_tasks.add(task)
            task.add_done_callback(self._optional_tasks.discard)
        return True

    async def publish(self, packet: AudioPacket) -> bool:
        """Publish to required and optional sinks using the same packet."""

        if not await self.publish_required(packet):
            return False
        return await self.publish_optional(packet)

    async def _emit_optional(self, sink: AudioSink, packet: AudioPacket) -> bool:
        try:
            await asyncio.wait_for(
                sink.emit(packet), timeout=self._optional_timeout_sec
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("Optional audio sink %s timed out", sink.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Optional audio sink %s failed: %s", sink.name, exc)
        return False

    def snapshot(self) -> dict:
        """Return local sink state without performing network I/O."""

        sinks: dict[str, object] = {}
        for sink in tuple(self._sinks.values()):
            snapshot = getattr(sink, "snapshot", None)
            if callable(snapshot):
                try:
                    sinks[sink.name] = snapshot()
                except Exception as exc:  # diagnostics must never break audio
                    sinks[sink.name] = {"degraded": True, "error": str(exc)}
        return {
            "generation": self._generation,
            "optional_tasks": len(self._optional_tasks),
            "sinks": sinks,
        }

    async def drain_optional(self) -> None:
        """Wait for currently scheduled best-effort fan-out (mainly for tests)."""
        if self._optional_tasks:
            await asyncio.gather(*tuple(self._optional_tasks), return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in tuple(self._optional_tasks):
            task.cancel()
        if self._optional_tasks:
            await asyncio.gather(*tuple(self._optional_tasks), return_exceptions=True)
        await asyncio.gather(
            *(sink.close() for sink in tuple(self._sinks.values())),
            return_exceptions=True,
        )
        self._sinks.clear()
