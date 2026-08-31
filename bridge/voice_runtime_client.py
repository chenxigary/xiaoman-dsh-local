"""Loopback client for the versioned Xiaoman v3 Voice Runtime API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx


PROTOCOL_VERSION = "xiaoman.voice-runtime.v1"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class VoiceRuntimeError(RuntimeError):
    """A safe error at the DSH-to-v3 process boundary."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_voice_runtime_base(value: str) -> str:
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
        raise ValueError("voice runtime base URL must be a loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("voice runtime base URL has an invalid port") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError("voice runtime base URL must include a valid port")
    return value.strip().rstrip("/")


class VoiceRuntimeClient:
    """Strict client; it never falls back to loading duplicate local models."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        values = dict(config or {})
        self.mode = str(values.get("mode", "v3")).strip().lower()
        if self.mode not in {"v3", "local"}:
            raise ValueError("voice runtime mode must be 'v3' or 'local'")
        self.base_url = validate_voice_runtime_base(
            str(values.get("base_url", "http://127.0.0.1:7860"))
        )
        timeout = float(values.get("request_timeout_sec", 600.0))
        connect_timeout = float(values.get("connect_timeout_sec", 2.0))
        if timeout <= 0 or connect_timeout <= 0:
            raise ValueError("voice runtime timeouts must be positive")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=False,
        )
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return self.mode == "v3"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/voice-runtime/v1{path}"

    @staticmethod
    async def _require_success(response: httpx.Response) -> None:
        if response.is_success:
            return
        await response.aread()
        status = response.status_code if response.status_code in {400, 413, 422, 503} else 502
        raise VoiceRuntimeError("v3 Voice Runtime request failed", status_code=status)

    @staticmethod
    def _require_protocol(response: httpx.Response) -> None:
        protocol = response.headers.get("X-Voice-Runtime-Protocol")
        if protocol != PROTOCOL_VERSION:
            raise VoiceRuntimeError("v3 Voice Runtime protocol mismatch", status_code=502)

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"mode": "local", "reachable": True, "ready": False}
        try:
            response = await self._client.get(self._url("/health"))
            if response.status_code not in {200, 503}:
                await response.aread()
                raise VoiceRuntimeError(
                    "v3 Voice Runtime health failed",
                    status_code=502,
                )
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("protocol_version") != PROTOCOL_VERSION:
                raise VoiceRuntimeError("v3 Voice Runtime protocol mismatch", status_code=502)
            return {
                "mode": "v3",
                "reachable": response.status_code in {200, 503},
                **payload,
            }
        except VoiceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized process boundary
            raise VoiceRuntimeError("v3 Voice Runtime is unavailable") from exc

    async def transcribe(
        self,
        body: bytes,
        *,
        content_type: str,
        trace_id: str,
        max_audio_sec: str | None = None,
        sample_rate: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": content_type or "application/octet-stream",
            "X-Voice-Trace-Id": trace_id,
        }
        if max_audio_sec:
            headers["X-Max-Audio-Sec"] = max_audio_sec
        if sample_rate:
            headers["X-Voice-Sample-Rate"] = sample_rate
        try:
            response = await self._client.post(
                self._url("/stt"),
                content=body,
                headers=headers,
            )
            await self._require_success(response)
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise VoiceRuntimeError("v3 Voice Runtime returned invalid STT data", status_code=502)
            return payload
        except VoiceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VoiceRuntimeError("v3 Voice Runtime STT failed") from exc

    async def synthesize(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> tuple[bytes, Mapping[str, str]]:
        try:
            response = await self._client.post(
                self._url("/tts"),
                json=dict(payload),
                headers={"X-Voice-Trace-Id": trace_id},
            )
            await self._require_success(response)
            self._require_protocol(response)
            if response.headers.get("content-type", "").split(";", 1)[0] != "audio/wav":
                raise VoiceRuntimeError("v3 Voice Runtime returned invalid TTS data", status_code=502)
            return response.content, response.headers
        except VoiceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VoiceRuntimeError("v3 Voice Runtime TTS failed") from exc

    @asynccontextmanager
    async def stream_tts(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
    ) -> AsyncIterator[httpx.Response]:
        context = self._client.stream(
            "POST",
            self._url("/tts/stream"),
            json=dict(payload),
            headers={"X-Voice-Trace-Id": trace_id},
        )
        entered = False
        try:
            response = await context.__aenter__()
            entered = True
            await self._require_success(response)
            self._require_protocol(response)
            yield response
        except VoiceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VoiceRuntimeError("v3 Voice Runtime TTS stream failed") from exc
        finally:
            if entered:
                await context.__aexit__(None, None, None)

    async def avatar_session(
        self,
        method: str,
        dsh_session_id: str,
        avatar_session_id: str,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                self._url("/avatar/session"),
                json={
                    "dsh_session_id": dsh_session_id,
                    "avatar_session_id": avatar_session_id,
                },
            )
            await self._require_success(response)
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("protocol_version") != PROTOCOL_VERSION:
                raise VoiceRuntimeError("v3 Voice Runtime protocol mismatch", status_code=502)
            return payload
        except VoiceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VoiceRuntimeError("v3 Voice Runtime Avatar registration failed") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "PROTOCOL_VERSION",
    "VoiceRuntimeClient",
    "VoiceRuntimeError",
    "validate_voice_runtime_base",
]
