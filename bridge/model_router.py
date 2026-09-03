"""Single-slot llama.cpp model router.

DSH's pi-ai profile carries `baseURL` on the provider, not on the model, so a
second local model would otherwise need a second provider on a second port --
and a second resident copy of the weights.  This router keeps one llama-server
alive at a time behind one port: a request naming a different model id stops the
child and starts the next one before proxying.

Everything stays on loopback and no request leaves the machine.  The child keeps
llama.cpp's own `--sleep-idle-seconds`, so an idle model releases its weights
(measured on this box: 8.9 GB RSS -> 0.1 GB) without needing a swap at all.

Run it in place of llama-server:

    .venv/bin/python -m uvicorn bridge.model_router:app --host 127.0.0.1 --port 8090
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# Hang off uvicorn's error logger: it is the one uvicorn attaches a handler to,
# so swap messages land in the same stream as the access log instead of being
# swallowed by the root logger's lastResort WARNING filter.
LOGGER = logging.getLogger("uvicorn.error").getChild("model-router")

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(
    os.environ.get("LOCAL_MODELS_CONFIG", REPO_ROOT / "config" / "local-models.json")
)
# Child readiness and swap bounds.  A 23 GiB Q6 model off a cold page cache is
# the slow case these have to tolerate without giving up on a healthy child.
CHILD_START_TIMEOUT_SEC = float(os.environ.get("ROUTER_START_TIMEOUT_SEC", "300"))
CHILD_STOP_TIMEOUT_SEC = float(os.environ.get("ROUTER_STOP_TIMEOUT_SEC", "30"))
IDLE_SLEEP_SECONDS = os.environ.get("LLM_IDLE_SLEEP", "600")

# Per-hop headers must not be forwarded; Content-Length is recomputed by the
# client and by Starlette, and forwarding a stale one truncates the body.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)


class ModelSpec:
    """One switchable model: its API id, display name, and GGUF on disk."""

    __slots__ = ("id", "name", "path", "context_size")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.id = str(raw["id"])
        self.name = str(raw.get("name") or self.id)
        self.path = Path(str(raw["path"])).expanduser()
        self.context_size = int(raw.get("contextSize") or 32768)


class Catalog:
    """The parsed model table plus the port the child listens on."""

    def __init__(self, path: Path) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        self.upstream_port = int(document.get("upstreamPort") or 8190)
        self.models: dict[str, ModelSpec] = {}
        for raw in document.get("models") or []:
            spec = ModelSpec(raw)
            self.models[spec.id] = spec
        if not self.models:
            raise RuntimeError(f"model catalog has no models: {path}")
        # start.sh's --model sets the override so the launcher can pick which
        # model is warm without editing the catalog.
        default = (
            os.environ.get("ROUTER_DEFAULT_MODEL")
            or document.get("default")
            or next(iter(self.models))
        )
        if default not in self.models:
            raise RuntimeError(f"default model {default!r} is not in the catalog")
        self.default = str(default)

    def present(self) -> list[ModelSpec]:
        """Only the entries whose weights actually exist on this machine."""
        return [spec for spec in self.models.values() if spec.path.is_file()]


class Router:
    """Owns the single child process and serializes swaps behind one lock."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.upstream = f"http://127.0.0.1:{catalog.upstream_port}"
        self.current: str | None = None
        self.child: subprocess.Popen[bytes] | None = None
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
        self.last_error: str | None = None

    # -- child lifecycle ---------------------------------------------------

    def _spawn(self, spec: ModelSpec) -> subprocess.Popen[bytes]:
        binary = os.environ.get("LLAMA_SERVER_BIN") or shutil.which("llama-server")
        if not binary:
            raise RuntimeError("llama-server not found on PATH")
        log_dir = REPO_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = (log_dir / f"llm-{spec.id}.log").open("ab")
        environment = dict(os.environ)
        # llama.cpp reads LLAMA_API_KEY as a *server-side* auth key, and this
        # machine exports one for an unrelated corp service; leaving it set
        # makes every loopback client fail with 401.
        environment.pop("LLAMA_API_KEY", None)
        command = [
            binary,
            "--model", str(spec.path),
            # Echo the catalog id back, so a response's `model` matches what the
            # caller asked for instead of a fixed alias that hides the swap.
            "--alias", spec.id,
            "--host", "127.0.0.1",
            "--port", str(self.catalog.upstream_port),
            "--ctx-size", str(spec.context_size),
            "--parallel", "1",
            # Measured on this M4 Max: flash-attn is 2.1x decode at depth 8192,
            # and K and V must be quantized together or throughput collapses.
            "--flash-attn", "on",
            "-ctk", "q8_0", "-ctv", "q8_0",
            "-ub", "2048", "-b", "2048",
            "--jinja",
            "--reasoning", "off",
            "--reasoning-budget", "0",
            "--sleep-idle-seconds", str(IDLE_SLEEP_SECONDS),
        ]
        LOGGER.info("starting %s (%s)", spec.id, spec.path.name)
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )

    def _stop_child(self) -> None:
        child = self.child
        self.child = None
        self.current = None
        if child is None or child.poll() is not None:
            return
        LOGGER.info("stopping child pid %s", child.pid)
        child.terminate()
        deadline = time.monotonic() + CHILD_STOP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return
            time.sleep(0.2)
        LOGGER.warning("child pid %s ignored SIGTERM; sending SIGKILL", child.pid)
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)

    async def _await_child_health(self, spec: ModelSpec) -> None:
        deadline = time.monotonic() + CHILD_START_TIMEOUT_SEC
        while time.monotonic() < deadline:
            child = self.child
            if child is not None and child.poll() is not None:
                raise RuntimeError(
                    f"llama-server for {spec.id} exited with code {child.returncode};"
                    f" see logs/llm-{spec.id}.log"
                )
            try:
                response = await self.client.get(f"{self.upstream}/health", timeout=3.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)
        raise RuntimeError(f"llama-server for {spec.id} did not become healthy in time")

    async def ensure(self, requested: str | None) -> ModelSpec:
        """Make `requested` the loaded model, swapping the child when needed.

        An unknown or absent id keeps whatever is already loaded rather than
        failing: the voice runtime and other probes address this port without
        knowing the catalog, and refusing them would take the stack down for a
        request that never cared which model answered.
        """
        wanted = requested if requested in self.catalog.models else None
        if wanted is None:
            wanted = self.current or self.catalog.default
        async with self.lock:
            if self.current == wanted and self.child is not None and self.child.poll() is None:
                return self.catalog.models[wanted]
            spec = self.catalog.models[wanted]
            if not spec.path.is_file():
                raise HTTPException(
                    status_code=503,
                    detail=f"model weights are missing for {spec.id}: {spec.path}",
                )
            await asyncio.to_thread(self._stop_child)
            self.child = await asyncio.to_thread(self._spawn, spec)
            try:
                await self._await_child_health(spec)
            except Exception as error:  # noqa: BLE001 - reported to the caller
                self.last_error = str(error)
                await asyncio.to_thread(self._stop_child)
                raise HTTPException(status_code=503, detail=str(error)) from error
            self.current = wanted
            self.last_error = None
            LOGGER.info("loaded %s", wanted)
            return spec

    async def aclose(self) -> None:
        await asyncio.to_thread(self._stop_child)
        await self.client.aclose()


CATALOG = Catalog(CATALOG_PATH)
ROUTER = Router(CATALOG)
app = FastAPI(title="Xiaoman local model router")


@app.on_event("startup")
async def _preload() -> None:
    """Warm the default model so the first real turn is not a cold load."""
    if os.environ.get("ROUTER_PRELOAD", "1") != "1":
        return
    try:
        await ROUTER.ensure(CATALOG.default)
    except Exception as error:  # noqa: BLE001 - startup must stay non-fatal
        LOGGER.warning("preload of %s failed: %s", CATALOG.default, error)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await ROUTER.aclose()


@app.get("/health")
async def health() -> JSONResponse:
    """Always answers, so a swap or a cold start never fails the stack probe."""
    return JSONResponse(
        {
            "status": "ok",
            "loaded": ROUTER.current,
            "default": CATALOG.default,
            "available": [spec.id for spec in CATALOG.present()],
            "last_error": ROUTER.last_error,
        }
    )


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """The catalog, not the child's single entry -- the UI lists all of them."""
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": spec.id,
                    "object": "model",
                    "owned_by": "local",
                    "name": spec.name,
                }
                for spec in CATALOG.present()
            ],
        }
    )


def _forward_headers(source: Any) -> dict[str, str]:
    return {k: v for k, v in source.items() if k.lower() not in HOP_BY_HOP}


async def _proxy(request: Request, path: str, body: bytes) -> StreamingResponse:
    url = f"{ROUTER.upstream}/{path}"
    upstream_request = ROUTER.client.build_request(
        request.method,
        url,
        content=body or None,
        headers=_forward_headers(request.headers),
        params=request.query_params,
    )
    try:
        response = await ROUTER.client.send(upstream_request, stream=True)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"upstream failed: {error}") from error
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=_forward_headers(response.headers),
        background=BackgroundTask(response.aclose),
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request) -> StreamingResponse:
    """Every other route is llama.cpp's; only the model id is inspected."""
    body = await request.body()
    requested: str | None = None
    if body:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            value = payload.get("model")
            requested = value if isinstance(value, str) else None
    await ROUTER.ensure(requested)
    return await _proxy(request, path, body)
