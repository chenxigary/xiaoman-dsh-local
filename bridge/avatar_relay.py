"""Best-effort relay from the voice bridge to a loopback LiveTalking avatar.

The browser owns the WebRTC session.  It registers the opaque LiveTalking
session id here under its DSH conversation id; completed TTS WAVs can then be
uploaded to the matching avatar without delaying browser audio playback.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import secrets
import urllib.request
import wave
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("voice.avatar")

_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_AVATAR_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_AVATAR_WAV_BYTES = 4 * 1024 * 1024


@dataclass
class _StreamCursor:
    turn_id: str
    generation: int
    seq: int = 0
    pts_ms: int = 0
    started: bool = False


def validate_avatar_base(value: str) -> str:
    """Accept an HTTP loopback origin only; this sink must never become SSRF."""

    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("avatar base URL must be a loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("avatar base URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("avatar base URL must include a valid port")
    return value.strip().rstrip("/")


def validate_dsh_session(value: str) -> str:
    value = value.strip()
    if not _SESSION_RE.fullmatch(value):
        raise ValueError("invalid DSH session id")
    return value


def validate_avatar_session(value: str) -> str:
    value = value.strip()
    if not _AVATAR_SESSION_RE.fullmatch(value):
        raise ValueError("invalid avatar session id")
    return value


class AvatarRelay:
    """Session registry plus bounded, non-blocking LiveTalking WAV uploads."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = 8.0,
        queue_size: int = 8,
    ) -> None:
        if queue_size < 2:
            raise ValueError("avatar queue_size must be at least 2")
        configured = base_url or os.environ.get("XIAOMAN_AVATAR_URL", "http://127.0.0.1:8010")
        self.base_url = validate_avatar_base(configured)
        self.timeout_seconds = min(30.0, max(1.0, float(timeout_seconds)))
        self.queue_size = min(64, int(queue_size))
        self._sessions: dict[str, str] = {}
        self._session_tokens: dict[str, object] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._stream_pending: dict[object, set[asyncio.Task[bool]]] = {}
        self._stream_cursors: dict[str, _StreamCursor] = {}
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._stream_tails: dict[str, asyncio.Task[bool]] = {}

    def register(self, dsh_session_id: str, avatar_session_id: str) -> None:
        dsh_id = validate_dsh_session(dsh_session_id)
        self._sessions[dsh_id] = validate_avatar_session(avatar_session_id)
        # Re-registering invalidates packets queued for the previous browser
        # owner, even when the opaque Avatar id happens to be reused.
        self._session_tokens[dsh_id] = object()
        self._stream_cursors.pop(dsh_id, None)
        self._stream_locks.pop(dsh_id, None)
        self._stream_tails.pop(dsh_id, None)

    def unregister(self, dsh_session_id: str, avatar_session_id: str) -> bool:
        dsh_id = validate_dsh_session(dsh_session_id)
        avatar_id = validate_avatar_session(avatar_session_id)
        if self._sessions.get(dsh_id) != avatar_id:
            return False
        del self._sessions[dsh_id]
        self._session_tokens.pop(dsh_id, None)
        self._stream_cursors.pop(dsh_id, None)
        self._stream_locks.pop(dsh_id, None)
        return True

    def avatar_session_for(self, dsh_session_id: str) -> str | None:
        try:
            return self._sessions.get(validate_dsh_session(dsh_session_id))
        except ValueError:
            return None

    def submit_wav(self, dsh_session_id: str | None, wav: bytes) -> bool:
        """Schedule a WAV upload and return immediately after validation."""

        if dsh_session_id is None or not wav or len(wav) > MAX_AVATAR_WAV_BYTES:
            return False
        avatar_id = self.avatar_session_for(dsh_session_id)
        if avatar_id is None:
            return False
        task = asyncio.create_task(self._forward(avatar_id, bytes(wav)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    def submit_pcm(
        self,
        dsh_session_id: str | None,
        pcm: bytes,
        *,
        turn_id: str,
        generation: int,
        sample_rate: int = 16000,
        end: bool = False,
    ) -> asyncio.Task[bool] | None:
        """Queue an ordered PCM packet without blocking browser playback.

        Each DSH session owns a task chain. This preserves LiveTalking packet
        order across sentence-sized HTTP requests while keeping its optional
        loopback upload off the browser PCM critical path.
        """

        if dsh_session_id is None or not pcm or len(pcm) % 2:
            return None
        try:
            dsh_id = validate_dsh_session(dsh_session_id)
        except ValueError:
            return None
        if self.avatar_session_for(dsh_id) is None:
            return None
        session_token = self._session_tokens.get(dsh_id)
        if session_token is None:
            return None
        if not isinstance(generation, int) or generation < 0:
            return None
        bounded_turn = str(turn_id).strip()
        if not bounded_turn or len(bounded_turn) > 256:
            return None
        if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 48000:
            return None

        pending = self._stream_pending.setdefault(session_token, set())
        # Reserve one slot for the logical end marker. Optional Avatar audio
        # degrades by dropping new packets instead of growing without bound or
        # applying backpressure to browser PCM.
        limit = self.queue_size if end else self.queue_size - 1
        if len(pending) >= limit:
            logger.warning(
                "LiveTalking PCM queue full; dropping optional packet "
                "(turn=%s generation=%d end=%s)",
                bounded_turn,
                generation,
                end,
            )
            return None

        previous = self._stream_tails.get(dsh_id)

        async def run_in_order() -> bool:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - optional sink stays isolated
                    logger.warning("Previous LiveTalking PCM upload failed", exc_info=True)
            return await self.forward_pcm(
                dsh_id,
                bytes(pcm),
                turn_id=bounded_turn,
                generation=generation,
                sample_rate=sample_rate,
                end=end,
                _session_token=session_token,
            )

        task = asyncio.create_task(run_in_order())
        self._stream_tails[dsh_id] = task
        self._tasks.add(task)
        pending.add(task)

        def discard(completed: asyncio.Task[bool]) -> None:
            self._tasks.discard(completed)
            pending.discard(completed)
            if not pending:
                self._stream_pending.pop(session_token, None)
            if self._stream_tails.get(dsh_id) is completed:
                self._stream_tails.pop(dsh_id, None)

        task.add_done_callback(discard)
        return task

    async def forward_pcm(
        self,
        dsh_session_id: str | None,
        pcm: bytes,
        *,
        turn_id: str,
        generation: int,
        sample_rate: int = 16000,
        end: bool = False,
        _session_token: object | None = None,
    ) -> bool:
        """Upload one ordered PCM16 packet to the registered WebRTC session.

        One cursor is retained per DSH session so independently synthesized
        sentence streams still form a continuous LiveTalking timeline.
        """

        if dsh_session_id is None or not pcm or len(pcm) % 2:
            return False
        try:
            dsh_id = validate_dsh_session(dsh_session_id)
        except ValueError:
            return False
        avatar_id = self.avatar_session_for(dsh_id)
        if avatar_id is None:
            return False
        session_token = _session_token or self._session_tokens.get(dsh_id)
        if session_token is None:
            return False
        if not isinstance(generation, int) or generation < 0:
            return False
        bounded_turn = str(turn_id).strip()
        if not bounded_turn or len(bounded_turn) > 256:
            return False
        if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 48000:
            return False

        lock = self._stream_locks.setdefault(dsh_id, asyncio.Lock())
        async with lock:
            # Registration may have changed while this packet waited.
            if self._session_tokens.get(dsh_id) is not session_token:
                return False
            cursor = self._stream_cursors.get(dsh_id)
            if cursor is None or cursor.turn_id != bounded_turn or cursor.generation != generation:
                cursor = _StreamCursor(turn_id=bounded_turn, generation=generation)
                self._stream_cursors[dsh_id] = cursor

            duration_ms = round((len(pcm) // 2) * 1000 / sample_rate)
            wav = self._pcm16_wav(pcm, sample_rate)
            metadata = {
                "X-Xiaoman-Turn-ID": bounded_turn,
                "X-Xiaoman-Generation": str(generation),
                "X-Xiaoman-Sequence": str(cursor.seq),
                "X-Xiaoman-First-Seq": str(cursor.seq),
                "X-Xiaoman-Last-Seq": str(cursor.seq),
                "X-Xiaoman-PTS-MS": str(cursor.pts_ms),
                "X-Xiaoman-Start": "true" if not cursor.started else "false",
                "X-Xiaoman-End": "true" if end else "false",
                "X-Xiaoman-Streaming": "true",
                "X-Xiaoman-Audio-Duration-MS": str(duration_ms),
            }
            try:
                await asyncio.to_thread(self._post_wav, avatar_id, wav, metadata)
            except Exception:  # noqa: BLE001 - Avatar is an optional sink
                logger.warning("LiveTalking PCM upload failed", exc_info=True)
                return False
            cursor.started = True
            cursor.seq += 1
            cursor.pts_ms += duration_ms
            logger.info(
                "LiveTalking PCM packet accepted (turn=%s seq=%d duration_ms=%d)",
                bounded_turn,
                cursor.seq - 1,
                duration_ms,
            )
            if end:
                self._stream_cursors.pop(dsh_id, None)
            return True

    @staticmethod
    def _pcm16_wav(pcm: bytes, sample_rate: int) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return output.getvalue()

    async def _forward(self, avatar_session_id: str, wav: bytes) -> None:
        try:
            await asyncio.to_thread(self._post_wav, avatar_session_id, wav)
            logger.info("LiveTalking WAV upload accepted (%d bytes)", len(wav))
        except Exception:  # noqa: BLE001 - optional sink must not fail TTS
            logger.warning("LiveTalking WAV upload failed", exc_info=True)

    def _post_wav(
        self,
        avatar_session_id: str,
        wav: bytes,
        metadata: dict[str, str] | None = None,
    ) -> None:
        boundary = f"----dsh-avatar-{secrets.token_hex(12)}"
        body = bytearray()

        def field(name: str, value: bytes, filename: str | None = None, content_type: str | None = None) -> None:
            body.extend(f"--{boundary}\r\n".encode())
            disposition = f'Content-Disposition: form-data; name="{name}"'
            if filename is not None:
                disposition += f'; filename="{filename}"'
            body.extend(f"{disposition}\r\n".encode())
            if content_type is not None:
                body.extend(f"Content-Type: {content_type}\r\n".encode())
            body.extend(b"\r\n")
            body.extend(value)
            body.extend(b"\r\n")

        field("sessionid", avatar_session_id.encode("ascii"))
        field("file", wav, filename="reply.wav", content_type="audio/wav")
        body.extend(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base_url}/humanaudio",
            data=bytes(body),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **(metadata or {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - base URL is validated loopback
            if response.status != 200:
                raise RuntimeError(f"LiveTalking returned HTTP {response.status}")
            payload = response.read(4096)
            if b'"code": 0' not in payload and b'"code":0' not in payload:
                raise RuntimeError("LiveTalking rejected the WAV upload")


__all__ = [
    "AvatarRelay",
    "MAX_AVATAR_WAV_BYTES",
    "validate_avatar_base",
    "validate_avatar_session",
    "validate_dsh_session",
]
