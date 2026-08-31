from __future__ import annotations

import json
import unittest

import httpx

from voice_runtime_client import (
    PROTOCOL_VERSION,
    VoiceRuntimeClient,
    VoiceRuntimeError,
    validate_voice_runtime_base,
)


class VoiceRuntimeClientTests(unittest.IsolatedAsyncioTestCase):
    def test_base_url_is_loopback_only(self) -> None:
        self.assertEqual(
            validate_voice_runtime_base("http://127.0.0.1:7860/"),
            "http://127.0.0.1:7860",
        )
        for value in (
            "https://127.0.0.1:7860",
            "http://example.com:7860",
            "http://127.0.0.1:7860/path",
        ):
            with self.assertRaises(ValueError):
                validate_voice_runtime_base(value)

    async def test_health_requires_exact_protocol(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "protocol_version": PROTOCOL_VERSION,
                    "ready": True,
                    "tts": {"loaded": True},
                    "asr": {"loaded": True},
                },
                request=request,
            )

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        client = VoiceRuntimeClient(client=http)
        try:
            result = await client.health()
            self.assertTrue(result["reachable"])
            self.assertTrue(result["ready"])
        finally:
            await http.aclose()

    async def test_health_rejects_unexpected_http_status(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"protocol_version": PROTOCOL_VERSION},
                request=request,
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VoiceRuntimeClient(client=http)
        try:
            with self.assertRaises(VoiceRuntimeError) as rejected:
                await client.health()
            self.assertEqual(rejected.exception.status_code, 502)
        finally:
            await http.aclose()

    async def test_stt_forwards_audio_and_trace(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"text": "你好", "language": "zh", "trace_id": "trace-1"},
                request=request,
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VoiceRuntimeClient(client=http)
        try:
            result = await client.transcribe(
                bytes(320 * 2),
                content_type="application/octet-stream",
                trace_id="trace-1",
                sample_rate="16000",
            )
            self.assertEqual(result["text"], "你好")
            self.assertEqual(requests[0].url.path, "/api/voice-runtime/v1/stt")
            self.assertEqual(requests[0].headers["x-voice-trace-id"], "trace-1")
        finally:
            await http.aclose()

    async def test_stream_rejects_protocol_drift(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=bytes(320 * 2),
                headers={"X-Voice-Runtime-Protocol": "future"},
                request=request,
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VoiceRuntimeClient(client=http)
        try:
            with self.assertRaises(VoiceRuntimeError):
                async with client.stream_tts(
                    {"text": "你好"}, trace_id="trace-1"
                ):
                    pass
        finally:
            await http.aclose()

    async def test_nonstream_tts_requires_protocol_header(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"RIFFbad",
                headers={"Content-Type": "audio/wav"},
                request=request,
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VoiceRuntimeClient(client=http)
        try:
            with self.assertRaises(VoiceRuntimeError):
                await client.synthesize({"text": "你好"}, trace_id="trace-2")
        finally:
            await http.aclose()

    async def test_avatar_registration_uses_versioned_path(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"ok": True, "protocol_version": PROTOCOL_VERSION},
                request=request,
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VoiceRuntimeClient(client=http)
        try:
            await client.avatar_session("PUT", "dsh", "avatar")
            self.assertEqual(
                requests[0].url.path,
                "/api/voice-runtime/v1/avatar/session",
            )
            self.assertEqual(json.loads(requests[0].content)["dsh_session_id"], "dsh")
        finally:
            await http.aclose()


if __name__ == "__main__":
    unittest.main()
