"""
Voice bridge — keep the DSH browser/agent API stable while proxying heavy voice
work to the authoritative Xiaoman v3 Voice Runtime.

The default v3 mode does not load MLX models in this process. DSH continues to
own Silero VAD, media, and UI orchestration; STT/TTS/Avatar audio cross the
loopback-only ``xiaoman.voice-runtime.v1`` boundary. This private distribution
is local-only: DeepSeek routes are disabled by the DSH overlay and the Codex
subscription boundary is hard-disabled here. The copied voice providers remain
available only through explicit ``mode=local`` rollback.

Run (macOS/Linux):
  .venv/bin/python -m uvicorn voice_bridge:app \
      --app-dir bridge --host 127.0.0.1 --port 8765

The Windows ``.cmd`` launchers remain available for the original setup.  The
bridge itself is platform-neutral: relative paths are rooted at the checkout
and ``device: auto`` selects CUDA, Apple MPS, or CPU at runtime.
"""

from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import math
import os
import shlex
import threading
import warnings
from pathlib import Path
from typing import AsyncIterator, Literal, Mapping

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.codex import (
    AppServerClient,
    AppServerConfig,
    CodexAgentService,
    CodexAuthService,
    CodexBusyError,
    CodexCompatibilityError,
    CodexError,
    CodexProcessError,
    CodexTimeoutError,
    ChatgptSubscriptionBroker,
    ThreadManager,
    ThreadMappingStore,
    managed_auth_file,
    prepare_isolated_home,
)
from agents.codex.compatibility import EXPECTED_CLI_VERSION

try:
    from .latency import LatencyConfig, LatencyRecorder, new_trace_id
    from .character_registry import CHARACTERS, normalize_character, state_media
    from .avatar_relay import AvatarRelay
    from .voice_runtime_client import VoiceRuntimeClient, VoiceRuntimeError
    from .xiaoman_v3_adapters.registry import ProviderRegistry, load_provider_config
except ImportError:  # uvicorn imports ``voice_bridge`` with bridge/ on sys.path
    from latency import LatencyConfig, LatencyRecorder, new_trace_id
    from character_registry import CHARACTERS, normalize_character, state_media
    from avatar_relay import AvatarRelay
    from voice_runtime_client import VoiceRuntimeClient, VoiceRuntimeError
    from xiaoman_v3_adapters.registry import ProviderRegistry, load_provider_config

HERE = Path(__file__).resolve().parent
# Repo root: this file lives in <repo>/bridge/, so relative paths in
# bridge-config.json are resolved against the repo root (e.g. media dirs,
# ref_audio.wav). Absolute paths pass through untouched.
REPO_ROOT = HERE.parent
CONFIG_PATH = Path(os.environ.get("VOICE_BRIDGE_CONFIG", HERE / "bridge-config.json"))
EXAMPLE_CONFIG_PATH = HERE / "bridge-config.example.json"
LOCAL_ONLY_BUILD = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("voice_bridge")

# Request/stream limits are server-owned security caps.  Caller-provided
# headers may lower a limit but can never raise it.
MAX_STT_BODY_BYTES = 1_048_576  # 30 s PCM16 at 16 kHz plus a small WAV header
MAX_STT_AUDIO_SECONDS = 30.0
MAX_TTS_BODY_BYTES = 64 * 1024
MAX_TTS_TEXT_CHARS = 512
MAX_TTS_RESPONSE_BYTES = 4 * 1024 * 1024
TTS_STREAM_SAMPLE_RATE = 16000
MAX_VAD_FRAME_BYTES = 4096
MAX_VAD_TOTAL_BYTES = 8 * 1024 * 1024
MAX_VAD_BYTES_PER_SECOND = 128 * 1024
MAX_QQ_EVENT_BODY_BYTES = 64 * 1024
MAX_QQ_WS_FRAME_BYTES = 64 * 1024


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    """Read an HTTP body with a fixed cap, including chunked requests."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
            if declared < 0:
                raise HTTPException(status_code=400, detail="Invalid Content-Length")
            if declared > maximum:
                raise HTTPException(status_code=413, detail="Request body exceeds the server limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(status_code=413, detail="Request body exceeds the server limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _stt_audio_limit(header_value: str | None) -> float:
    """Apply a caller limit without allowing it to raise the server cap."""

    try:
        requested = float(header_value) if header_value else MAX_STT_AUDIO_SECONDS
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid X-Max-Audio-Sec") from exc
    if not math.isfinite(requested):
        raise HTTPException(status_code=400, detail="Invalid X-Max-Audio-Sec")
    return min(MAX_STT_AUDIO_SECONDS, max(0.0, requested))


def _resolve_path(value: str) -> str:
    """Resolve a relative path against the repo root; absolute paths unchanged."""
    # ``$VAR``/``${VAR}`` keeps a local config portable without hard-coding a
    # machine-specific model or reference-audio path.
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    # ``Path.is_absolute`` follows the host OS.  Keep a Windows drive path
    # intact when a config is copied to macOS/Linux so it does not become a
    # nonsensical ``<repo>/C:/...`` path.
    if path.is_absolute() or (
        len(expanded) >= 3
        and expanded[1] == ":"
        and expanded[2] in {"/", "\\"}
    ):
        return expanded
    return str((REPO_ROOT / path).resolve())


def _reject_nonfinite_config_number(_value: str) -> None:
    raise ValueError("bridge config contains a non-finite JSON number")


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    """Load bridge-config.json and normalize relative paths.

    Only TTS model / ref audio / media directories are resolved (they point
    at local files). The STT model_name stays untouched — it is a HuggingFace
    model id (e.g. openai/whisper-large-v3) and must NOT be path-resolved.
    """
    # A fresh checkout intentionally does not contain the user-specific
    # bridge-config.json.  Falling back to the checked-in example lets the
    # health/media endpoints and startup smoke checks work before model setup;
    # model requests still return a useful load error until the user supplies
    # real model paths.
    source = config_path if config_path.is_file() else EXAMPLE_CONFIG_PATH
    if not source.is_file():
        raise FileNotFoundError(
            f"No bridge config found at {config_path} or {EXAMPLE_CONFIG_PATH}"
        )
    with open(source, encoding="utf-8") as f:
        cfg = json.load(f, parse_constant=_reject_nonfinite_config_number)

    # FunASR STT model is a LOCAL directory in this repo (models/funasr/...)
    # — resolve it relative to the repo root like the other paths.
    if cfg.get("stt", {}).get("backend") == "funasr" and cfg["stt"].get("model_name"):
        cfg["stt"]["model_name"] = _resolve_path(cfg["stt"]["model_name"])

    tts = cfg.setdefault("tts", {})
    if tts.get("model_name") and tts.get("backend", "upstream-qwen3") not in {
        "xiaoman",
        "qwen3",
        "omnivoice",
        "qwen3-adapter",
        "omnivoice-adapter",
    }:
        tts["model_name"] = _resolve_path(tts["model_name"])
    if tts.get("ref_audio"):
        tts["ref_audio"] = _resolve_path(tts["ref_audio"])

    media = cfg.setdefault("media", {})
    media.setdefault("bg_images_dir", "assets/bg-images")
    media.setdefault("task_videos_dir", "assets/task-videos")
    for key in ("bg_images_dir", "task_videos_dir"):
        if media.get(key):
            media[key] = _resolve_path(media[key])

    codex = cfg.setdefault("codex", {})
    # This repository is the private local-only distribution. Keep the old
    # protocol implementation and tests for auditability, but make it
    # impossible for a copied config to start a subscription-backed process.
    codex["enabled"] = False
    codex.setdefault("command", ["codex", "app-server", "--stdio"])
    codex.setdefault("runtime_state", "runtime/codex-thread-map.json")
    codex.setdefault("workspace", ".")
    codex.setdefault("startup_timeout_sec", 15.0)
    codex.setdefault("request_timeout_sec", 30.0)
    codex.setdefault("turn_timeout_sec", 1800.0)
    codex.setdefault("subscriber_queue_size", 256)
    # Coding-capable sandboxes remain disabled until a typed approval gateway
    # is shipped.  Never let a copied config silently opt into writes.
    codex["sandbox"] = "read-only"
    codex.setdefault("approval_policy", "never")
    codex.setdefault("expected_cli_version", EXPECTED_CLI_VERSION)
    if codex.get("runtime_state"):
        codex["runtime_state"] = _resolve_path(str(codex["runtime_state"]))
    if codex.get("workspace"):
        codex["workspace"] = _resolve_path(str(codex["workspace"]))

    xiaoman = cfg.setdefault("xiaoman", {})
    xiaoman.setdefault("enabled", False)
    xiaoman["character"] = normalize_character(xiaoman.get("character"))
    if xiaoman.get("provider_config"):
        xiaoman["provider_config"] = _resolve_path(str(xiaoman["provider_config"]))

    voice_runtime = cfg.setdefault("voice_runtime", {})
    voice_runtime["mode"] = os.environ.get(
        "DSH_VOICE_RUNTIME_MODE",
        str(voice_runtime.get("mode", "v3")),
    ).strip().lower()
    voice_runtime["base_url"] = os.environ.get(
        "DSH_VOICE_RUNTIME_URL",
        str(voice_runtime.get("base_url", "http://127.0.0.1:7860")),
    ).strip()
    voice_runtime.setdefault("connect_timeout_sec", 2.0)
    voice_runtime.setdefault("request_timeout_sec", 600.0)

    return cfg


CONFIG = load_config()
XIAOMAN_REGISTRY = ProviderRegistry(
    load_provider_config(CONFIG.get("xiaoman", {}).get("provider_config"))
    if CONFIG.get("xiaoman", {}).get("provider_config")
    else None
)


def _available_torch_devices() -> set[str]:
    """Return accelerators available to the installed PyTorch build."""

    try:
        import torch
    except Exception:  # noqa: BLE001 - torch is an optional lazy dependency
        return set()

    devices: set[str] = set()
    try:
        if bool(torch.cuda.is_available()):
            devices.add("cuda")
    except Exception:  # noqa: BLE001 - defensive around stripped-down builds
        pass
    try:
        if bool(torch.backends.mps.is_available()):
            devices.add("mps")
    except (AttributeError, RuntimeError):
        pass
    return devices


def resolve_device(requested: str | None) -> str:
    """Resolve ``auto`` and unavailable CUDA requests for the current host.

    Existing Windows configs often say ``cuda``.  On macOS, silently keeping
    that value causes a model-load failure before the service can report
    health.  We preserve explicit non-CUDA values and gracefully choose MPS
    or CPU when CUDA is unavailable.
    """

    value = (requested or "auto").strip().lower()
    available = _available_torch_devices()
    if value in {"", "auto"}:
        if "cuda" in available:
            return "cuda"
        if "mps" in available:
            return "mps"
        return "cpu"
    if value == "cuda" and "cuda" not in available:
        fallback = "mps" if "mps" in available else "cpu"
        warnings.warn(
            f"CUDA was requested but is unavailable; falling back to {fallback}",
            RuntimeWarning,
            stacklevel=2,
        )
        return fallback
    if value == "mps" and "mps" not in available:
        warnings.warn(
            "Apple MPS was requested but is unavailable; falling back to CPU",
            RuntimeWarning,
            stacklevel=2,
        )
        return "cpu"
    return value


def resolve_dtype(requested: str | None, device: str) -> str:
    """Use a CPU-safe dtype when ``auto`` or an old CUDA config is present."""

    value = (requested or "auto").strip().lower()
    if value in {"", "auto"}:
        return "float16" if device in {"cuda", "mps"} else "float32"
    if device == "cpu" and value in {"float16", "half", "fp16"}:
        return "float32"
    return value

app = FastAPI(title="voice-bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONFIG.get("cors_origins", ["http://127.0.0.1:3080"]),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Voice-Trace-Id",
        "X-Voice-Audio-Format",
        "X-Voice-Sample-Rate",
        "X-Voice-Channels",
    ],
)

LATENCY = LatencyRecorder(
    LatencyConfig.from_mapping(CONFIG.get("latency")),
    logger=logging.getLogger("voice.latency"),
)
AVATAR_RELAY = AvatarRelay()
VOICE_RUNTIME = VoiceRuntimeClient(CONFIG.get("voice_runtime"))

# Codex is intentionally constructed lazily.  Importing the bridge or probing
# /api/health must not spawn a subscription-consuming app-server process.
_CODEX_LOCK = asyncio.Lock()
_CODEX_CLIENT: AppServerClient | None = None
_CODEX_AUTH_CLIENT: AppServerClient | None = None
_CODEX_AUTH: CodexAuthService | None = None
_CODEX_AGENT: CodexAgentService | None = None
_CODEX_BROKER: ChatgptSubscriptionBroker | None = None
_CODEX_SHUTTING_DOWN = False
# Defense in depth for the local-only distribution. Even a unit-test-style
# replacement of CONFIG cannot make the production WebSocket start a turn.
CODEX_TURN_EXECUTION_ENABLED = False


def _codex_command() -> tuple[str, ...]:
    raw = os.environ.get("CODEX_APP_SERVER_COMMAND")
    if raw is None:
        raw = CONFIG.get("codex", {}).get("command", ["codex", "app-server", "--stdio"])
    if isinstance(raw, str):
        command = tuple(shlex.split(raw))
    elif isinstance(raw, (list, tuple)):
        command = tuple(str(part) for part in raw if str(part))
    else:
        command = ("codex", "app-server", "--stdio")
    if not command:
        raise ValueError("Codex app-server command must not be empty")
    return command


def _codex_runtime_state() -> Path:
    raw = os.environ.get(
        "DSH_CODEX_RUNTIME_STATE",
        str(CONFIG.get("codex", {}).get("runtime_state", REPO_ROOT / "runtime/codex-thread-map.json")),
    )
    return Path(_resolve_path(raw))


def _codex_workspace() -> Path:
    raw = os.environ.get(
        "DSH_CODEX_WORKSPACE",
        str(CONFIG.get("codex", {}).get("workspace", REPO_ROOT)),
    )
    workspace = Path(_resolve_path(raw)).resolve()
    if not workspace.is_dir():
        raise ValueError("configured Codex workspace is not a directory")
    return workspace


def _codex_execution_home() -> Path:
    raw = os.environ.get(
        "DSH_CODEX_EXECUTION_HOME",
        str(CONFIG.get("codex", {}).get("execution_home", REPO_ROOT / "runtime/codex-execution-home")),
    )
    home = Path(_resolve_path(raw))
    auth_path = managed_auth_file()
    resolved = home.expanduser().resolve(strict=False)
    if resolved == auth_path.parent or resolved in auth_path.parents:
        raise CodexError("Codex execution home overlaps managed credentials", code="security_isolation_unavailable")
    return prepare_isolated_home(resolved)


def _production_codex_version(cfg: Mapping[str, object]) -> str:
    """Reject production version overrides before constructing AppServerClient."""

    if cfg.get("expected_cli_version") != EXPECTED_CLI_VERSION:
        raise CodexCompatibilityError(
            "unsupported Codex app-server CLI version configuration",
            code="codex_version_unsupported",
        )
    return EXPECTED_CLI_VERSION


def _codex_bounded_number(
    cfg: Mapping[str, object],
    key: str,
    default: float,
    maximum: float,
) -> float:
    """Validate one raw JSON number without Python's bool coercion."""

    value = cfg.get(key, default)
    finite = False
    if type(value) in {int, float}:
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
    if not finite or not 0 < value <= maximum:
        raise ValueError("Codex numeric configuration is outside the audited bound")
    return float(value)


def _codex_bounded_integer(
    cfg: Mapping[str, object],
    key: str,
    default: int,
    maximum: int,
) -> int:
    """Validate one raw JSON integer; bool and integral floats are invalid."""

    value = cfg.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Codex integer configuration is outside the audited bound")
    return value


def _codex_origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    # Codex turn/control is a Host-only seam. A Node Host WebSocket has no
    # Origin header; any browser-originated socket is rejected even when the
    # ordinary STT/TTS CORS allow-list contains that origin.
    return not bool(origin)


async def _ensure_codex() -> tuple[AppServerClient, CodexAuthService, CodexAgentService]:
    """Create one app-server/client/service bundle, but start it only on use."""

    global _CODEX_CLIENT, _CODEX_AUTH_CLIENT, _CODEX_AUTH, _CODEX_AGENT, _CODEX_BROKER
    async with _CODEX_LOCK:
        if _CODEX_SHUTTING_DOWN:
            raise CodexError("Codex bridge is shutting down", code="shutting_down")
        raw_cfg = CONFIG.get("codex", {})
        if not isinstance(raw_cfg, Mapping):
            raise ValueError("Codex configuration must be an object")
        cfg = raw_cfg
        enabled = cfg.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("Codex enabled configuration must be boolean")
        if not enabled:
            raise CodexError("Codex is disabled", code="codex_disabled")
        expected_cli_version = _production_codex_version(cfg)
        startup_timeout = _codex_bounded_number(
            cfg, "startup_timeout_sec", 15.0, 120.0
        )
        request_timeout = _codex_bounded_number(
            cfg, "request_timeout_sec", 30.0, 300.0
        )
        shutdown_timeout = _codex_bounded_number(
            cfg, "shutdown_timeout_sec", 3.0, 30.0
        )
        turn_timeout = _codex_bounded_number(
            cfg, "turn_timeout_sec", 1800.0, 7200.0
        )
        subscriber_queue_size = _codex_bounded_integer(
            cfg, "subscriber_queue_size", 256, 4096
        )
        if (
            _CODEX_CLIENT is not None
            and _CODEX_AUTH_CLIENT is not None
            and _CODEX_AUTH is not None
            and _CODEX_AGENT is not None
            and _CODEX_BROKER is not None
        ):
            return _CODEX_CLIENT, _CODEX_AUTH, _CODEX_AGENT
        auth_client = AppServerClient(
            AppServerConfig(
                command=_codex_command(),
                startup_timeout=startup_timeout,
                request_timeout=request_timeout,
                shutdown_timeout=shutdown_timeout,
                subscriber_queue_size=subscriber_queue_size,
                expected_cli_version=expected_cli_version,
                managed_token_refresh=True,
            )
        )
        execution_home = _codex_execution_home()
        broker = ChatgptSubscriptionBroker(
            auth_client,
            managed_auth_file(),
            execution_home,
        )
        client = AppServerClient(
            AppServerConfig(
                command=_codex_command(),
                startup_timeout=startup_timeout,
                request_timeout=request_timeout,
                shutdown_timeout=shutdown_timeout,
                subscriber_queue_size=subscriber_queue_size,
                expected_cli_version=expected_cli_version,
                external_chatgpt_auth=True,
                isolated_home=str(execution_home),
            ),
            post_initialize=broker.bootstrap,
            chatgpt_token_refresh=broker.refresh,
        )
        manager = ThreadManager(
            client,
            ThreadMappingStore(_codex_runtime_state()),
            sandbox="read-only",
            approval_policy="never",
        )
        auth = CodexAuthService(auth_client)
        agent = CodexAgentService(
            client,
            manager,
            turn_timeout=turn_timeout,
            event_queue_size=subscriber_queue_size,
        )
        _CODEX_CLIENT = client
        _CODEX_AUTH_CLIENT = auth_client
        _CODEX_AUTH = auth
        _CODEX_AGENT = agent
        _CODEX_BROKER = broker
        return client, auth, agent


def _codex_error_payload(exc: Exception) -> tuple[int, dict[str, str]]:
    if isinstance(exc, CodexBusyError):
        return 409, {"code": "turn_in_progress", "message": "a Codex turn is already active"}
    if isinstance(exc, CodexTimeoutError):
        return 504, {"code": "timeout", "message": "Codex operation timed out"}
    if isinstance(exc, CodexProcessError):
        return 503, {"code": "app_server_unavailable", "message": "Codex app-server is unavailable"}
    if isinstance(exc, CodexCompatibilityError):
        return 503, {"code": "codex_version_unsupported", "message": "Codex app-server protocol is unsupported"}
    if isinstance(exc, CodexError):
        # Never forward app-server error strings: they can contain prompt,
        # cwd, command, account, or provider material.  Only this finite set
        # of boundary-owned codes is visible to a browser.
        safe_errors = {
            "turn_not_found": (404, "turn not found"),
            "codex_disabled": (404, "Codex is disabled"),
            "shutting_down": (503, "Codex bridge is shutting down"),
            "turn_in_progress": (409, "a Codex turn is already active"),
            "invalid_response": (502, "Codex returned an invalid response"),
            "thread_ownership_conflict": (409, "Codex thread ownership conflict"),
            "mapping_commit_failed": (503, "Codex thread mapping requires reconciliation"),
            "login_in_progress": (409, "a browser login is already pending"),
            "login_operation_conflict": (409, "browser login operation conflicts with an existing flow"),
            "login_operation_capacity": (503, "browser login operation capacity is exhausted"),
            "interrupt_isolated": (409, "Codex turn was isolated"),
            "isolation_failed": (503, "Codex process isolation failed"),
            "security_isolation_unavailable": (503, "Codex turn execution is unavailable"),
        }
        raw_code = exc.code if isinstance(exc.code, (str, int)) else None
        code = str(raw_code) if raw_code in safe_errors else "codex_error"
        status, message = safe_errors.get(code, (502, "Codex operation failed"))
        return status, {"code": code, "message": message}
    if isinstance(exc, ValueError):
        return 400, {"code": "invalid_request", "message": "invalid request"}
    return 500, {"code": "internal_error", "message": "internal Codex bridge error"}


@app.on_event("shutdown")
async def _shutdown_codex() -> None:
    """Close the direct backend before uvicorn exits its event loop."""

    global _CODEX_CLIENT, _CODEX_AUTH_CLIENT, _CODEX_AUTH, _CODEX_AGENT, _CODEX_BROKER, _CODEX_SHUTTING_DOWN
    _CODEX_SHUTTING_DOWN = True
    async with _CODEX_LOCK:
        client, auth_client, auth, agent = _CODEX_CLIENT, _CODEX_AUTH_CLIENT, _CODEX_AUTH, _CODEX_AGENT
        _CODEX_CLIENT = _CODEX_AUTH = _CODEX_AGENT = None
        _CODEX_AUTH_CLIENT = None
        _CODEX_BROKER = None
    shutdown_timeout = 3.0
    if client is not None:
        configured_timeout = getattr(getattr(client, "config", None), "shutdown_timeout", None)
        if isinstance(configured_timeout, (int, float)) and math.isfinite(configured_timeout):
            shutdown_timeout = max(0.1, min(float(configured_timeout), 30.0))
    failures: list[str] = []
    cancelled = False
    stages = (
        ("agent", agent, (shutdown_timeout * 2) + 1.0),
        ("auth", auth, 1.0),
        ("client", client, shutdown_timeout + 1.0),
        ("auth_client", auth_client, shutdown_timeout + 1.0),
    )
    for name, service, timeout in stages:
        if service is None:
            continue
        try:
            await asyncio.wait_for(service.close(), timeout=timeout)
        except asyncio.CancelledError:
            # Cancellation must not skip the exact AppServerClient close
            # stage. Preserve it and re-raise only after every owner ran.
            cancelled = True
            failures.append(name)
        except BaseException:  # noqa: BLE001 - shutdown must reach all stages
            failures.append(name)
    if cancelled:
        raise asyncio.CancelledError
    if failures:
        # Stage names and underlying exception text are deliberately not
        # reflected; shutdown errors can carry local paths or process detail.
        raise RuntimeError("Codex shutdown cleanup failed")


@app.on_event("shutdown")
async def _shutdown_voice_runtime_client() -> None:
    await VOICE_RUNTIME.close()


class ModelManager:
    """Owns lazily-loaded handlers for explicit local rollback mode.

    Handlers are loaded on first use (heavy: whisper-large-v3 + qwen3-tts,
    plus TTS warmup ~10-60s), guarded by a lock so concurrent requests queue
    instead of double-loading. A shared `infer_lock` serializes ALL model
    work (STT + TTS share the one GPU; single-user local service).
    """

    def __init__(self) -> None:
        self._stt = None
        self._tts = None
        self._stt_error: str | None = None
        self._tts_error: str | None = None
        self._load_lock = asyncio.Lock()
        # Serializes every model inference call (STT + TTS) on the shared GPU.
        self.infer_lock = asyncio.Lock()

    @property
    def stt_ready(self) -> bool:
        return self._stt is not None

    @property
    def tts_ready(self) -> bool:
        return self._tts is not None

    @property
    def stt_error(self) -> str | None:
        return self._stt_error

    @property
    def tts_error(self) -> str | None:
        return self._tts_error

    async def ensure_stt(self):
        """Lazily load the Whisper STT handler once (thread off the event loop)."""
        async with self._load_lock:
            if self._stt is not None:
                return self._stt
            if self._stt_error is not None:
                raise HTTPException(status_code=503, detail="STT model is unavailable")
            try:
                self._stt = await asyncio.to_thread(_load_stt_handler)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                logger.exception("STT model load failed")
                self._stt_error = "model_load_failed"
                raise HTTPException(status_code=503, detail="STT model is unavailable") from exc
        return self._stt

    async def ensure_tts(self):
        """Lazily load the Qwen3 TTS handler once (T3)."""
        async with self._load_lock:
            if self._tts is not None:
                return self._tts
            if self._tts_error is not None:
                raise HTTPException(status_code=503, detail="TTS model is unavailable")
            try:
                self._tts = await asyncio.to_thread(_load_tts_handler)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                logger.exception("TTS model load failed")
                self._tts_error = "model_load_failed"
                raise HTTPException(status_code=503, detail="TTS model is unavailable") from exc
        return self._tts


def _load_stt_handler():
    """Instantiate the configured STT backend: 'funasr' (Chinese ASR, default
    when configured), a migrated xiaoman adapter, or the original
    WhisperSTTHandler fallback."""
    backend = CONFIG["stt"].get("backend", "whisper")
    if backend == "funasr":
        return _load_funasr_handler()
    if backend in {"xiaoman", "mac-mlx-whisper", "legacy-whisper"}:
        selection = CONFIG.get("xiaoman", {}).get("stt_provider") if backend == "xiaoman" else backend
        return XIAOMAN_REGISTRY.create_stt(selection, **dict(CONFIG["stt"]))

    from queue import Empty, Queue
    from threading import Event

    from speech_to_speech.STT.whisper_stt_handler import WhisperSTTHandler

    cfg = dict(CONFIG["stt"])
    cfg.pop("backend", None)
    handler = WhisperSTTHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(),
        setup_kwargs=cfg,
    )
    return handler


def _load_funasr_handler():
    """Lazily load the FunASR Chinese ASR model (Paraformer-large, 16k).

    Returns the funasr AutoModel; transcribing goes through _transcribe_funasr.
    The FunASR AutoModel caches its own singleton, so repeated loads are cheap.
    """
    from funasr import AutoModel

    model_name = CONFIG["stt"].get(
        "model_name",
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    device = resolve_device(CONFIG["stt"].get("device", "auto"))
    dtype = resolve_dtype(CONFIG["stt"].get("torch_dtype", "auto"), device)
    return AutoModel(
        model=model_name,
        trust_remote_code=True,
        device=device,
        dtype=dtype,
    )


def _load_tts_handler():
    """Instantiate Qwen3TTSHandler with bridge-config.json['tts'] settings (T3)."""
    from queue import Queue
    from threading import Event

    tts_backend = CONFIG["tts"].get("backend", "upstream-qwen3")
    if tts_backend in {"xiaoman", "qwen3", "omnivoice", "qwen3-adapter", "omnivoice-adapter"}:
        selection = CONFIG.get("xiaoman", {}).get("tts_provider") if tts_backend == "xiaoman" else tts_backend.replace("-adapter", "")
        return XIAOMAN_REGISTRY.create_tts(selection, **dict(CONFIG["tts"]))

    from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler

    cfg = dict(CONFIG["tts"])
    cfg["device"] = resolve_device(cfg.get("device", "auto"))
    if "dtype" in cfg:
        cfg["dtype"] = resolve_dtype(cfg.get("dtype"), cfg["device"])
    handler = Qwen3TTSHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(Event(),),  # should_listen
        setup_kwargs=cfg,
    )
    return handler


def decode_audio(body: bytes, content_type: str) -> np.ndarray:
    """Decode request audio to float32 mono at 16 kHz.

    Accepts WAV (any rate/channels soundfile can read) or raw little-endian
    16-bit PCM mono at 16 kHz (the mic-capture worklet output)."""
    if content_type == "audio/wav" or body[:4] == b"RIFF":
        import soundfile as sf

        data, sr = sf.read(io.BytesIO(body), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
    else:
        raw = np.frombuffer(body, dtype="<i2")
        data = raw.astype(np.float32) / 32768.0
        sr = 16000
    if sr != 16000:
        from scipy.signal import resample_poly

        gcd = int(np.gcd(sr, 16000))
        data = resample_poly(data, up=16000 // gcd, down=sr // gcd)
    return np.ascontiguousarray(data, dtype=np.float32)


def _transcribe(handler, audio: np.ndarray) -> tuple[str, str | None]:
    if CONFIG["stt"].get("backend", "whisper") == "funasr":
        return _transcribe_funasr(handler, audio)

    # Migrated adapters expose the normalized provider boundary directly.
    if hasattr(handler, "transcribe"):
        result = handler.transcribe(audio, sample_rate=16000)
        return str(getattr(result, "text", "") or ""), str(getattr(result, "language", "") or "zh")

    from speech_to_speech.pipeline.messages import VADAudio

    try:
        transcription = next(iter(handler.process(VADAudio(audio=audio))))
    except IndexError:
        # Upstream whisper handler assumes >= 2 generated tokens (language
        # token + content) and reads pred_ids[0, 1]; a near-silent or very
        # short utterance can produce a single token and crash. Guard: treat
        # it as an empty transcription so continuous listening never breaks.
        logger.warning("STT: whisper returned a degenerate (1-token) generation; treating as empty")
        return "", None
    return transcription.text, transcription.language_code


def _transcribe_funasr(model, audio: np.ndarray) -> tuple[str, str | None]:
    """Transcribe 16 kHz mono float32 audio with the FunASR model."""
    try:
        result = model.generate(input=audio, cache={})
        text = (result[0].get("text") or "").strip() if result else ""
        if not text:
            logger.warning("STT: funasr returned empty result; treating as empty")
        return text, "zh"
    except Exception:  # noqa: BLE001 - surfaced to the client
        logger.exception("STT: funasr transcribe failed")
        return "", None


models = ModelManager()


@app.get("/api/health")
async def health() -> dict:
    """Bridge health plus authoritative v3 readiness or local fallback state."""
    runtime: dict[str, Any]
    if VOICE_RUNTIME.enabled:
        try:
            runtime = await VOICE_RUNTIME.health()
        except VoiceRuntimeError as exc:
            runtime = {
                "mode": "v3",
                "reachable": False,
                "ready": False,
                "error": str(exc),
            }
    else:
        runtime = {"mode": "local", "reachable": True, "ready": False}
    remote_tts = runtime.get("tts", {}) if isinstance(runtime.get("tts"), Mapping) else {}
    remote_asr = runtime.get("asr", {}) if isinstance(runtime.get("asr"), Mapping) else {}
    return {
        "status": "ok",
        "local_only": LOCAL_ONLY_BUILD,
        "stt": bool(remote_asr.get("loaded")) if VOICE_RUNTIME.enabled else models.stt_ready,
        "tts": bool(remote_tts.get("loaded")) if VOICE_RUNTIME.enabled else models.tts_ready,
        "stt_error": models.stt_error,
        "tts_error": models.tts_error,
        "voice_runtime": runtime,
        "device": (
            {"stt": "voice-runtime", "tts": "voice-runtime"}
            if VOICE_RUNTIME.enabled
            else {
                "stt": resolve_device(CONFIG.get("stt", {}).get("device", "auto")),
                "tts": resolve_device(CONFIG.get("tts", {}).get("device", "auto")),
            }
        ),
        "latency": {
            "enabled": LATENCY.config.enabled,
            "sample_rate": LATENCY.config.sample_rate,
        },
        "characters": sorted(CHARACTERS),
        "character_default": CONFIG.get("xiaoman", {}).get("character", "default"),
        "xiaoman": XIAOMAN_REGISTRY.health(),
    }


def _require_codex_host(request: Request) -> None:
    """Reject browser-originated Codex control/auth calls at the HTTP edge."""

    if request.headers.get("origin"):
        raise HTTPException(status_code=403, detail={"code": "host_only", "message": "Codex control is host-only"})


def _unique_codex_request_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Codex request contains a duplicate key")
        value[key] = item
    return value


def _reject_codex_request_constant(_value: str) -> None:
    raise ValueError("Codex request contains a non-finite number")


def _decode_codex_request(body: bytes) -> dict[str, object]:
    value = json.loads(
        body,
        object_pairs_hook=_unique_codex_request_object,
        parse_constant=_reject_codex_request_constant,
    )
    if type(value) is not dict:
        raise ValueError("Codex request must be an object")
    return value


class CodexLoginStartRequest(BaseModel):
    """Exact Host-only idempotency contract for browser login start."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )


@app.get("/api/codex/health")
async def codex_health(request: Request) -> dict:
    """Non-starting Codex process/thread health probe."""

    _require_codex_host(request)

    if not bool(CONFIG.get("codex", {}).get("enabled", True)):
        return {"status": "disabled", "codex": {"enabled": False, "started": False}}
    if _CODEX_AGENT is None:
        return {
            "status": "ok",
            "codex": {
                "enabled": True,
                "started": False,
                "pending_requests": 0,
                "active_turns": 0,
                "thread_mappings": 0,
                "last_error": None,
                "protocol": {"cli_version": None, "schema_sha256": None},
            },
        }
    return {"status": "ok", "codex": await _CODEX_AGENT.health()}


@app.get("/api/codex/auth")
async def codex_auth(request: Request) -> dict:
    """Read allow-listed account state; credentials never cross this API."""

    _require_codex_host(request)

    if not bool(CONFIG.get("codex", {}).get("enabled", True)):
        raise HTTPException(status_code=404, detail={"code": "codex_disabled", "message": "Codex is disabled"})
    try:
        _client, auth, _agent = await _ensure_codex()
        return await auth.account_read()
    except Exception as exc:  # noqa: BLE001 - map at the HTTP boundary
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.get("/api/codex/models")
async def codex_models(request: Request) -> dict:
    """Host-only, bounded projection of the live App Server model catalog."""

    _require_codex_host(request)
    try:
        _client, _auth, agent = await _ensure_codex()
        return await agent.list_models()
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.post("/api/codex/auth/login/start")
async def codex_login_start(request: Request) -> dict:
    """Start/reconcile one idempotent official browser ChatGPT login flow."""

    _require_codex_host(request)

    try:
        body = await _bounded_request_body(request, 256)
        req = CodexLoginStartRequest.model_validate(_decode_codex_request(body))
        _client, auth, _agent = await _ensure_codex()
        state = await auth.login_start(req.operation_id)
        return {"operation_id": req.operation_id, **state.to_dict()}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.get("/api/codex/auth/login/{login_id}")
async def codex_login_status(login_id: str, request: Request) -> dict:
    _require_codex_host(request)
    try:
        _client, auth, _agent = await _ensure_codex()
        return (await auth.login_status(login_id)).to_dict()
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.post("/api/codex/auth/login/{login_id}/cancel")
async def codex_login_cancel(login_id: str, request: Request) -> dict:
    _require_codex_host(request)
    try:
        _client, auth, _agent = await _ensure_codex()
        return (await auth.login_cancel(login_id)).to_dict()
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


class _CodexControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1, max_length=512)
    thread_id: str | None = Field(default=None, min_length=1, max_length=512)
    turn_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _complete_exact_pair(self):
        if (self.thread_id is None) != (self.turn_id is None):
            raise ValueError("thread_id and turn_id must be supplied together")
        return self


class CodexInterruptRequest(_CodexControlRequest):
    execution_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _has_reference(self):
        if (
            self.execution_id is None
            and getattr(self, "correlation_id", None) is None
            and self.thread_id is None
        ):
            raise ValueError("an exact execution or turn reference is required")
        return self


class CodexIsolateRequest(_CodexControlRequest):
    execution_id: str = Field(min_length=1, max_length=512)


class _CodexWsInterruptRequest(CodexInterruptRequest):
    type: Literal["turn/interrupt"]
    correlation_id: str | None = Field(default=None, min_length=1, max_length=512)
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _consistent_execution_alias(self):
        if (
            self.execution_id is not None
            and self.correlation_id is not None
            and self.execution_id != self.correlation_id
        ):
            raise ValueError("execution identity aliases conflict")
        if self.execution_id is None and self.correlation_id is not None:
            self.execution_id = self.correlation_id
        return self


class _CodexWsIsolateRequest(CodexIsolateRequest):
    type: Literal["turn/isolate"]
    correlation_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _consistent_execution_alias(self):
        if self.correlation_id is not None and self.execution_id != self.correlation_id:
            raise ValueError("execution identity aliases conflict")
        return self


def _validated_isolate_outcome(value: object) -> str:
    if value not in {"released", "isolated"}:
        raise CodexError("Codex isolate outcome is invalid", code="invalid_response")
    return str(value)


@app.post("/api/codex/turn/interrupt")
async def codex_turn_interrupt(request: Request) -> dict:
    """Interrupt by exact thread+turn and return the authoritative terminal."""

    if request.headers.get("origin"):
        raise HTTPException(status_code=403, detail={"code": "host_only", "message": "Codex control is host-only"})
    try:
        body = await _bounded_request_body(request, 16 * 1024)
        req = CodexInterruptRequest.model_validate(json.loads(body))
        _client, _auth, agent = await _ensure_codex()
        terminal = await agent.interrupt_by_reference(
            req.session_id,
            execution_id=req.execution_id,
            thread_id=req.thread_id,
            turn_id=req.turn_id,
        )
        return {"ok": True, "terminal": terminal.to_dict()}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.post("/api/codex/turn/isolate")
async def codex_turn_isolate(request: Request) -> dict:
    """Host-only process-facing isolation fallback for a closed WS."""

    if request.headers.get("origin"):
        raise HTTPException(status_code=403, detail={"code": "host_only", "message": "Codex control is host-only"})
    try:
        body = await _bounded_request_body(request, 16 * 1024)
        req = CodexIsolateRequest.model_validate(json.loads(body))
        _client, _auth, agent = await _ensure_codex()
        # The provider validates the exact session/execution pair and kills
        # the shared App Server process group before this 200 is returned.
        outcome = _validated_isolate_outcome(
            await agent.isolate_turn(
                req.session_id,
                req.execution_id,
                thread_id=req.thread_id,
                turn_id=req.turn_id,
                reason="host isolation HTTP fallback",
            )
        )
        return {
            "ok": True,
            "status": outcome,
            "session_id": req.session_id,
            "execution_id": req.execution_id,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        status, payload = _codex_error_payload(exc)
        raise HTTPException(status_code=status, detail=payload) from exc


@app.websocket("/api/codex/ws")
async def codex_ws(ws: WebSocket) -> None:
    """Host-only Codex turn stream; browsers are rejected before accept."""

    if not _codex_origin_allowed(ws):
        await ws.close(code=1008, reason="Origin is not allowed")
        return
    await ws.accept()
    send_lock = asyncio.Lock()
    stream_task: asyncio.Task[None] | None = None
    interrupt_task: asyncio.Task[None] | None = None
    stream_started = False
    # `terminal_seen` means an app-server terminal was observed. It is not a
    # cleanup proof: the provider generator's finally must finish before the
    # Host may retire the socket or skip process isolation.
    terminal_seen = False
    provider_released = False
    pending_reservation: tuple[str, str] | None = None
    codex_agent = None
    turn_context: dict[str, str | None] = {
        "session_id": None,
        "thread_id": None,
        "turn_id": None,
        "correlation_id": None,
    }

    async def send(payload: dict) -> None:
        # Every payload here is a public contract object, not a JSON-RPC
        # envelope.  The lock prevents interleaved stream/interrupt writes.
        if "jsonrpc" in payload:
            payload = {key: value for key, value in payload.items() if key != "jsonrpc"}
        async with send_lock:
            await ws.send_json(payload)

    async def send_error(exc: Exception, *, correlation_id: str | None = None) -> None:
        _status, payload = _codex_error_payload(exc)
        if correlation_id:
            payload["correlation_id"] = correlation_id
        try:
            await send({"type": "error", **payload})
        except Exception:
            pass

    def validate_turn_request(message: dict) -> tuple[str, str, str, str, str, str | None, str]:
        """Validate public turn input before any acceptance acknowledgement."""

        session_id = message.get("session_id")
        text = message.get("text")
        correlation_id = message.get("correlation_id") or message.get("execution_id") or new_trace_id()
        model = message.get("model", "gpt-5.4-mini")
        reasoning_effort = message.get("reasoning_effort", "low")
        service_tier = message.get("service_tier")
        character = message.get("character", "xiaoman")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("session_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if len(text) > 16000:
            raise ValueError("text exceeds the 16000 character limit")
        if not isinstance(correlation_id, str) or not correlation_id or len(correlation_id) > 256:
            raise ValueError("invalid correlation_id")
        if message.get("cwd") is not None:
            raise ValueError("cwd is controlled by the bridge workspace configuration")
        if not isinstance(model, str) or not model or len(model) > 128:
            raise ValueError("model is required")
        if not isinstance(reasoning_effort, str) or not reasoning_effort or len(reasoning_effort) > 32:
            raise ValueError("reasoning_effort is required")
        if service_tier is not None and (not isinstance(service_tier, str) or not service_tier or len(service_tier) > 64):
            raise ValueError("service_tier is invalid")
        if character not in CHARACTERS:
            raise ValueError("character is invalid")
        return session_id, text, correlation_id, model, reasoning_effort, service_tier, character

    async def run_turn(message: dict) -> None:
        nonlocal codex_agent
        nonlocal turn_context
        nonlocal pending_reservation
        nonlocal terminal_seen
        nonlocal provider_released
        try:
            session_id, text, correlation_id, model, reasoning_effort, service_tier, character = validate_turn_request(message)
        except ValueError as exc:
            await send_error(exc)
            return
        try:
            _client, _auth, agent = await _ensure_codex()
            codex_agent = agent
            # Reserve the public correlation before thread/start so an
            # interrupt arriving in the same scheduling window records intent
            # and is replayed once exact threadId+turnId are known.
            await agent.reserve_turn(
                session_id,
                correlation_id,
                cwd=str(_codex_workspace()),
            )
            async for event in agent.stream_turn(
                session_id,
                text,
                correlation_id=correlation_id,
                cwd=str(_codex_workspace()),
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                character=character,
            ):
                # Clear the disconnect-isolation reservation before exposing
                # a terminal to the Host only after the provider generator has
                # settled.  The Host waits for `turn/released`; a close after
                # an authoritative terminal must never kill a normal turn.
                if event.terminal:
                    terminal_seen = True
                turn_context.update(
                    {
                        "session_id": event.session_id,
                        "thread_id": event.thread_id,
                        "turn_id": event.turn_id,
                        "correlation_id": event.correlation_id,
                    }
                )
                await send(event.to_dict())
            if terminal_seen:
                # Generator exhaustion is not release authority. In
                # particular, an isolation_failed terminal deliberately keeps
                # its state poisoned and its released_event false. Require the
                # exact session/execution ledger written by provider cleanup;
                # verified process isolation and normal completion both write
                # a true ledger, while poisoned/pending/unknown never emit the
                # Host's maintenance-release control frame.
                release_status = await agent.wait_for_execution_release(
                    session_id,
                    correlation_id,
                    timeout=min(1.0, agent.client.config.shutdown_timeout),
                )
                if release_status == "released":
                    pending_reservation = None
                    provider_released = True
                    await send({
                        "type": "turn/released",
                        "session_id": turn_context.get("session_id"),
                        "execution_id": turn_context.get("correlation_id"),
                        "thread_id": turn_context.get("thread_id"),
                        "turn_id": turn_context.get("turn_id"),
                    })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await send_error(exc, correlation_id=correlation_id)

    async def run_interrupt(message: dict) -> None:
        nonlocal pending_reservation
        nonlocal codex_agent
        try:
            req = _CodexWsInterruptRequest.model_validate(message)
        except ValueError as exc:
            await send_error(exc)
            return
        session_id = req.session_id
        thread_id = req.thread_id
        turn_id = req.turn_id
        execution_id = req.execution_id
        try:
            _client, _auth, agent = await _ensure_codex()
            codex_agent = agent
            if (
                isinstance(execution_id, str)
                and execution_id
                and thread_id is None
                and turn_id is None
            ):
                # An interrupt may arrive before the browser's turn/start
                # packet. Reserve the correlation now; the later start reuses
                # this state and the provider waits for exact ids.
                await agent.reserve_turn(
                    session_id,
                    execution_id,
                    cwd=str(_codex_workspace()),
                )
                pending_reservation = (session_id, execution_id)
            # The stream task remains the sole sender of a live turn's
            # AgentInterrupted event.  This await enforces the same
            # authoritative turn/completed semantics for the WS path; an
            # interrupt_result is sent only when no stream can deliver it.
            terminal = await agent.interrupt_by_reference(
                session_id,
                execution_id=execution_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if not stream_started:
                await send({"type": "interrupt_result", "terminal": terminal.to_dict()})
        except Exception as exc:  # noqa: BLE001
            await send_error(exc)

    async def run_isolate(message: dict) -> None:
        """Kill the shared App Server process group and acknowledge the gate."""

        try:
            req = _CodexWsIsolateRequest.model_validate(message)
        except ValueError as exc:
            await send_error(exc)
            return
        session_id = req.session_id
        execution_id = req.execution_id
        try:
            _client, _auth, agent = await _ensure_codex()
            outcome = _validated_isolate_outcome(
                await agent.isolate_turn(
                    session_id,
                    execution_id,
                    thread_id=req.thread_id,
                    turn_id=req.turn_id,
                    reason="host isolation",
                )
            )
            await send({
                "type": "isolate_result",
                "ok": True,
                "status": outcome,
                "session_id": session_id,
                "execution_id": execution_id,
            })
        except Exception as exc:  # noqa: BLE001
            await send_error(exc, correlation_id=execution_id)

    try:
        while True:
            message = await ws.receive_json()
            if not isinstance(message, dict):
                await send_error(ValueError("message must be an object"))
                continue
            if "jsonrpc" in message:
                await send_error(ValueError("JSON-RPC envelopes are not accepted on this browser API"))
                continue
            message_type = message.get("type")
            if message_type == "initialize":
                await send({"type": "ready", "session_id": message.get("session_id")})
            elif message_type == "turn/start":
                if not CODEX_TURN_EXECUTION_ENABLED:
                    await send_error(CodexError("Codex turn execution is unavailable", code="security_isolation_unavailable"))
                    continue
                if stream_task is not None and not stream_task.done():
                    await send_error(CodexBusyError())
                    continue
                try:
                    session_id, _text, correlation_id, _model, _effort, _tier, _character = validate_turn_request(message)
                    _client, _auth, agent = await _ensure_codex()
                    codex_agent = agent
                    await agent.reserve_turn(
                        session_id,
                        correlation_id,
                        cwd=str(_codex_workspace()),
                    )
                    pending_reservation = (session_id, correlation_id)
                except Exception as exc:  # noqa: BLE001 - reserve gate
                    await send_error(exc)
                    continue
                # Normalize an omitted execution/correlation id so run_turn
                # and an early interrupt share the same pending key.
                message = dict(message)
                message["correlation_id"] = correlation_id
                turn_context = {
                    "session_id": session_id,
                    "thread_id": None,
                    "turn_id": None,
                    "correlation_id": correlation_id,
                }
                await send(
                    {
                        "type": "accepted",
                        "session_id": turn_context["session_id"],
                        "correlation_id": turn_context["correlation_id"],
                    }
                )
                stream_started = True
                stream_task = asyncio.create_task(run_turn(message))
            elif message_type == "turn/interrupt":
                # Reserve before acknowledging an execution-only interrupt.
                # This closes the receive/disconnect scheduling window in
                # which a task could be canceled before it recorded intent.
                interrupt_session = message.get("session_id")
                interrupt_execution = message.get("execution_id") or message.get("correlation_id")
                if (
                    isinstance(interrupt_session, str)
                    and interrupt_session
                    and isinstance(interrupt_execution, str)
                    and interrupt_execution
                    and message.get("thread_id") is None
                    and message.get("turn_id") is None
                ):
                    try:
                        _client, _auth, agent = await _ensure_codex()
                        codex_agent = agent
                        await agent.reserve_turn(
                            interrupt_session,
                            interrupt_execution,
                            cwd=str(_codex_workspace()),
                        )
                        pending_reservation = (interrupt_session, interrupt_execution)
                    except Exception as exc:  # noqa: BLE001 - pending cancel gate
                        await send_error(exc)
                        continue
                await send(
                    {
                        "type": "interrupt_requested",
                        "session_id": message.get("session_id"),
                        "thread_id": message.get("thread_id"),
                        "turn_id": message.get("turn_id"),
                        "execution_id": message.get("execution_id") or message.get("correlation_id"),
                    }
                )
                if interrupt_task is None or interrupt_task.done():
                    interrupt_task = asyncio.create_task(run_interrupt(message))
            elif message_type == "turn/isolate":
                # This operation is host-only; browser-origin sockets are
                # rejected before accept and cannot reach this branch.
                await run_isolate(message)
            else:
                await send_error(ValueError("unsupported Codex WS message type"))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Codex websocket closed", exc_info=True)
    finally:
        # A terminal frame can be sent immediately before the client closes.
        # Give the provider generator a bounded chance to run its cleanup
        # finally and publish the release fence; only an unreleased task is
        # process-facing uncertainty that must be isolated.
        if terminal_seen and not provider_released and stream_task is not None and not stream_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stream_task), timeout=1.0)
            except Exception:
                pass
        if codex_agent is not None and not provider_released and (
            (stream_task is not None and not stream_task.done())
            or pending_reservation is not None
        ):
            target = pending_reservation or (
                turn_context.get("session_id"),
                turn_context.get("correlation_id"),
            )
            if isinstance(target[0], str) and isinstance(target[1], str) and target[0] and target[1]:
                try:
                    # Do this before canceling child tasks: a browser timeout
                    # must not leave a ghost rollout after WS disconnect.
                    await codex_agent.isolate_turn(target[0], target[1], reason="browser disconnect")
                except Exception:
                    pass
        if interrupt_task is not None:
            interrupt_task.cancel()
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
        for task in (interrupt_task, stream_task):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        # A synchronous reserve can exist even when the stream task never got
        # scheduled (accepted-then-immediate-disconnect).  Once exact ids
        # have been observed, stream/provider cleanup owns the state; before
        # that point remove the reservation explicitly so no pending
        # correlation survives a browser abort.
        if (
            pending_reservation is not None
            and turn_context.get("thread_id") is None
            and turn_context.get("turn_id") is None
        ):
            agent = codex_agent or _CODEX_AGENT
            if agent is not None:
                await agent.cancel_reservation(*pending_reservation)


def _trace_id_from_request(request: Request) -> str:
    """Use the browser correlation id when present, otherwise create one."""

    return request.headers.get("x-voice-trace-id", "").strip() or new_trace_id()


@app.post("/api/stt")
async def stt(request: Request) -> dict:
    """Speech to text: 16 kHz PCM16 (raw or WAV) -> { text, language }."""
    trace_id = _trace_id_from_request(request)
    span = LATENCY.start("stt", trace_id=trace_id)
    status = "ok"
    body = b""
    try:
        with span.stage("request_body"):
            body = await _bounded_request_body(request, MAX_STT_BODY_BYTES)
        if not body:
            status = "http_400"
            raise HTTPException(status_code=400, detail="Empty body")
        if VOICE_RUNTIME.enabled:
            try:
                with span.stage("voice_runtime"):
                    result = await VOICE_RUNTIME.transcribe(
                        body,
                        content_type=request.headers.get("content-type", ""),
                        trace_id=trace_id,
                        max_audio_sec=request.headers.get("X-Max-Audio-Sec"),
                        sample_rate=request.headers.get("X-Voice-Sample-Rate"),
                    )
            except VoiceRuntimeError as exc:
                status = f"http_{exc.status_code}"
                raise HTTPException(
                    status_code=exc.status_code,
                    detail="v3 Voice Runtime STT unavailable",
                ) from exc
            return {
                "text": result["text"],
                "language": result.get("language"),
                "trace_id": result.get("trace_id", trace_id),
            }
        with span.stage("decode"):
            audio = await asyncio.to_thread(
                decode_audio, body, request.headers.get("content-type", "")
            )
        duration = len(audio) / 16000.0
        requested_max = request.headers.get("X-Max-Audio-Sec")
        try:
            # The client header is only a stricter per-request preference.
            max_sec = _stt_audio_limit(requested_max)
        except HTTPException:
            status = "http_400"
            raise
        if duration > max_sec:
            status = "http_422"
            raise HTTPException(
                status_code=422,
                detail=f"Audio too long: {duration:.1f}s exceeds X-Max-Audio-Sec {max_sec}s",
            )
        with span.stage("model"):
            async with models.infer_lock:
                handler = await models.ensure_stt()
                text, language = await asyncio.to_thread(_transcribe, handler, audio)
        return {
            "text": text,
            "language": language,
            "trace_id": trace_id,
        }
    except HTTPException as exc:
        if status == "ok":
            status = f"http_{exc.status_code}"
        raise
    except Exception as exc:
        status = "error"
        logger.exception("STT request failed")
        raise HTTPException(status_code=500, detail="STT request failed") from exc
    finally:
        span.finish(status=status, audio_bytes=len(body))


class TTSRequest(BaseModel):
    text: str
    character: str | None = None
    session_id: str | None = None
    turn_id: str | None = Field(default=None, max_length=256)
    generation: int = Field(default=0, ge=0)
    end: bool = False


class AvatarSessionRequest(BaseModel):
    dsh_session_id: str = Field(min_length=1, max_length=256)
    avatar_session_id: str = Field(min_length=1, max_length=64)


@app.put("/api/avatar/session")
async def register_avatar_session(req: AvatarSessionRequest) -> dict[str, bool]:
    """Associate one browser-owned LiveTalking session with a DSH session."""
    if VOICE_RUNTIME.enabled:
        try:
            await VOICE_RUNTIME.avatar_session(
                "PUT", req.dsh_session_id, req.avatar_session_id
            )
        except VoiceRuntimeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail="v3 Voice Runtime Avatar registration unavailable",
            ) from exc
        return {"ok": True}
    try:
        AVATAR_RELAY.register(req.dsh_session_id, req.avatar_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/api/avatar/session")
async def unregister_avatar_session(req: AvatarSessionRequest) -> dict[str, bool]:
    """Compare-and-delete so a stale React cleanup cannot remove a new owner."""
    if VOICE_RUNTIME.enabled:
        try:
            result = await VOICE_RUNTIME.avatar_session(
                "DELETE", req.dsh_session_id, req.avatar_session_id
            )
        except VoiceRuntimeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail="v3 Voice Runtime Avatar registration unavailable",
            ) from exc
        return {"ok": True, "removed": bool(result.get("removed"))}
    try:
        removed = AVATAR_RELAY.unregister(req.dsh_session_id, req.avatar_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "removed": removed}


@app.post("/api/tts")
async def tts(request: Request) -> Response:
    """Text to speech: { text } -> 16 kHz mono PCM16 WAV (Xiaoya voice clone).

    Cooperative cancellation: while the client aborts its fetch (the voice
    toggle turned off), the request disconnects here; a watchdog sets a
    threading event and the synthesis loop stops between chunks, so the GPU
    is freed immediately instead of draining the queue."""
    trace_id = _trace_id_from_request(request)
    span = LATENCY.start("tts", trace_id=trace_id)
    status = "ok"
    text = ""
    character = normalize_character(request.headers.get("X-Voice-Character"))
    dsh_session_id: str | None = None
    cancel = threading.Event()
    wav = b""
    watcher: asyncio.Task[None] | None = None
    try:
        body = await _bounded_request_body(request, MAX_TTS_BODY_BYTES)
        try:
            payload = json.loads(body)
            req = TTSRequest.model_validate(payload)
            text = (req.text or "").strip()
            character = normalize_character(req.character or request.headers.get("X-Voice-Character"))
            dsh_session_id = req.session_id
        except Exception as exc:  # noqa: BLE001 - bounded JSON boundary
            status = "http_400"
            raise HTTPException(status_code=400, detail="Invalid TTS request") from exc
        if not text:
            status = "http_400"
            raise HTTPException(status_code=400, detail="Empty text")
        if len(text) > MAX_TTS_TEXT_CHARS:
            status = "http_413"
            raise HTTPException(status_code=413, detail="TTS text exceeds the server limit")

        if VOICE_RUNTIME.enabled:
            try:
                with span.stage("voice_runtime"):
                    runtime_payload = req.model_dump(exclude_none=True)
                    runtime_payload["character"] = character
                    wav, runtime_headers = await VOICE_RUNTIME.synthesize(
                        runtime_payload,
                        trace_id=trace_id,
                    )
            except VoiceRuntimeError as exc:
                status = f"http_{exc.status_code}"
                raise HTTPException(
                    status_code=exc.status_code,
                    detail="v3 Voice Runtime TTS unavailable",
                ) from exc
            return Response(
                content=wav,
                media_type="audio/wav",
                headers={
                    "X-Voice-Trace-Id": runtime_headers.get(
                        "X-Voice-Trace-Id", trace_id
                    ),
                    "X-Voice-Character": character,
                },
            )

        async def watch_disconnect() -> None:
            while True:
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.2)

        watcher = asyncio.create_task(watch_disconnect())
        with span.stage("model"):
            async with models.infer_lock:
                handler = await models.ensure_tts()
                samples = await asyncio.to_thread(_synthesize, handler, text, cancel)

        if cancel.is_set():
            status = "cancelled"
            logger.info("TTS cancelled by client disconnect")
            raise HTTPException(status_code=499, detail="TTS cancelled by client")
        with span.stage("wav_encode"):
            wav = _pcm16_to_wav(samples)
        if character == "xiaoman":
            AVATAR_RELAY.submit_wav(dsh_session_id, wav)
        logger.info("TTS OK: %d chars -> %.2fs wav (%d bytes)", len(text), len(samples) / 16000.0, len(wav))
        return Response(
            content=wav,
            media_type="audio/wav",
            headers={"X-Voice-Trace-Id": trace_id, "X-Voice-Character": character},
        )
    except HTTPException as exc:
        if status == "ok":
            status = f"http_{exc.status_code}"
        raise
    except Exception as exc:
        status = "error"
        logger.exception("TTS request failed")
        raise HTTPException(status_code=500, detail="TTS request failed") from exc
    finally:
        if watcher is not None:
            watcher.cancel()
        span.finish(status=status, text_chars=len(text), audio_bytes=len(wav) if "wav" in locals() else 0)


def _tts_result_pcm16(result: object, handler: object) -> bytes:
    """Normalize one provider result to 16 kHz mono little-endian PCM16."""

    audio = np.asarray(getattr(result, "audio", result), dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return b""
    if not np.all(np.isfinite(audio)):
        raise RuntimeError("TTS produced non-finite audio")
    source_rate = int(
        getattr(result, "sample_rate", None)
        or getattr(handler, "sample_rate", TTS_STREAM_SAMPLE_RATE)
        or TTS_STREAM_SAMPLE_RATE
    )
    if not 8000 <= source_rate <= 48000:
        raise RuntimeError("TTS produced an unsupported sample rate")
    if source_rate != TTS_STREAM_SAMPLE_RATE:
        from scipy.signal import resample_poly

        gcd = int(np.gcd(source_rate, TTS_STREAM_SAMPLE_RATE))
        audio = resample_poly(
            audio,
            up=TTS_STREAM_SAMPLE_RATE // gcd,
            down=source_rate // gcd,
        )
    if audio.size == 0:
        return b""
    if float(np.max(np.abs(audio))) <= 1.5:
        audio = audio * 32767.0
    return np.asarray(np.clip(audio, -32768, 32767), dtype="<i2").tobytes()


async def _iter_tts_results(
    handler: object,
    text: str,
    cancel: threading.Event,
    turn_id: str,
) -> AsyncIterator[object]:
    """Bridge a synchronous model iterator into an async response stream."""

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    finished = object()

    def produce() -> None:
        try:
            if hasattr(handler, "stream"):
                results = handler.stream(text, turn_id=turn_id, cancel=cancel)
            elif hasattr(handler, "synthesize"):
                results = (handler.synthesize(text, turn_id=turn_id, cancel=cancel),)
            else:
                raise RuntimeError("configured TTS provider does not support streaming")
            for result in results:
                if cancel.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, result)
        except BaseException as exc:  # propagate provider errors on the event loop
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, finished)

    worker = asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            item = await queue.get()
            if item is finished:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancel.set()
        # Provider implementations hold their own model lock.  Let an
        # uninterruptible native generation finish in the background rather
        # than delaying HTTP disconnect; later requests still serialize there.
        if not worker.done():
            worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        else:
            await worker


@app.post("/api/tts/stream")
async def tts_stream(request: Request) -> StreamingResponse:
    """Stream 16 kHz mono PCM16 as soon as the provider yields each chunk."""

    trace_id = _trace_id_from_request(request)
    body = await _bounded_request_body(request, MAX_TTS_BODY_BYTES)
    try:
        payload = json.loads(body)
        req = TTSRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - bounded JSON boundary
        raise HTTPException(status_code=400, detail="Invalid TTS request") from exc
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")
    if len(text) > MAX_TTS_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="TTS text exceeds the server limit")

    character = normalize_character(req.character or request.headers.get("X-Voice-Character"))
    turn_id = (req.turn_id or trace_id).strip()
    cancel = threading.Event()
    span = LATENCY.start("tts.stream", trace_id=trace_id)

    if VOICE_RUNTIME.enabled:
        runtime_payload = req.model_dump(exclude_none=True)
        runtime_payload["character"] = character
        runtime_context = VOICE_RUNTIME.stream_tts(
            runtime_payload,
            trace_id=trace_id,
        )
        try:
            upstream = await runtime_context.__aenter__()
        except VoiceRuntimeError as exc:
            span.finish(status=f"http_{exc.status_code}", text_chars=len(text))
            raise HTTPException(
                status_code=exc.status_code,
                detail="v3 Voice Runtime TTS stream unavailable",
            ) from exc

        async def proxy() -> AsyncIterator[bytes]:
            total_bytes = 0
            chunks = 0
            status = "ok"
            try:
                async for pcm in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        status = "cancelled"
                        break
                    if not pcm:
                        continue
                    total_bytes += len(pcm)
                    if total_bytes > MAX_TTS_RESPONSE_BYTES:
                        raise RuntimeError("TTS stream exceeds the audio limit")
                    if chunks == 0:
                        span.mark("first_pcm")
                    chunks += 1
                    yield pcm
                if chunks == 0 and status == "ok":
                    raise RuntimeError("TTS produced no audio")
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception:
                status = "error"
                logger.exception("v3 Voice Runtime proxy stream failed")
                raise
            finally:
                await runtime_context.__aexit__(None, None, None)
                span.finish(
                    status=status,
                    text_chars=len(text),
                    chunks=chunks,
                    audio_bytes=total_bytes,
                    audio_ms=round(
                        total_bytes * 1000 / (TTS_STREAM_SAMPLE_RATE * 2)
                    ),
                    voice_runtime="v3",
                )

        return StreamingResponse(
            proxy(),
            media_type="audio/L16",
            headers={
                "Cache-Control": "no-store",
                "X-Voice-Trace-Id": upstream.headers.get(
                    "X-Voice-Trace-Id", trace_id
                ),
                "X-Voice-Character": character,
                "X-Voice-Audio-Format": "pcm_s16le",
                "X-Voice-Sample-Rate": str(TTS_STREAM_SAMPLE_RATE),
                "X-Voice-Channels": "1",
                "X-Voice-Runtime-Protocol": upstream.headers.get(
                    "X-Voice-Runtime-Protocol", ""
                ),
            },
        )

    async def generate() -> AsyncIterator[bytes]:
        status = "ok"
        total_bytes = 0
        chunks = 0
        avatar_packets = 0
        try:
            async with models.infer_lock:
                handler = await models.ensure_tts()
                async for result in _iter_tts_results(handler, text, cancel, turn_id):
                    if await request.is_disconnected():
                        status = "cancelled"
                        cancel.set()
                        break
                    pcm = _tts_result_pcm16(result, handler)
                    if not pcm:
                        continue
                    if total_bytes + len(pcm) > MAX_TTS_RESPONSE_BYTES:
                        raise RuntimeError("TTS stream exceeds the audio limit")
                    if chunks == 0:
                        span.mark("first_pcm")
                    if character == "xiaoman":
                        avatar_task = AVATAR_RELAY.submit_pcm(
                            req.session_id,
                            pcm,
                            turn_id=turn_id,
                            generation=req.generation,
                            sample_rate=TTS_STREAM_SAMPLE_RATE,
                        )
                        if avatar_task is not None:
                            avatar_packets += 1
                    chunks += 1
                    total_bytes += len(pcm)
                    yield pcm
            if chunks == 0 and status == "ok":
                raise RuntimeError("TTS produced no audio")
            if req.end and character == "xiaoman" and req.session_id:
                # LiveTalking requires a non-empty media packet for an end
                # marker.  Twenty milliseconds of silence closes the logical
                # turn without creating a browser-side playback gap.
                silence = bytes(TTS_STREAM_SAMPLE_RATE * 2 // 50)
                AVATAR_RELAY.submit_pcm(
                    req.session_id,
                    silence,
                    turn_id=turn_id,
                    generation=req.generation,
                    sample_rate=TTS_STREAM_SAMPLE_RATE,
                    end=True,
                )
        except asyncio.CancelledError:
            status = "cancelled"
            cancel.set()
            raise
        except Exception:
            status = "error"
            logger.exception("Streaming TTS request failed")
            raise
        finally:
            cancel.set()
            span.finish(
                status=status,
                text_chars=len(text),
                chunks=chunks,
                audio_bytes=total_bytes,
                audio_ms=round(total_bytes * 1000 / (TTS_STREAM_SAMPLE_RATE * 2)),
                avatar_packets=avatar_packets,
            )

    return StreamingResponse(
        generate(),
        media_type="audio/L16",
        headers={
            "Cache-Control": "no-store",
            "X-Voice-Trace-Id": trace_id,
            "X-Voice-Character": character,
            "X-Voice-Audio-Format": "pcm_s16le",
            "X-Voice-Sample-Rate": str(TTS_STREAM_SAMPLE_RATE),
            "X-Voice-Channels": "1",
        },
    )


def _synthesize(handler, text: str, cancel: threading.Event | None = None) -> np.ndarray:
    """Run the Qwen3 TTS handler for one utterance, concatenating int16 chunks.

    Stops early between chunks when `cancel` is set (client disconnect)."""
    if (hasattr(handler, "stream") or hasattr(handler, "synthesize")) and not hasattr(handler, "process"):
        # xiaoman adapters return normalized float PCM in TTSResult. Convert
        # at the bridge boundary so the existing WAV/QQ sinks remain stable.
        chunks: list[np.ndarray] = []
        results = handler.stream(text, cancel=cancel) if hasattr(handler, "stream") else (handler.synthesize(text, cancel=cancel),)
        for result in results:
            if cancel is not None and cancel.is_set():
                break
            audio = np.asarray(getattr(result, "audio", result), dtype=np.float32)
            if audio.size == 0:
                continue
            if float(np.max(np.abs(audio))) <= 1.5:
                audio = audio * 32767.0
            chunks.append(np.asarray(np.clip(audio, -32768, 32767), dtype=np.int16))
        if not chunks:
            raise HTTPException(status_code=500, detail="TTS produced no audio")
        audio = np.concatenate(chunks)
        sample_rate = int(getattr(handler, "sample_rate", 16000) or 16000)
        if sample_rate != 16000 and audio.size > 0:
            from scipy.signal import resample_poly

            gcd = int(np.gcd(sample_rate, 16000))
            audio = resample_poly(audio.astype(np.float32), up=16000 // gcd, down=sample_rate // gcd)
        return np.asarray(audio, dtype=np.int16)

    from speech_to_speech.pipeline.messages import TTSInput

    chunks = []
    for chunk in handler.process(TTSInput(text=text, language_code="zh")):
        if cancel is not None and cancel.is_set():
            logger.info("TTS: cancelled mid-synthesis")
            break
        if isinstance(chunk, bytes):
            chunks.append(np.frombuffer(chunk, dtype=np.int16))
        else:
            chunks.append(np.asarray(chunk, dtype=np.int16))
    if not chunks:
        raise HTTPException(status_code=500, detail="TTS produced no audio")
    return np.concatenate(chunks)


def _pcm16_to_wav(samples: np.ndarray) -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


# ── Companion media hosting (T8) ────────────────────────────────────────────

BG_IMAGES_DIR = Path(CONFIG["media"]["bg_images_dir"])
TASK_VIDEOS_DIR = Path(CONFIG["media"]["task_videos_dir"])
VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".mov", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}


def _list_media(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    entries = []
    for name in sorted(os.listdir(directory)):
        ext = Path(name).suffix.lower()
        if ext in VIDEO_EXTS:
            entries.append({"name": name, "type": "video"})
        elif ext in IMAGE_EXTS:
            entries.append({"name": name, "type": "image"})
    return entries


@app.get("/api/media/bg-images")
async def media_bg_images() -> dict:
    """Idle/background media list (videos + images, name-sorted)."""
    return {"media": _list_media(BG_IMAGES_DIR)}


@app.get("/api/media/task-videos")
async def media_task_videos() -> dict:
    """Speaking-animation video list (videos only, name-sorted)."""
    return {
        "videos": [
            entry["name"]
            for entry in _list_media(TASK_VIDEOS_DIR)
            if entry["type"] == "video"
        ]
    }


@app.get("/api/avatar/{character}/{state}")
async def avatar_state_media(character: str, state: str) -> dict:
    """Return character/state media with idle fallback for missing clips."""
    normalized = normalize_character(character)
    entries = state_media(normalized, state)
    return {
        "character": normalized,
        "state": state if state in {"idle", "listening", "thinking", "speaking"} else "idle",
        "fallback": "idle",
        "media": entries,
    }


# Static mounts (Range-capable) for the companion window's <video> sources.
if BG_IMAGES_DIR.is_dir():
    app.mount("/media/bg-images", StaticFiles(directory=str(BG_IMAGES_DIR)), name="media-bg")
if TASK_VIDEOS_DIR.is_dir():
    app.mount("/media/task-videos", StaticFiles(directory=str(TASK_VIDEOS_DIR)), name="media-task")
XIAOMAN_MEDIA_DIR = REPO_ROOT / "assets" / "xiaoman"
if XIAOMAN_MEDIA_DIR.is_dir():
    app.mount("/media/avatars/xiaoman", StaticFiles(directory=str(XIAOMAN_MEDIA_DIR)), name="media-avatar-xiaoman")


# ── QQ 推送（NapCat OneBot）───────────────────────────────────────────────
#
# Sends text and TTS voice to a target QQ via a local NapCat OneBot v11 HTTP
# endpoint. Config (bridge-config.json):
#   "qq": {
#     "enabled": true,
#     "napcat_base": "http://127.0.0.1:3000",
#     "napcat_token": "",
#     "target_qq": 0
#   }

class QQSendRequest(BaseModel):
    text: str
    voice: bool = False
    user_id: int | None = None  # override the configured target


class QQImageRequest(BaseModel):
    path: str
    user_id: int | None = None


@app.post("/api/qq/image")
async def qq_send_image(req: QQImageRequest) -> dict:
    """Send a local image file to the configured QQ."""
    qq = CONFIG.get("qq", {})
    if not qq.get("enabled"):
        raise HTTPException(status_code=400, detail="QQ push disabled in bridge-config.json")
    base = qq.get("napcat_base", "http://127.0.0.1:3000")
    token = qq.get("napcat_token", "")
    user_id = req.user_id or qq.get("target_qq")
    if not user_id:
        raise HTTPException(status_code=400, detail="target_qq not configured")
    if not Path(req.path).is_file():
        raise HTTPException(status_code=404, detail=f"image not found: {req.path}")
    from qq_bridge import send_image

    try:
        result = send_image(base, token, user_id, req.path)
        return {"ok": True, "user_id": user_id, "napcat": result}
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        logger.exception("QQ image send failed")
        raise HTTPException(status_code=502, detail=f"QQ image send failed: {exc}") from exc


@app.post("/api/qq/send")
async def qq_send(req: QQSendRequest) -> dict:
    """Send { text } (and optionally TTS voice) to the configured QQ."""
    qq = CONFIG.get("qq", {})
    if not qq.get("enabled"):
        raise HTTPException(status_code=400, detail="QQ push disabled in bridge-config.json")
    base = qq.get("napcat_base", "http://127.0.0.1:3000")
    token = qq.get("napcat_token", "")
    user_id = req.user_id or qq.get("target_qq")
    if not user_id:
        raise HTTPException(status_code=400, detail="target_qq not configured")
    if not req.text.strip() and not req.voice:
        raise HTTPException(status_code=400, detail="Empty text")

    from qq_bridge import send_text, send_voice

    try:
        if req.voice:
            if not req.text.strip():
                raise HTTPException(status_code=400, detail="voice needs text to synthesize")
            text = (req.text or "").strip()[:512]
            cancel = threading.Event()
            async with models.infer_lock:
                handler = await models.ensure_tts()
                samples = await asyncio.to_thread(_synthesize, handler, text, cancel)
            result = send_voice(base, token, user_id, samples.astype("<i2").tobytes())
        else:
            result = send_text(base, token, user_id, req.text.strip()[:2000])
        return {"ok": True, "user_id": user_id, "napcat": result}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        logger.exception("QQ send failed")
        raise HTTPException(status_code=502, detail=f"QQ send failed: {exc}") from exc


# ── QQ 双向：事件接收 + 插件 WS 桥 ────────────────────────────────────────
#
# NapCat 把消息事件 POST 到 /api/qq/event（HTTP 上报 postUrls）；桥接再把
# 私聊文本推给已连接的浏览器插件（/api/qq/ws）。插件注入 DSH，回复完成后
# 把回复文本发回桥接（WS {"type":"reply"}），桥接 TTS→silk→QQ 发出。
# 单连接设计（个人使用）：新连接顶掉旧连接。

_qq_ws_conn: WebSocket | None = None
_qq_ws_lock = asyncio.Lock()


def _qq_owner_token(qq: dict) -> str:
    """Return the separate inbound owner secret, never a backend detail."""

    value = qq.get("owner_token")
    return value if isinstance(value, str) else ""


def _qq_token_matches(headers: Mapping[str, str], qq: dict) -> bool:
    expected = _qq_owner_token(qq)
    authorization = headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    return bool(expected) and hmac.compare_digest(supplied, expected)


def _qq_allowed_origins() -> set[str]:
    values = CONFIG.get("cors_origins", [])
    return {value for value in values if isinstance(value, str) and value}


def _qq_http_owner_gate(request: Request) -> None:
    """Reject disabled/browser/ownerless NapCat HTTP before body parsing."""

    qq = CONFIG.get("qq", {})
    if not bool(qq.get("enabled")):
        raise HTTPException(status_code=403, detail={"code": "qq_disabled", "message": "QQ bridge is disabled"})
    if request.headers.get("origin") or not _qq_token_matches(request.headers, qq):
        raise HTTPException(status_code=403, detail={"code": "qq_unauthorized", "message": "QQ bridge authorization required"})


def _qq_websocket_gate(ws: WebSocket, *, browser: bool) -> bool:
    """Check enabled + owner/origin policy before accepting a QQ socket."""

    qq = CONFIG.get("qq", {})
    if not bool(qq.get("enabled")):
        return False
    origin = ws.headers.get("origin")
    if browser and origin in _qq_allowed_origins():
        return True
    # NapCat's OneBot socket must be host-owned and therefore has no browser
    # Origin. The browser/plugin socket may also use the explicit owner token
    # for non-browser local clients.
    return not origin and _qq_token_matches(ws.headers, qq)


async def _qq_push(json_msg: dict) -> None:
    global _qq_ws_conn
    async with _qq_ws_lock:
        conn = _qq_ws_conn
    if conn is not None:
        try:
            await conn.send_json(json_msg)
        except Exception:
            logger.debug("QQ ws push failed (client gone)", exc_info=True)


@app.post("/api/qq/event")
async def qq_event(request: Request) -> dict:
    """OneBot v11 HTTP 上报入口（NapCat postUrls）。私聊文本消息 → 推给插件。"""
    _qq_http_owner_gate(request)
    try:
        raw = await _bounded_request_body(request, MAX_QQ_EVENT_BODY_BYTES)
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("QQ event must be an object")
        post_type = body.get("post_type")
        if post_type == "message" and body.get("message_type") == "private":
            user_id = body.get("user_id")
            text = str(body.get("raw_message") or body.get("message") or "").strip()
            if user_id and text:
                await _qq_push({"type": "qq_message", "user_id": user_id, "text": text})
                logger.info("QQ event: %s -> %s", user_id, text[:40])
    except HTTPException:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_request", "message": "Invalid QQ event"}) from exc
    except Exception:  # noqa: BLE001 - never break the upstream event feed
        logger.exception("QQ event handling failed")
    return {"ok": True}


@app.websocket("/api/qq/onebot")
async def qq_onebot_ws(ws: WebSocket) -> None:
    """NapCat WebSocket 客户端连到这里（OneBot 事件推送）。

    在 NapCat WebUI 网络配置里添加一个「WebSocket 客户端」指向
    ws://127.0.0.1:8765/api/qq/onebot，NapCat 会把全部事件推过来；
    私聊文本消息同样经 _qq_push 转给浏览器插件。这绕开了 HTTP 3000
    服务不稳定时的事件上报缺口。
    """
    if not _qq_websocket_gate(ws, browser=False):
        await ws.close(code=1008, reason="QQ bridge disabled or unauthorized")
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw.encode("utf-8")) > MAX_QQ_WS_FRAME_BYTES:
                await ws.close(code=1009, reason="QQ frame too large")
                return
            if not raw.strip():
                continue
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                continue
            post_type = body.get("post_type")
            if post_type == "message" and body.get("message_type") == "private":
                user_id = body.get("user_id")
                text = str(body.get("raw_message") or body.get("message") or "").strip()
                if user_id and text:
                    await _qq_push({"type": "qq_message", "user_id": user_id, "text": text})
                    logger.info("QQ event(ws): %s -> %s", user_id, text[:40])
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("QQ onebot ws error")


@app.websocket("/api/qq/ws")
async def qq_ws(ws: WebSocket) -> None:
    """插件桥连接：桥接 → 插件(qq_message)，插件 → 桥接(reply → 发 QQ)。"""
    global _qq_ws_conn
    if not _qq_websocket_gate(ws, browser=True):
        await ws.close(code=1008, reason="QQ bridge disabled or unauthorized")
        return
    await ws.accept()
    async with _qq_ws_lock:
        old = _qq_ws_conn
        _qq_ws_conn = ws
    if old is not None:
        try:
            await old.close()
        except Exception:
            pass
    try:
        while True:
            raw_text = await ws.receive_text()
            if len(raw_text.encode("utf-8")) > MAX_QQ_WS_FRAME_BYTES:
                await ws.close(code=1009, reason="QQ frame too large")
                return
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("type") == "reply":
                text = str(raw.get("text") or "").strip()
                if text:
                    qq = CONFIG.get("qq", {})
                    if qq.get("enabled"):
                        try:
                            # 先发原始文本，再发 TTS 语音（复用 /api/qq/send 逻辑）
                            resp_text = await qq_send(QQSendRequest(text=text, voice=False))
                            resp_voice = await qq_send(QQSendRequest(text=text, voice=True))
                            await ws.send_json({"type": "sent", "ok": True, "text": resp_text, "voice": resp_voice})
                        except HTTPException as exc:
                            await ws.send_json({"type": "sent", "ok": False, "detail": exc.detail})
                    else:
                        await ws.send_json({"type": "sent", "ok": False, "detail": "QQ push disabled"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("QQ ws error")
    finally:
        async with _qq_ws_lock:
            if _qq_ws_conn is ws:
                _qq_ws_conn = None


# ── Silero VAD endpoint (barge-in detection) ──────────────────────────────
#
# The original speech-to-speech project runs VAD on the SERVER with silero-vad,
# a neural network trained to tell a real human voice apart from noise / music /
# TTS echo. Our browser-side RMS threshold cannot do that, which is why ambient
# sounds kept tripping the barge-in and got STT'd into phantom messages.
#
# /api/vad is a WebSocket: while a reply is playing the client streams its mic
# PCM16 chunks here; the server runs them through silero VAD (loaded from the
# local <repo>/models/silero-vad/ directory, NOT the torch hub cache) and
# replies {"event":"speech_start"} only when a real voice is heard — the
# client then interrupts the reply. Chunks are never stored.

class VADSession:
    """One silero VAD session per WebSocket connection.

    Loads silero_vad_v4.jit (stable, no annotator) from <repo>/models/, falling
    back to silero_vad.jit if the v4 file is absent. State (h/c) lives in the
    jit model instance, so each session gets a fresh detector.
    """

    def __init__(self) -> None:
        import torch
        from speech_to_speech.VAD.vad_iterator import VADIterator

        # Models live at <repo>/models, alongside bridge/, not under
        # <repo>/bridge/models.  The old path made VAD fail on every clean
        # checkout even when the model had been downloaded correctly.
        models_dir = REPO_ROOT / "models" / "silero-vad"
        model_path = models_dir / "silero_vad_v4.jit"
        if not model_path.is_file():
            model_path = models_dir / "silero_vad.jit"
        if not model_path.is_file():
            raise RuntimeError(f"silero-vad model not found under {models_dir}")

        self.model = torch.jit.load(str(model_path), map_location="cpu")
        self.model.eval()
        self.iterator = VADIterator(
            self.model,
            threshold=0.6,
            sampling_rate=16000,
            min_silence_duration_ms=64,
            speech_pad_ms=30,
        )
        self.min_speech_ms = 384
        self.speech_started = False
        # Byte buffer: client chunks (any size) accumulate until a full
        # 512-sample window is available — silero gets CONTINUOUS audio, never
        # zero-padded frames (padding between real audio breaks VAD state).
        self._buf = b""
        self.total_bytes = 0
        self._rate_window_started = 0.0
        self._rate_window_bytes = 0

    def feed(self, pcm16: bytes) -> list[dict]:
        """Feed one 16 kHz PCM16 chunk (any size); returns outbound JSON events.

        Silero VAD requires fixed 512-sample windows at 16 kHz; chunks are
        buffered and cut into 512-sample frames so the audio stream stays
        contiguous. A barge-in fires once sustained speech reaches
        min_speech_ms (384ms) — the same confirmation the original project
        applies. VADAudio outputs (final utterances) are intentionally ignored
        here — this endpoint only signals barge-in timing; the client keeps
        its own capture for STT.
        """
        if len(pcm16) > MAX_VAD_FRAME_BYTES or len(pcm16) % 2:
            raise ValueError("VAD input must be an even PCM16 frame within the server limit")

        import numpy as np
        import torch

        self._buf += pcm16
        out: list[dict] = []
        while len(self._buf) >= 1024:  # 512 int16 samples = 1024 bytes
            window = self._buf[:1024]
            self._buf = self._buf[1024:]
            x = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768.0
            utterance = self.iterator(torch.from_numpy(x))
            if self.iterator.triggered and not self.speech_started:
                active_ms = self.iterator.active_speech_samples / 16.0
                if active_ms >= self.min_speech_ms:
                    self.speech_started = True
                    out.append({"event": "speech_start"})
            if utterance is not None:
                self.speech_started = False
                out.append({"event": "speech_end"})
        return out


@app.websocket("/api/vad")
async def vad_endpoint(ws: WebSocket) -> None:
    """Streaming barge-in VAD. Client pushes raw 16 kHz mono PCM16 (any chunk
    size, ~40ms typical); server replies speech_start/speech_end JSON when
    silero VAD hears human speech."""
    await ws.accept()
    session = VADSession()
    try:
        while True:
            data = await ws.receive_bytes()
            if not data:
                continue
            if len(data) > MAX_VAD_FRAME_BYTES or len(data) % 2:
                await ws.close(code=1009, reason="VAD frame is too large or not PCM16 aligned")
                return
            now = asyncio.get_running_loop().time()
            if now - session._rate_window_started >= 1.0:
                session._rate_window_started = now
                session._rate_window_bytes = 0
            if session.total_bytes + len(data) > MAX_VAD_TOTAL_BYTES:
                await ws.close(code=1009, reason="VAD session byte budget exceeded")
                return
            if session._rate_window_bytes + len(data) > MAX_VAD_BYTES_PER_SECOND:
                await ws.close(code=1013, reason="VAD frame rate exceeded")
                return
            session.total_bytes += len(data)
            session._rate_window_bytes += len(data)
            for msg in session.feed(data):
                await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("VAD websocket error")
        try:
            await ws.close()
        except Exception:
            pass
