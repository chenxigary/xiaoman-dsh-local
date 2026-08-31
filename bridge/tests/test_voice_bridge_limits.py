from __future__ import annotations

import asyncio
import unittest
import json
from types import SimpleNamespace

import numpy as np

try:
    from fastapi import HTTPException
    from bridge import voice_bridge
    from bridge.voice_bridge import (
        MAX_QQ_EVENT_BODY_BYTES,
        MAX_STT_BODY_BYTES,
        MAX_TTS_BODY_BYTES,
        MAX_VAD_FRAME_BYTES,
        VADSession,
        _bounded_request_body,
        _stt_audio_limit,
        codex_turn_interrupt,
        codex_turn_isolate,
        qq_event,
        qq_onebot_ws,
        qq_ws,
        stt,
        tts,
        tts_stream,
    )
except ModuleNotFoundError as exc:  # optional model/runtime dependencies
    raise unittest.SkipTest(f"voice bridge dependencies unavailable: {exc}") from exc

from unittest.mock import patch


class FakeRequest:
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        self.chunks = chunks
        self.headers = headers or {}
        self.consumed = False

    async def stream(self):
        self.consumed = True
        for chunk in self.chunks:
            yield chunk

    async def is_disconnected(self) -> bool:
        return False


class FakeWebSocket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.accepted = False
        self.closed: tuple[int | None, str | None] | None = None
        self.received = False

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def receive_text(self) -> str:
        self.received = True
        return ""


class VoiceBridgeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._runtime_mode = voice_bridge.VOICE_RUNTIME.mode
        voice_bridge.VOICE_RUNTIME.mode = "local"

    async def asyncTearDown(self) -> None:
        voice_bridge.VOICE_RUNTIME.mode = self._runtime_mode

    async def test_content_length_rejects_before_stream_consumption(self) -> None:
        request = FakeRequest(
            [b"must-not-read"],
            {"content-length": str(MAX_STT_BODY_BYTES + 1)},
        )
        with self.assertRaises(HTTPException) as context:
            await _bounded_request_body(request, MAX_STT_BODY_BYTES)
        self.assertEqual(context.exception.status_code, 413)
        self.assertFalse(request.consumed)

    async def test_negative_content_length_is_invalid_before_stream_consumption(self) -> None:
        request = FakeRequest([b"must-not-read"], {"content-length": "-1"})
        with self.assertRaises(HTTPException) as context:
            await _bounded_request_body(request, MAX_STT_BODY_BYTES)
        self.assertEqual(context.exception.status_code, 400)
        self.assertFalse(request.consumed)

    async def test_chunked_body_rejects_on_first_byte_over_server_cap(self) -> None:
        request = FakeRequest([b"a" * MAX_TTS_BODY_BYTES, b"b"])
        with self.assertRaises(HTTPException) as context:
            await _bounded_request_body(request, MAX_TTS_BODY_BYTES)
        self.assertEqual(context.exception.status_code, 413)

    async def test_stt_endpoint_rejects_oversized_body_before_decode_or_model(self) -> None:
        request = FakeRequest(
            [b"must-not-read"],
            {"content-length": str(MAX_STT_BODY_BYTES + 1)},
        )
        with self.assertRaises(HTTPException) as context:
            await stt(request)  # type: ignore[arg-type]
        self.assertEqual(context.exception.status_code, 413)
        self.assertFalse(request.consumed)

    async def test_tts_endpoint_rejects_chunked_oversize_before_json_parse_or_model(self) -> None:
        request = FakeRequest([b"{" + b"x" * MAX_TTS_BODY_BYTES, b"}"])
        with self.assertRaises(HTTPException) as context:
            await tts(request)  # type: ignore[arg-type]
        self.assertEqual(context.exception.status_code, 413)
        self.assertTrue(request.consumed)

    async def test_runtime_proxy_normalizes_character_and_omits_null_fields(self) -> None:
        class Runtime:
            enabled = True

            def __init__(self) -> None:
                self.payload = None

            async def synthesize(self, payload, *, trace_id):
                self.payload = payload
                return b"RIFFtest", {
                    "X-Voice-Trace-Id": trace_id,
                    "X-Voice-Runtime-Protocol": "xiaoman.voice-runtime.v1",
                }

        runtime = Runtime()
        request = FakeRequest([json.dumps({"text": "你好"}).encode()])
        with patch.object(voice_bridge, "VOICE_RUNTIME", runtime):
            response = await tts(request)  # type: ignore[arg-type]
        self.assertEqual(response.body, b"RIFFtest")
        self.assertEqual(runtime.payload["character"], "default")
        self.assertNotIn("session_id", runtime.payload)
        self.assertNotIn("turn_id", runtime.payload)

    async def test_tts_stream_yields_provider_chunks_without_wav_buffering(self) -> None:
        class Provider:
            sample_rate = 16000

            def stream(self, text, turn_id=None, cancel=None):
                self.args = (text, turn_id, cancel)
                yield SimpleNamespace(audio=np.array([0.0, 0.5], dtype=np.float32), sample_rate=16000)
                yield SimpleNamespace(audio=np.array([-0.5, 0.0], dtype=np.float32), sample_rate=16000)

        provider = Provider()
        previous = voice_bridge.models._tts
        previous_error = voice_bridge.models._tts_error
        voice_bridge.models._tts = provider
        voice_bridge.models._tts_error = None
        request = FakeRequest([
            json.dumps({"text": "你好", "turn_id": "turn-1", "generation": 2}).encode()
        ])
        try:
            response = await tts_stream(request)  # type: ignore[arg-type]
            chunks = [chunk async for chunk in response.body_iterator]
        finally:
            voice_bridge.models._tts = previous
            voice_bridge.models._tts_error = previous_error
        self.assertEqual(len(chunks), 2)
        self.assertEqual(b"".join(chunks), np.array([0, 16383, -16383, 0], dtype="<i2").tobytes())
        self.assertEqual(response.headers["x-voice-audio-format"], "pcm_s16le")
        self.assertEqual(provider.args[1], "turn-1")

    async def test_tts_stream_first_pcm_does_not_wait_for_avatar_upload(self) -> None:
        class Provider:
            sample_rate = 16000

            def stream(self, text, turn_id=None, cancel=None):
                del text, turn_id, cancel
                yield SimpleNamespace(
                    audio=np.array([0.0, 0.5], dtype=np.float32),
                    sample_rate=16000,
                )

        class BlockingAvatarRelay:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.tasks: list[asyncio.Task[bool]] = []

            def submit_pcm(self, *args, **kwargs):
                del args, kwargs

                async def upload() -> bool:
                    await self.release.wait()
                    return True

                task = asyncio.create_task(upload())
                self.tasks.append(task)
                return task

        provider = Provider()
        relay = BlockingAvatarRelay()
        request = FakeRequest([
            json.dumps(
                {
                    "text": "你好",
                    "character": "xiaoman",
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "generation": 2,
                    "end": True,
                }
            ).encode()
        ])
        previous = voice_bridge.models._tts
        previous_error = voice_bridge.models._tts_error
        voice_bridge.models._tts = provider
        voice_bridge.models._tts_error = None
        try:
            with patch.object(voice_bridge, "AVATAR_RELAY", relay):
                response = await tts_stream(request)  # type: ignore[arg-type]
                first = await asyncio.wait_for(anext(response.body_iterator), timeout=0.1)
                self.assertTrue(first)
                self.assertEqual(len(relay.tasks), 1)
                self.assertFalse(relay.tasks[0].done())
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(anext(response.body_iterator), timeout=0.1)
                self.assertEqual(len(relay.tasks), 2)
                self.assertTrue(all(not task.done() for task in relay.tasks))
                relay.release.set()
                self.assertTrue(all(await asyncio.gather(*relay.tasks)))
        finally:
            relay.release.set()
            voice_bridge.models._tts = previous
            voice_bridge.models._tts_error = previous_error

    async def test_exact_body_limit_is_accepted_without_model_or_json_parse(self) -> None:
        request = FakeRequest([b"a" * MAX_TTS_BODY_BYTES])
        body = await _bounded_request_body(request, MAX_TTS_BODY_BYTES)
        self.assertEqual(len(body), MAX_TTS_BODY_BYTES)

    def test_audio_header_can_only_lower_the_fixed_server_cap(self) -> None:
        self.assertEqual(_stt_audio_limit("999999"), 30.0)
        self.assertEqual(_stt_audio_limit("5"), 5.0)
        with self.assertRaises(HTTPException):
            _stt_audio_limit("nan")

    def test_vad_rejects_oversized_and_odd_pcm_before_buffering(self) -> None:
        session = VADSession.__new__(VADSession)
        with self.assertRaises(ValueError):
            session.feed(b"a" * (MAX_VAD_FRAME_BYTES + 1))
        with self.assertRaises(ValueError):
            session.feed(b"a")

    async def test_codex_origin_gate_precedes_large_body_parse(self) -> None:
        for endpoint in (codex_turn_interrupt, codex_turn_isolate):
            request = FakeRequest(
                [b"not-json"],
                {
                    "origin": "https://evil.example",
                    "content-length": str(16 * 1024 * 1024),
                },
            )
            with self.assertRaises(HTTPException) as context:
                await endpoint(request)  # type: ignore[arg-type]
            self.assertEqual(context.exception.status_code, 403)
            self.assertFalse(request.consumed)

    async def test_qq_disabled_rejects_http_before_body_consumption(self) -> None:
        request = FakeRequest(
            [b"must-not-read"],
            {"content-length": str(MAX_QQ_EVENT_BODY_BYTES + 1)},
        )
        with patch.dict(voice_bridge.CONFIG, {"qq": {"enabled": False}}, clear=False):
            with self.assertRaises(HTTPException) as context:
                await qq_event(request)  # type: ignore[arg-type]
        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.detail["code"], "qq_disabled")
        self.assertFalse(request.consumed)

    async def test_qq_disabled_rejects_all_inbound_websockets_before_accept(self) -> None:
        with patch.dict(voice_bridge.CONFIG, {"qq": {"enabled": False}}, clear=False):
            for endpoint in (qq_onebot_ws, qq_ws):
                socket = FakeWebSocket()
                await endpoint(socket)  # type: ignore[arg-type]
                self.assertFalse(socket.accepted)
                self.assertEqual(socket.closed, (1008, "QQ bridge disabled or unauthorized"))
                self.assertFalse(socket.received)

    async def test_qq_owner_and_origin_gate_rejects_unauthorized_enabled_inputs(self) -> None:
        request = FakeRequest([b"must-not-read"], {"origin": "https://evil.example"})
        socket = FakeWebSocket({"origin": "https://evil.example"})
        with patch.dict(
            voice_bridge.CONFIG,
            {"qq": {"enabled": True, "owner_token": "known-owner"}},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as context:
                await qq_event(request)  # type: ignore[arg-type]
            await qq_onebot_ws(socket)  # type: ignore[arg-type]
        self.assertEqual(context.exception.status_code, 403)
        self.assertFalse(request.consumed)
        self.assertFalse(socket.accepted)
        self.assertEqual(socket.closed, (1008, "QQ bridge disabled or unauthorized"))
