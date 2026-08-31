"""Runtime-only LiveTalking WebRTC continuity overlay.

The authoritative Xiaoman v3 checkout stays read-only.  ``start-avatar.sh``
loads this overlay before LiveTalking's ``app.py``.  It replaces each track's
plain audio producer queue with a compatible queue that supplies one paced
20 ms silent frame during a short renderer gap.

LiveTalking's own ``PlayerStreamTrack.next_timestamp`` still owns PTS and wall
clock pacing.  Real queued frames always take priority, so the overlay neither
drops nor advances synthesized speech; it only prevents an RTP discontinuity
while Wav2Lip accumulates its configured inference stride.  The video queue is
left completely untouched: copying full-size frames in Python multiplied CPU
cost under multiple browser sessions and could make the renderer queue grow.
"""

from __future__ import annotations

import queue
from typing import Any, Callable


class ContinuityQueue(queue.Queue[Any]):
    """A Queue that returns synthetic media only when no real item is ready."""

    def __init__(
        self,
        maxsize: int,
        *,
        generation: Callable[[], int],
        silent_audio: Callable[[], Any],
    ) -> None:
        super().__init__(maxsize=maxsize)
        self._generation = generation
        self._silent_audio = silent_audio
        self._armed = False
        self.fallback_frames = 0

    def get_nowait(self) -> Any:
        try:
            item = super().get_nowait()
        except queue.Empty:
            # Do not manufacture the first frame.  Let LiveTalking bring both
            # tracks and its render worker up using its normal startup path;
            # continuity fill is only valid after that producer has emitted.
            if not self._armed:
                raise
            self.fallback_frames += 1
            return self._silent_audio(), None, self._generation()
        self._armed = True
        return item


def install_livetalking_continuity(webrtc_module: Any) -> bool:
    """Patch one loaded ``server.webrtc`` module, idempotently."""

    track_class = webrtc_module.PlayerStreamTrack
    if getattr(track_class, "_xiaoman_continuity_installed", False):
        return False
    original_init = track_class.__init__
    original_clear_queue = track_class.clear_queue

    def silent_audio() -> Any:
        samples = round(webrtc_module.AUDIO_PTIME * webrtc_module.SAMPLE_RATE)
        frame = webrtc_module.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(bytes(samples * 2))
        frame.sample_rate = webrtc_module.SAMPLE_RATE
        return frame

    def patched_init(self: Any, player: Any, kind: str) -> None:
        original_init(self, player, kind)
        if kind != "audio":
            return
        current_queue = self._queue
        self._queue = ContinuityQueue(
            maxsize=current_queue.maxsize,
            generation=lambda: self.generation,
            silent_audio=silent_audio,
        )

    def patched_clear_queue(self: Any) -> int:
        target = self._queue
        if not isinstance(target, ContinuityQueue):
            return original_clear_queue(self)
        # Bypass ContinuityQueue.get_nowait(): queue maintenance must drain
        # only producer-owned frames and must terminate once the real queue is
        # empty.  This path runs on every generation switch.
        drained = 0
        while True:
            try:
                queue.Queue.get_nowait(target)
            except queue.Empty:
                return drained
            drained += 1
            try:
                target.task_done()
            except ValueError:
                pass

    track_class.__init__ = patched_init
    track_class.clear_queue = patched_clear_queue
    track_class._xiaoman_continuity_installed = True
    return True


__all__ = ["ContinuityQueue", "install_livetalking_continuity"]
