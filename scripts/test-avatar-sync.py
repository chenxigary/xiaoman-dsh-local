#!/usr/bin/env python3
"""Live WebRTC regression test for Avatar stalls and mouth/audio sync.

This probe is intentionally client-only.  It creates a real LiveTalking
``/offer`` session, injects a fixed human-speech PCM fixture through the
production ``AvatarRelay``, receives both WebRTC tracks, and then evaluates:

* audio/video inter-frame stalls on the receiver clock;
* nearest audio/video delivery skew;
* audio-energy to mouth-region-motion lag and correlation;
* LiveTalking playback underflow/inserted-silence telemetry.

Exit codes: 0=pass, 1=quality regression, 2=incomplete/environment unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time
import uuid
import wave
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bridge.avatar_relay import AvatarRelay  # noqa: E402
from bridge.av_quality import (  # noqa: E402
    AVQualityThresholds,
    TimedValue,
    evaluate_av_quality,
    paired_clock_gap_metrics,
    playback_idle_ready,
)
from bridge.livetalking_video import video_codecs_from_sdp  # noqa: E402

try:
    import aiohttp
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription
except ImportError as exc:  # pragma: no cover - exercised by wrapper dependency gate
    print(json.dumps({
        "schema": "xiaoman.av-quality/v1",
        "status": "incomplete",
        "error": f"live probe dependency missing: {exc}",
    }))
    raise SystemExit(2) from exc


class Capture:
    """Shared capture clock and lightweight media-derived traces."""

    def __init__(
        self,
        mouth_roi: tuple[float, float, float, float],
        control_roi: tuple[float, float, float, float],
    ) -> None:
        self.started_at = time.perf_counter()
        self.recording = False
        self.mouth_roi = mouth_roi
        self.control_roi = control_roi
        self.audio_times_ms: list[float] = []
        self.video_times_ms: list[float] = []
        self.audio_monotonic_ms: list[float] = []
        self.video_monotonic_ms: list[float] = []
        self.audio_media_pts_ms: list[float] = []
        self.video_media_pts_ms: list[float] = []
        self.audio_envelope: list[TimedValue] = []
        self.mouth_motion: list[TimedValue] = []
        self._previous_mouth: Any | None = None
        self._previous_control: Any | None = None
        self.audio_frames = 0
        self.video_frames = 0

    def now_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def reset(self) -> None:
        self.started_at = time.perf_counter()
        self.recording = True
        self.audio_times_ms.clear()
        self.video_times_ms.clear()
        self.audio_monotonic_ms.clear()
        self.video_monotonic_ms.clear()
        self.audio_media_pts_ms.clear()
        self.video_media_pts_ms.clear()
        self.audio_envelope.clear()
        self.mouth_motion.clear()
        self._previous_mouth = None
        self._previous_control = None
        self.audio_frames = 0
        self.video_frames = 0

    def accept_audio(self, frame: Any) -> None:
        if not self.recording:
            return
        timestamp = self.now_ms()
        monotonic_ms = time.monotonic_ns() / 1_000_000
        media_pts_ms = float(frame.pts * frame.time_base * 1000)
        samples = frame.to_ndarray().astype(np.float32, copy=False)
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        self.audio_times_ms.append(timestamp)
        self.audio_monotonic_ms.append(monotonic_ms)
        self.audio_media_pts_ms.append(media_pts_ms)
        self.audio_envelope.append(TimedValue(timestamp, rms))
        self.audio_frames += 1

    @staticmethod
    def _crop_normalized(image: Any, roi: tuple[float, float, float, float]) -> Any:
        height, width = image.shape[:2]
        x0, y0, x1, y1 = roi
        left = max(0, min(width - 1, round(width * x0)))
        right = max(left + 1, min(width, round(width * x1)))
        top = max(0, min(height - 1, round(height * y0)))
        bottom = max(top + 1, min(height, round(height * y1)))
        return image[top:bottom:3, left:right:3].astype(np.float32, copy=False)

    async def accept_video(self, frame: Any) -> None:
        if not self.recording:
            return
        timestamp = self.now_ms()
        monotonic_ms = time.monotonic_ns() / 1_000_000
        media_pts_ms = float(frame.pts * frame.time_base * 1000)
        self.video_times_ms.append(timestamp)
        self.video_monotonic_ms.append(monotonic_ms)
        self.video_media_pts_ms.append(media_pts_ms)
        # Pixel conversion is deliberately off the asyncio loop.  Otherwise
        # the diagnostic client can delay its own audio recv calls and invent
        # the very stall it is intended to detect.
        image = await asyncio.to_thread(
            lambda: frame.reformat(
                width=max(1, frame.width // 4),
                height=max(1, frame.height // 4),
                format="gray",
            ).to_ndarray()
        )
        mouth = self._crop_normalized(image, self.mouth_roi)
        control = self._crop_normalized(image, self.control_roi)
        if (
            self._previous_mouth is not None
            and self._previous_control is not None
            and self._previous_mouth.shape == mouth.shape
            and self._previous_control.shape == control.shape
        ):
            mouth_delta = float(np.mean(np.abs(mouth - self._previous_mouth)))
            control_delta = float(np.mean(np.abs(control - self._previous_control)))
            # Subtract a same-size upper-face control region so idle head
            # motion and codec noise do not masquerade as lip articulation.
            self.mouth_motion.append(
                TimedValue(timestamp, max(0.0, mouth_delta - control_delta))
            )
        self._previous_mouth = mouth.copy()
        self._previous_control = control.copy()
        self.video_frames += 1


def load_speech_pcm(
    path: Path,
    *,
    duration_ms: int,
    sample_rate: int,
) -> bytes:
    """Load a deterministic mono speech fixture and resample it to PCM16."""

    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("speech fixture must be mono signed 16-bit PCM WAV")
        source_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    if source_rate != sample_rate:
        target_count = round(len(samples) * sample_rate / source_rate)
        source_positions = np.arange(len(samples), dtype=np.float64)
        target_positions = np.arange(target_count, dtype=np.float64) * source_rate / sample_rate
        samples = np.clip(
            np.interp(target_positions, source_positions, samples.astype(np.float64)),
            -32768,
            32767,
        ).astype("<i2")
    # Do not upload leading zero PCM as normal speech: LiveTalking correctly
    # treats every type-0 frame as a speaking frame, which would make the
    # diagnostic itself manufacture an early mouth onset.  Preserve 40 ms of
    # preroll before the first active 20-ms window.
    window = max(1, round(sample_rate * 0.02))
    first_active = None
    for offset in range(0, len(samples), window):
        chunk = samples[offset : offset + window].astype(np.float32, copy=False)
        if chunk.size and float(np.sqrt(np.mean(np.square(chunk)))) >= 200.0:
            first_active = max(0, offset - window * 2)
            break
    if first_active is None:
        raise ValueError("speech fixture contains no active speech")
    samples = samples[first_active:]
    required = round(duration_ms * sample_rate / 1000)
    if len(samples) < required:
        raise ValueError(
            "speech fixture is too short: "
            f"need {duration_ms}ms after trim, have {len(samples) * 1000 / sample_rate:.0f}ms"
        )
    return samples[:required].astype("<i2", copy=False).tobytes()


async def json_post(session: Any, url: str, payload: dict[str, object], timeout: float = 5.0) -> dict[str, Any]:
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        response.raise_for_status()
        result = await response.json()
    if result.get("code") not in (None, 0):
        raise RuntimeError(f"{url} rejected request: {result.get('msg', 'unknown error')}")
    return result


async def wait_for_idle(
    client: Any,
    avatar_url: str,
    avatar_session_id: str,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, object] | None, bool]:
    deadline = time.monotonic() + timeout_seconds
    seen_speaking = False
    idle_since: float | None = None
    continuity: dict[str, object] | None = None
    while time.monotonic() < deadline:
        result = await json_post(
            client,
            f"{avatar_url}/is_speaking",
            {"sessionid": avatar_session_id},
        )
        if isinstance(result.get("continuity"), dict):
            continuity = result["continuity"]
        if result.get("data") is True:
            seen_speaking = True
            idle_since = None
        elif playback_idle_ready(
            is_speaking=result.get("data"),
            seen_speaking=seen_speaking,
            continuity=continuity,
        ):
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= 0.4:
                return continuity, True
        else:
            # ``is_speaking`` can briefly flicker false between Wav2Lip
            # inference batches even though the logical turn is still active
            # and seconds of queued PCM remain.  Treat that as playback, not
            # idle, or the probe will close WebRTC in the middle of the turn.
            idle_since = None
        await asyncio.sleep(0.08)
    return continuity, False


async def run_probe(args: argparse.Namespace) -> dict[str, object]:
    avatar_url = args.avatar_url.rstrip("/")
    peer = RTCPeerConnection()
    connected = asyncio.Event()
    track_tasks: set[asyncio.Task[None]] = set()
    capture = Capture(tuple(args.mouth_roi), tuple(args.control_roi))
    avatar_session_id: str | None = None

    @peer.on("connectionstatechange")
    async def connection_state_change() -> None:
        if peer.connectionState in {"connected", "failed", "closed"}:
            connected.set()

    async def consume(track: Any) -> None:
        while True:
            try:
                frame = await track.recv()
            except Exception:  # track closed after test cleanup
                return
            if track.kind == "audio":
                capture.accept_audio(frame)
            elif track.kind == "video":
                await capture.accept_video(frame)

    @peer.on("track")
    def on_track(track: Any) -> None:
        task = asyncio.create_task(consume(track))
        track_tasks.add(task)
        task.add_done_callback(track_tasks.discard)

    peer.addTransceiver("video", direction="recvonly")
    peer.addTransceiver("audio", direction="recvonly")
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    while peer.iceGatheringState != "complete":
        await asyncio.sleep(0.01)

    async with aiohttp.ClientSession() as client:
        answer = await json_post(
            client,
            f"{avatar_url}/offer",
            {
                "sdp": peer.localDescription.sdp,
                "type": peer.localDescription.type,
                "session_ttl_sec": max(45, round(args.timeout + 15)),
            },
            timeout=15.0,
        )
        avatar_session_id = str(answer["sessionid"])
        answer_sdp = str(answer["sdp"])
        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type=answer["type"])
        )
        await asyncio.wait_for(connected.wait(), timeout=15.0)
        if peer.connectionState != "connected":
            raise RuntimeError(f"WebRTC connection state is {peer.connectionState}")

        # Let both idle tracks settle before resetting the common capture clock.
        await asyncio.sleep(0.6)
        capture.reset()
        # Record a true idle baseline before any type-0 audio enters
        # LiveTalking.  This is wall-clock wait, not silent "speech" PCM.
        await asyncio.sleep(args.lead_silence_ms / 1000.0)
        relay = AvatarRelay(avatar_url, timeout_seconds=5.0, queue_size=32)
        dsh_session_id = f"av-sync-test:{uuid.uuid4()}"
        relay.register(dsh_session_id, avatar_session_id)
        turn_id = f"av-sync-{uuid.uuid4()}"
        packet_tasks: list[asyncio.Task[bool]] = []
        speech_pcm = load_speech_pcm(
            args.speech_wav,
            duration_ms=args.duration_ms,
            sample_rate=args.sample_rate,
        )
        offset_bytes = 0
        packet_count = math.ceil(args.duration_ms / args.packet_ms)
        for index in range(packet_count):
            packet_duration = min(args.packet_ms, args.duration_ms - index * args.packet_ms)
            packet_bytes = round(packet_duration * args.sample_rate / 1000) * 2
            pcm = speech_pcm[offset_bytes : offset_bytes + packet_bytes]
            offset_bytes += len(pcm)
            task = relay.submit_pcm(
                dsh_session_id,
                pcm,
                turn_id=turn_id,
                generation=1,
                sample_rate=args.sample_rate,
                end=index == packet_count - 1,
            )
            if task is None:
                raise RuntimeError(f"AvatarRelay rejected packet {index}")
            packet_tasks.append(task)

        idle_task = asyncio.create_task(
            wait_for_idle(
                client,
                avatar_url,
                avatar_session_id,
                timeout_seconds=args.timeout,
            )
        )
        upload_results = await asyncio.gather(*packet_tasks)
        if not all(upload_results):
            raise RuntimeError("one or more AvatarRelay PCM uploads failed")
        continuity, observed_idle = await idle_task
        await asyncio.sleep(0.35)
        capture.recording = False

        thresholds = AVQualityThresholds(
            max_audio_gap_ms=args.max_audio_gap_ms,
            max_video_gap_ms=args.max_video_gap_ms,
            max_av_delivery_skew_p95_ms=args.max_av_skew_p95_ms,
            max_abs_lip_lag_ms=args.max_lip_lag_ms,
            min_lip_correlation=args.min_lip_correlation,
            max_underflow_events=args.max_underflows,
            max_inserted_silence_ms=args.max_inserted_silence_ms,
        )
        report = evaluate_av_quality(
            audio_frame_times_ms=capture.audio_times_ms,
            video_frame_times_ms=capture.video_times_ms,
            audio_envelope=capture.audio_envelope,
            mouth_motion=capture.mouth_motion,
            continuity=continuity,
            thresholds=thresholds,
            audio_active_threshold=200.0,
        )
        report.update({
            "timing": {
                "clock": "time.monotonic_ns",
                "receiver": {
                    "audio": paired_clock_gap_metrics(
                        capture.audio_monotonic_ms,
                        capture.audio_media_pts_ms,
                    ),
                    "video": paired_clock_gap_metrics(
                        capture.video_monotonic_ms,
                        capture.video_media_pts_ms,
                    ),
                },
                "sender": (
                    continuity.get("webrtc", {}).get("sender_timing")
                    if isinstance(continuity, dict)
                    and isinstance(continuity.get("webrtc"), dict)
                    else None
                ),
            },
            "probe": {
                "avatar_url": avatar_url,
                "duration_ms": args.duration_ms,
                "packet_ms": args.packet_ms,
                "sample_rate": args.sample_rate,
                "speech_wav": str(args.speech_wav),
                "lead_silence_ms": args.lead_silence_ms,
                "mouth_roi": args.mouth_roi,
                "control_roi": args.control_roi,
                "negotiated_video_codecs": video_codecs_from_sdp(answer_sdp),
                "observed_speaking_then_idle": observed_idle,
                "audio_frames": capture.audio_frames,
                "video_frames": capture.video_frames,
            }
        })
        if not observed_idle and report["status"] != "fail":
            report["status"] = "incomplete"
            report["checks"].append({
                "name": "avatar_speaking_then_idle",
                "status": "incomplete",
                "value": False,
                "limit": True,
                "unit": "boolean",
            })

        try:
            await json_post(client, f"{avatar_url}/api/session/close", {"sessionid": avatar_session_id})
            avatar_session_id = None
        finally:
            await peer.close()
        return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avatar-url", default="http://127.0.0.1:8010")
    parser.add_argument("--duration-ms", type=int, default=7200)
    parser.add_argument("--packet-ms", type=int, default=400)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument(
        "--speech-wav",
        type=Path,
        default=REPO_ROOT / "assets" / "xiaoman" / "voice" / "ref.wav",
        help="deterministic mono PCM16 speech fixture",
    )
    parser.add_argument(
        "--lead-silence-ms",
        type=int,
        default=1000,
        help="true idle capture before speech upload, used as the motion baseline",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--mouth-roi",
        type=float,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=(0.51, 0.47, 0.69, 0.60),
        help="normalized mouth-region rectangle for the configured Avatar",
    )
    parser.add_argument(
        "--control-roi",
        type=float,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=(0.51, 0.29, 0.69, 0.42),
        help="same-size upper-face control rectangle used to remove idle motion",
    )
    parser.add_argument("--max-audio-gap-ms", type=float, default=100.0)
    parser.add_argument("--max-video-gap-ms", type=float, default=200.0)
    parser.add_argument("--max-av-skew-p95-ms", type=float, default=120.0)
    parser.add_argument("--max-lip-lag-ms", type=float, default=240.0)
    parser.add_argument("--min-lip-correlation", type=float, default=0.12)
    parser.add_argument("--max-underflows", type=int, default=0)
    parser.add_argument("--max-inserted-silence-ms", type=float, default=0.0)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if args.duration_ms < 2000 or args.packet_ms < 100 or args.packet_ms > args.duration_ms:
        parser.error("duration must be >=2000ms and packet must be between 100ms and duration")
    if args.sample_rate < 8000 or args.sample_rate > 48000:
        parser.error("sample rate must be between 8000 and 48000")
    if args.lead_silence_ms < 400 or args.lead_silence_ms >= args.duration_ms - 1000:
        parser.error("lead silence must be >=400ms and leave at least 1000ms of speech")
    if not args.speech_wav.is_file():
        parser.error(f"speech fixture is missing: {args.speech_wav}")
    if not all(0 <= value <= 1 for value in args.mouth_roi) or not (
        args.mouth_roi[0] < args.mouth_roi[2] and args.mouth_roi[1] < args.mouth_roi[3]
    ):
        parser.error("mouth ROI must be an increasing normalized rectangle")
    if not all(0 <= value <= 1 for value in args.control_roi) or not (
        args.control_roi[0] < args.control_roi[2]
        and args.control_roi[1] < args.control_roi[3]
    ):
        parser.error("control ROI must be an increasing normalized rectangle")
    return args


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run_probe(args))
    except Exception as exc:  # environment/service problems are not quality passes
        report = {
            "schema": "xiaoman.av-quality/v1",
            "status": "incomplete",
            "error": f"{type(exc).__name__}: {exc}",
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.get("status") == "pass" else (1 if report.get("status") == "fail" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
