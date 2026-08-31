"""Async stdio JSONL client for the official Codex app-server protocol."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import math
import os
import signal
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .compatibility import (
    EXPECTED_CLI_VERSION,
    STABLE_EXPERIMENTAL_API,
    STABLE_REQUEST_ATTESTATION,
    ProtocolCompatibilityGate,
    ProtocolInfo,
)
from .schema_validator import (
    INERT_SERVER_NOTIFICATION_METHODS,
    CodexSchemaValidationError,
    StableProtocolValidator,
)
from .types import CodexError, CodexProcessError, CodexTimeoutError


# These lifecycle sentinels are local subscriber messages; they never travel
# on Codex stdio and deliberately cannot collide with generated wire methods.
INTERNAL_APP_SERVER_EXITED = "dsh/app-server/exited"
INTERNAL_APP_SERVER_ISOLATION_FAILED = "dsh/app-server/isolation-failed"

_MAX_LOCAL_REQUEST_ID = 0x7FFFFFFF
_RESPONSE_TOMBSTONE_LIMIT = 512
_STDERR_DRAIN_CHUNK_BYTES = 16 * 1024


@dataclass(frozen=True)
class AppServerConfig:
    """Process and protocol timeouts; no credential fields are accepted."""

    command: tuple[str, ...] = ("codex", "app-server", "--stdio")
    startup_timeout: float = 15.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 3.0
    subscriber_queue_size: int = 256
    client_name: str = "xiaoman-dsh"
    client_title: str = "Xiaoman DSH direct Codex agent"
    client_version: str = "0.1.0"
    expected_cli_version: str | None = EXPECTED_CLI_VERSION
    external_chatgpt_auth: bool = False
    managed_token_refresh: bool = False
    isolated_home: str | None = None

    def __post_init__(self) -> None:
        maxima = {
            "startup_timeout": 120.0,
            "request_timeout": 300.0,
            "shutdown_timeout": 30.0,
        }
        for name, maximum in maxima.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"{name} is outside the audited bound")
        if (
            isinstance(self.subscriber_queue_size, bool)
            or not isinstance(self.subscriber_queue_size, int)
            or not 1 <= self.subscriber_queue_size <= 4096
        ):
            raise ValueError("subscriber_queue_size is outside the audited bound")
        if type(self.external_chatgpt_auth) is not bool or type(self.managed_token_refresh) is not bool:
            raise ValueError("Codex authentication capability flags must be boolean")
        if self.external_chatgpt_auth and self.managed_token_refresh:
            raise ValueError("one Codex process cannot own managed and external authentication")
        if self.isolated_home is not None:
            path = Path(self.isolated_home)
            if not path.is_absolute() or len(str(path)) > 4096:
                raise ValueError("Codex isolated home must be an absolute path")


class JsonRpcError(CodexError):
    """A JSON-RPC error response from app-server."""

    def __init__(self, code: int | str | None, message: str, data: Any = None) -> None:
        super().__init__(message, code=code)
        self.message = message
        self.data = data


class CodexAmbiguousRequestError(CodexProcessError):
    """A written request did not produce an authoritative response.

    ``method`` is drawn from the local stable-method allowlist.  The exception
    deliberately carries no response, stderr, path, or prompt material; its
    only purpose is to tell the owner that an operation may have taken effect
    and therefore needs a process-isolation fence before state can be released.
    """

    def __init__(self, method: str) -> None:
        super().__init__(
            "codex app-server request outcome is ambiguous",
            code="request_outcome_ambiguous",
        )
        self.method = method


class _PostWriteError(CodexProcessError):
    """The stdio write returned, but its drain did not complete cleanly."""


class _InvalidJsonFrame(ValueError):
    """Internal marker for duplicate/non-finite untrusted JSON."""


@dataclass(frozen=True)
class _PendingRequest:
    method: str
    params: Mapping[str, Any]
    future: asyncio.Future[dict[str, Any]]


def _unique_frame_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _InvalidJsonFrame
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise _InvalidJsonFrame


def _classify_wire_message(message: Mapping[str, Any]) -> str | None:
    """Return one exact JSON-RPC envelope kind, or fail closed."""

    keys = set(message)
    if "jsonrpc" in message and message.get("jsonrpc") != "2.0":
        return None
    optional = {"jsonrpc"}
    if (
        "id" in message
        and ("result" in message) ^ ("error" in message)
        and keys.issubset({"id", "result", "error"} | optional)
    ):
        return "response"
    if (
        {"id", "method", "params"}.issubset(keys)
        and keys.issubset({"id", "method", "params"} | optional)
    ):
        return "request"
    if (
        {"method", "params"}.issubset(keys)
        and keys.issubset({"method", "params", "emittedAtMs"} | optional)
    ):
        return "notification"
    return None



def _critical_notification(message: Mapping[str, Any]) -> bool:
    """Methods that must reach a turn/auth consumer to avoid a ghost turn."""

    return message.get("method") in {
        "turn/completed",
        INTERNAL_APP_SERVER_EXITED,
        INTERNAL_APP_SERVER_ISOLATION_FAILED,
        "server/request/denied",
        "item/completed",
        "account/login/completed",
    }


def _route_key(message: Mapping[str, Any]) -> tuple[Any, Any, Any, Any, Any]:
    params = message.get("params")
    if not isinstance(params, Mapping):
        params = {}
    method = message.get("method")
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    item_id: object = None
    correlation: object = None
    if method == "turn/completed":
        # The stable envelope carries identity in params.turn.id; turnId is
        # not part of TurnCompletedNotification and would collapse every real
        # turn onto the same reserved-lane route.
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, Mapping) else None
    elif method == "item/completed":
        item = params.get("item")
        item_id = item.get("id") if isinstance(item, Mapping) else None
    elif method == "account/login/completed":
        correlation = params.get("loginId")
    elif method == "server/request/denied":
        correlation = params.get("requestId")
    elif method not in {
        INTERNAL_APP_SERVER_EXITED,
        INTERNAL_APP_SERVER_ISOLATION_FAILED,
    }:
        # This fallback is unreachable for the current closed critical-method
        # set, but keeps future additions distinct until they define an exact
        # generated identity above.
        correlation = message.get("id")
    return (
        method,
        thread_id,
        turn_id,
        item_id,
        correlation,
    )


def _safe_route_identifier(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str) or not 0 < len(value) <= 256 or not value.isascii():
        return None
    if not all(character.isalnum() or character in "-_.:" for character in value):
        return None
    return value


def _same_notification_authority(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> bool:
    """Compare business authority while ignoring generated envelope metadata."""

    return first.get("method") == second.get("method") and first.get("params") == second.get("params")


class _PriorityNotificationQueue(asyncio.Queue[dict[str, Any]]):
    """Bounded queue with a small reserved lane for terminal notifications.

    Ordinary deltas/tool progress may be dropped under backpressure. Critical
    notifications use a bounded reserved lane. If even that lane saturates,
    the shared app-server is process-isolated before the owner publishes a
    real process terminal; a queue-local synthetic exit is never used while
    the process group may still be alive.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        # Keep one logical wire-order sequence even though ordinary and
        # reserved critical entries use separate bounded lanes.  The old
        # implementation always drained ``_priority`` first, so a later
        # turn/completed could overtake an earlier item/completed snapshot and
        # the provider would publish a truncated final answer.
        self._sequence = 0
        self._priority: deque[tuple[int, dict[str, Any]]] = deque()
        self._priority_limit = max(4, maxsize)
        self._overflowed = False

    @staticmethod
    def _message(value: tuple[int, dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
        return value[1] if isinstance(value, tuple) else value

    def _next(self, message: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._sequence += 1
        return self._sequence, message

    def _append_normal(self, item: tuple[int, dict[str, Any]]) -> None:
        self._queue.append(item)
        self._unfinished_tasks += 1
        self._wakeup_next(self._getters)

    def _drop_normal(self, index: int) -> None:
        del self._queue[index]
        self._unfinished_tasks = max(0, self._unfinished_tasks - 1)

    def put_nowait(self, item: dict[str, Any]) -> None:
        """Keep external Queue users on the same bounded/order-preserving path."""

        self.put_notification(item)

    def qsize(self) -> int:
        return len(self._queue) + len(self._priority)

    @property
    def overflowed(self) -> bool:
        """Whether a critical notification could not fit its bounded lanes."""

        return self._overflowed

    def force_notification(self, message: dict[str, Any]) -> None:
        """Replace a saturated queue with one authoritative process event."""

        # A queue overflow means the subscriber can no longer reconstruct
        # wire order.  Drop the buffered payload and retain only the real
        # process-facing terminal after AppServerClient has isolated the
        # process group; never synthesize a process exit while it is alive.
        dropped = len(self._queue) + len(self._priority)
        self._queue.clear()
        self._priority.clear()
        self._overflowed = False
        self._unfinished_tasks = max(0, self._unfinished_tasks - dropped)
        self._append_normal(self._next(message))

    def get_nowait(self) -> dict[str, Any]:
        candidates: list[tuple[str, int, dict[str, Any]]] = []
        if self._queue:
            sequence, message = self._queue[0]
            candidates.append(("normal", sequence, message))
        if self._priority:
            sequence, message = self._priority[0]
            candidates.append(("priority", sequence, message))
        if not candidates:
            raise asyncio.QueueEmpty
        lane, _sequence, message = min(candidates, key=lambda item: item[1])
        if lane == "normal":
            self._queue.popleft()
            self._wakeup_next(self._putters)
        elif lane == "priority":
            self._priority.popleft()
        return message

    async def get(self) -> dict[str, Any]:
        if self._queue or self._priority:
            return self.get_nowait()
        return await super().get()

    def put_notification(self, message: dict[str, Any]) -> None:
        item = self._next(message)
        critical = _critical_notification(message)
        # The ordinary lane has the configured base capacity.  Critical
        # entries can use the separate bounded reserve, but every lane carries
        # the same sequence and get_nowait() merges by that sequence.
        if len(self._queue) < self.maxsize:
            self._append_normal(item)
            return

        # A critical snapshot belongs in the reserved lane rather than
        # evicting an already queued delta prefix. Accessing the deques is
        # safe here: all writes happen on the event-loop thread, and a full
        # queue cannot have a waiting getter.
        if critical:
            if len(self._priority) < self._priority_limit:
                self._priority.append(item)
                self._unfinished_tasks += 1
                self._wakeup_next(self._getters)
                return
            # Coalesce only semantic-identical duplicates. A conflicting
            # terminal for the same exact route is a protocol conflict, not a
            # newer snapshot: preserve first authority and force the owner to
            # isolate through the ordinary overflow path.
            route = _route_key(message)
            for queued in self._priority:
                queued_message = self._message(queued)
                if _route_key(queued_message) == route:
                    if not _same_notification_authority(queued_message, message):
                        self._overflowed = True
                    return
            self._overflowed = True
            return

        # A non-critical update is expendable. Drop the newest update when the
        # ordinary lane is full: retaining the existing prefix lets a later
        # item/completed snapshot repair only the missing suffix instead of
        # speaking a mid-answer fragment out of order.
        return


NotificationSubscriber = _PriorityNotificationQueue
ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]
PostInitializeHook = Callable[["AppServerClient"], Awaitable[None]]
ChatgptTokenRefreshHandler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


def scrubbed_child_environment(*, isolated_home: str | None = None) -> dict[str, str]:
    """Return a default-deny environment for Codex and its tool children.

    ``HOME``/``CODEX_HOME`` are intentional: the official ChatGPT CLI stores
    its login state there. Everything else is admitted by an explicit
    non-secret runtime allowlist. In particular, credential brokers/locators
    such as SSH, Git, cloud SDK, container, proxy, and config-pointer
    variables must not cross the Host -> App Server process boundary.
    """

    allowed_exact = {
        "PATH", "HOME", "CODEX_HOME", "TMPDIR", "TMP", "TEMP", "LANG",
        "TERM", "SHELL", "USER", "LOGNAME", "__CF_USER_TEXT_ENCODING",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in allowed_exact or name.startswith("LC_")
    }
    if isolated_home is not None:
        environment["HOME"] = isolated_home
        environment["CODEX_HOME"] = str(Path(isolated_home) / ".codex")
    return environment


class AppServerClient:
    """Lazily start and reuse one app-server process per bridge process.

    The client owns exactly one JSONL reader and one JSONL writer lock.  Every
    request gets a local correlation id and pending Future; notifications are
    broadcast to subscribers so auth and turn routing can consume the same
    process stream without stealing each other's messages.
    """

    def __init__(
        self,
        config: AppServerConfig | None = None,
        *,
        logger: logging.Logger | None = None,
        process_factory: ProcessFactory | None = None,
        post_initialize: PostInitializeHook | None = None,
        chatgpt_token_refresh: ChatgptTokenRefreshHandler | None = None,
    ) -> None:
        self.config = config or AppServerConfig()
        if self.config.subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        self.logger = logger or logging.getLogger("agents.codex.app_server")
        self._process_factory = process_factory or self._create_process
        if post_initialize is not None and not self.config.external_chatgpt_auth:
            raise ValueError("post-initialize auth hook requires external ChatGPT auth")
        if chatgpt_token_refresh is not None and not self.config.external_chatgpt_auth:
            raise ValueError("ChatGPT token refresh handler requires external auth")
        self._post_initialize = post_initialize
        self._chatgpt_token_refresh = chatgpt_token_refresh
        self._process: asyncio.subprocess.Process | None = None
        # Monotonic ownership token for every spawned process. Object identity
        # alone is insufficient once a late callback resumes after restart:
        # all lifecycle side effects must belong to the exact generation.
        self._process_generation = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._response_tombstones: OrderedDict[
            int,
            tuple[str, Mapping[str, Any]],
        ] = OrderedDict()
        self._subscribers: set[NotificationSubscriber] = set()
        self._next_request_id = 1
        self._next_denial_sequence = 1
        self._closing = False
        self._exit_seen = False
        self._exit_hint: str | None = None
        self._isolation_failed = False
        self._last_error: str | None = None
        self._protocol_info: ProtocolInfo | None = None
        self._compatibility_gate = ProtocolCompatibilityGate(
            self.config.expected_cli_version,
            client_name=self.config.client_name,
            client_version=self.config.client_version,
        )
        # Verify the checked-in generated schema tree before this client can
        # ever create a Codex process or write a protocol frame.
        self._schema_validator = StableProtocolValidator(
            external_chatgpt_auth=self.config.external_chatgpt_auth,
            managed_token_refresh=self.config.managed_token_refresh,
        )
        # All callers of isolate() for one process share this complete
        # process-group kill/verification outcome, including the HTTP fallback
        # racing a WS isolate request.
        self._isolation_task: asyncio.Task[bool] | None = None
        # Reader EOF, the process waiter, explicit isolation, and bridge
        # shutdown can all discover the same process death concurrently. One
        # generation gets one in-flight termination attempt so those owners
        # cannot publish contradictory lifecycle authority.
        self._termination_task: asyncio.Task[bool] | None = None
        self._termination_process: asyncio.subprocess.Process | None = None
        self._termination_generation: int | None = None

    async def _create_process(self, *command: str) -> asyncio.subprocess.Process:
        # Never pass env=None: an app-server/tool child must not inherit
        # ambient API keys, cloud secrets, bearer tokens, or passwords.
        options: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": scrubbed_child_environment(isolated_home=self.config.isolated_home),
        }
        # Keep the app-server and any descendants in an isolated process group
        # so shutdown cannot leave a tool/child process behind.  The group id
        # is the app-server pid because start_new_session creates a new session
        # and process group for it.
        if os.name != "nt":
            options["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*command, **options)

    @property
    def started(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def subscribe(self, *, maxsize: int | None = None) -> NotificationSubscriber:
        queue_size = self.config.subscriber_queue_size if maxsize is None else maxsize
        if queue_size < 1:
            raise ValueError("subscriber queue maxsize must be positive")
        queue: NotificationSubscriber = _PriorityNotificationQueue(maxsize=queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: NotificationSubscriber) -> None:
        self._subscribers.discard(queue)

    async def ensure_started(self) -> None:
        if self._isolation_failed:
            raise CodexProcessError("codex app-server isolation requires reconciliation")
        if self.started:
            return
        initialize_params = self._schema_validator.validate_request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.config.client_name,
                    "title": self.config.client_title,
                    "version": self.config.client_version,
                },
                # Do not opt into experimental methods. This keeps the
                # protocol surface pinned to the stable v2 schema.
                "capabilities": {
                    "experimentalApi": (
                        True if self.config.external_chatgpt_auth else STABLE_EXPERIMENTAL_API
                    ),
                    "requestAttestation": STABLE_REQUEST_ATTESTATION,
                },
            },
        )
        async with self._lifecycle_lock:
            if self.started:
                return
            # A naturally exited process can leave reader/wait callbacks
            # suspended in teardown. Retire and join that whole generation
            # before resetting exit state or installing a new process.
            if self._process is not None or any(
                task is not None
                for task in (self._reader_task, self._stderr_task, self._wait_task)
            ):
                retired = await self._shutdown_locked()
                if not retired:
                    raise CodexProcessError(
                        "codex app-server process group could not be isolated"
                    )
            self._closing = False
            self._exit_seen = False
            self._exit_hint = None
            self._last_error = None
            self._isolation_task = None
            self._termination_task = None
            self._termination_process = None
            self._termination_generation = None
            self._response_tombstones.clear()
            try:
                process = await self._process_factory(*self.config.command)
            except Exception as exc:  # noqa: BLE001 - process boundary
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise CodexProcessError("failed to start codex app-server") from exc
            if process.stdin is None or process.stdout is None:
                await self._terminate_process(process)
                raise CodexProcessError("codex app-server did not expose stdio pipes")
            self._process_generation += 1
            generation = self._process_generation
            self._process = process
            self._isolation_failed = False
            self._reader_task = asyncio.create_task(self._reader_loop(process, generation))
            self._stderr_task = asyncio.create_task(self._stderr_loop(process, generation))
            self._wait_task = asyncio.create_task(self._wait_loop(process, generation))
            try:
                initialize_result = await self.request(
                    "initialize",
                    initialize_params,
                    ensure_started=False,
                    timeout=self.config.startup_timeout,
                )
                self._protocol_info = self._compatibility_gate.validate_initialize(initialize_result)
                await self.notify("initialized", ensure_started=False)
                if self._post_initialize is not None:
                    await self._post_initialize(self)
            except Exception:
                await self._shutdown_locked()
                raise

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        ensure_started: bool = True,
    ) -> dict[str, Any]:
        validated_params = self._schema_validator.validate_request(method, params)
        if ensure_started:
            await self.ensure_started()
        process = self._process
        generation = self._process_generation
        if process is None or process.stdin is None or not self.started:
            raise CodexProcessError("codex app-server is not running")
        request_id = self._allocate_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = _PendingRequest(
            method=method,
            params=validated_params,
            future=future,
        )
        written = False
        try:
            try:
                await self._send(
                    {
                        "id": request_id,
                        "method": method,
                        "params": validated_params,
                    },
                    expected_process=process,
                    generation=generation,
                )
                written = True
                try:
                    response = await asyncio.wait_for(
                        future,
                        timeout=self.config.request_timeout if timeout is None else timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise CodexTimeoutError(f"app-server request timed out: {method}") from exc
                if "error" in response:
                    error = response["error"]
                    assert isinstance(error, Mapping)
                    # A generated-schema-valid JSON-RPC error is an explicit
                    # rejection, not an uncertain successful dispatch.
                    raise JsonRpcError(
                        error.get("code"),
                        str(error.get("message") or "app-server request failed"),
                        error.get("data"),
                    )
                return self._schema_validator.validate_response(
                    method,
                    response.get("result"),
                    validated_params,
                )
            except JsonRpcError:
                raise
            except asyncio.CancelledError:
                # The provider's cancellation path owns the isolation fence;
                # retain cancellation semantics so it cannot be mistaken for
                # an ordinary start rejection.
                raise
            except CodexAmbiguousRequestError:
                raise
            except Exception as exc:
                if written or isinstance(exc, _PostWriteError):
                    raise CodexAmbiguousRequestError(method) from exc
                raise
        finally:
            pending = self._pending.pop(request_id, None)
            self._remember_response_id(
                request_id,
                pending.method if pending is not None else method,
                pending.params if pending is not None else validated_params,
            )

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        ensure_started: bool = True,
    ) -> None:
        message = self._schema_validator.validate_client_notification(method, params)
        if ensure_started:
            await self.ensure_started()
        process = self._process
        generation = self._process_generation
        if process is None or process.stdin is None or not self.started:
            raise CodexProcessError("codex app-server is not running")
        await self._send(
            message,
            expected_process=process,
            generation=generation,
        )

    async def _send(
        self,
        message: Mapping[str, Any],
        *,
        expected_process: asyncio.subprocess.Process | None = None,
        generation: int | None = None,
    ) -> None:
        process = self._process if expected_process is None else expected_process
        if process is None or process.stdin is None:
            raise CodexProcessError("codex app-server stdin is closed")
        if generation is not None and not self._owns_process(process, generation):
            raise CodexProcessError("codex app-server generation changed")
        line = (json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            if generation is not None and not self._owns_process(process, generation):
                raise CodexProcessError("codex app-server generation changed")
            write_returned = False
            try:
                process.stdin.write(line)
                write_returned = True
                await process.stdin.drain()
                if generation is not None and not self._owns_process(process, generation):
                    raise _PostWriteError("codex app-server generation changed after write")
            except Exception as exc:  # noqa: BLE001 - stdio process boundary
                if generation is None or self._owns_generation(process, generation):
                    self._last_error = "codex app-server stdin closed"
                if write_returned:
                    # Once StreamWriter.write() returned, a drain failure
                    # cannot prove that the peer did not receive the frame.
                    raise _PostWriteError("codex app-server stdin outcome is ambiguous") from exc
                raise CodexProcessError("codex app-server stdin closed") from exc

    def _owns_process(self, process: asyncio.subprocess.Process, generation: int) -> bool:
        return self._process is process and self._process_generation == generation

    def _owns_generation(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> bool:
        return (
            self._process_generation == generation
            and (self._process is process or self._process is None)
        )

    def _allocate_request_id(self) -> int:
        start = self._next_request_id
        while True:
            request_id = self._next_request_id
            self._next_request_id = (
                1 if request_id >= _MAX_LOCAL_REQUEST_ID else request_id + 1
            )
            if request_id not in self._pending and request_id not in self._response_tombstones:
                return request_id
            if self._next_request_id == start:
                raise CodexProcessError("codex app-server request id space exhausted")

    def _remember_response_id(
        self,
        request_id: int,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        self._response_tombstones[request_id] = (method, dict(params))
        self._response_tombstones.move_to_end(request_id)
        while len(self._response_tombstones) > _RESPONSE_TOMBSTONE_LIMIT:
            self._response_tombstones.popitem(last=False)

    async def _reader_loop(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        assert process.stdout is not None
        try:
            while True:
                if not self._owns_process(process, generation):
                    return
                line = await process.stdout.readline()
                if not self._owns_process(process, generation):
                    return
                if not line:
                    if not self._closing:
                        await self._terminate_and_mark_exit(
                            process,
                            generation,
                            "stdout EOF",
                        )
                    return
                try:
                    message = json.loads(
                        line.decode("utf-8"),
                        object_pairs_hook=_unique_frame_object,
                        parse_constant=_reject_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, _InvalidJsonFrame):
                    # A non-JSON stdout line makes correlation unsafe.  Do not
                    # forward or log it: fail closed and tear down the process
                    # group instead of trying to recover on a desynchronized
                    # stream.
                    self._exit_hint = "malformed stdout"
                    await self._terminate_and_mark_exit(
                        process,
                        generation,
                        "malformed stdout",
                    )
                    return
                if not isinstance(message, dict):
                    self._exit_hint = "invalid stdout message"
                    await self._terminate_and_mark_exit(
                        process,
                        generation,
                        "invalid stdout message",
                    )
                    return
                envelope = _classify_wire_message(message)
                if envelope is None:
                    self._exit_hint = "invalid protocol envelope"
                    await self._terminate_and_mark_exit(
                        process,
                        generation,
                        "invalid protocol envelope",
                    )
                    return
                if envelope == "response":
                    request_id = message.get("id")
                    if (
                        isinstance(request_id, bool)
                        or not isinstance(request_id, int)
                        or not 0 < request_id <= _MAX_LOCAL_REQUEST_ID
                    ):
                        self._exit_hint = "invalid protocol response"
                        await self._terminate_and_mark_exit(
                            process,
                            generation,
                            "invalid protocol response",
                        )
                        return
                    pending = self._pending.get(request_id)
                    tombstone = self._response_tombstones.get(request_id)
                    if pending is None and tombstone is None:
                        self._exit_hint = "invalid protocol response"
                        await self._terminate_and_mark_exit(
                            process,
                            generation,
                            "invalid protocol response",
                        )
                        return
                    try:
                        if "result" in message:
                            response_method = pending.method if pending is not None else tombstone[0]
                            response_params = pending.params if pending is not None else tombstone[1]
                            self._schema_validator.validate_response(
                                response_method,
                                message.get("result"),
                                response_params,
                            )
                        else:
                            self._schema_validator.validate_error_response(message)
                    except CodexSchemaValidationError as exc:
                        if pending is not None and not pending.future.done():
                            pending.future.set_exception(exc)
                        self._exit_hint = "invalid protocol response"
                        await self._terminate_and_mark_exit(
                            process,
                            generation,
                            "invalid protocol response",
                        )
                        return
                    if pending is not None and not pending.future.done():
                        pending.future.set_result(message)
                    continue
                if envelope in {"request", "notification"}:
                    if envelope == "request":
                        try:
                            business_known = self._schema_validator.validate_server_request(message)
                        except CodexSchemaValidationError:
                            self._exit_hint = "invalid protocol server request"
                            await self._terminate_and_mark_exit(
                                process,
                                generation,
                                "invalid protocol server request",
                            )
                            return
                        if (
                            business_known
                            and message.get("method") == "account/chatgptAuthTokens/refresh"
                            and self._chatgpt_token_refresh is not None
                        ):
                            await self._answer_chatgpt_token_refresh(
                                message,
                                process=process,
                                generation=generation,
                            )
                        else:
                            await self._deny_server_request(
                                message,
                                business_known=business_known,
                                process=process,
                                generation=generation,
                            )
                    else:
                        try:
                            method = self._schema_validator.validate_server_notification(message)
                        except CodexSchemaValidationError:
                            self._exit_hint = "invalid protocol notification"
                            await self._terminate_and_mark_exit(
                                process,
                                generation,
                                "invalid protocol notification",
                            )
                            return
                        if method in INERT_SERVER_NOTIFICATION_METHODS:
                            # This stable bootstrap status is required on the
                            # wire but carries remote identity fields that no
                            # DSH consumer needs. Validate the complete
                            # generated envelope above, then deliberately do
                            # not enqueue or interpret it.
                            continue
                        await self._broadcast(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - process boundary
            if not self._owns_process(process, generation):
                return
            self._exit_hint = f"stdout reader failed: {type(exc).__name__}"
            await self._terminate_and_mark_exit(
                process,
                generation,
                f"stdout reader failed: {type(exc).__name__}",
            )

    async def _stderr_loop(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        """Drain stderr without exposing arbitrary app-server text/secrets."""

        if process.stderr is None:
            return
        try:
            while self._owns_process(process, generation):
                # StreamReader.readline() raises LimitOverrunError once one
                # unterminated diagnostic exceeds its internal 64 KiB limit.
                # Fixed-size reads keep draining an adversarial/no-newline
                # stderr stream without retaining or exposing its content.
                chunk = await process.stderr.read(_STDERR_DRAIN_CHUNK_BYTES)
                if not self._owns_process(process, generation) or not chunk:
                    return
                # Never log the chunk: app-server diagnostics could contain
                # provider/account material, paths, or user prompt fragments.
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _wait_loop(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        try:
            await process.wait()
            if self._owns_process(process, generation) and not self._closing:
                # The parent may have exited while a tool/child descendant
                # still belongs to its isolated process group.  Reap that
                # group even on the EOF/wait path; ``close()`` is not required
                # to be called by a higher layer for tree cleanup.
                await self._terminate_and_mark_exit(
                    process,
                    generation,
                    "process exited",
                )
        except asyncio.CancelledError:
            raise

    async def _terminate_and_mark_exit(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
        reason: str,
    ) -> bool:
        """Verify teardown before publishing an internal lifecycle event."""

        if not self._owns_process(process, generation):
            return True
        terminated = await self._terminate_generation(process, generation)
        if not self._owns_generation(process, generation):
            # A newer generation owns all shared exit/error/subscriber state.
            return terminated
        if terminated:
            await self._mark_exit(
                self._exit_hint or reason,
                process=process,
                generation=generation,
            )
        else:
            self._isolation_failed = True
            # A failed group verification is not an authoritative app-server
            # exit: publishing one would let a Host release maintenance
            # while a tool descendant can still execute. Use a distinct
            # internal failure notification so the provider/Host quarantine
            # path remains non-authoritative until reconciliation.
            await self._mark_isolation_failed(
                process=process,
                generation=generation,
            )
        return terminated

    async def _deny_server_request(
        self,
        message: Mapping[str, Any],
        *,
        business_known: bool,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        if not self._owns_process(process, generation):
            return
        request_id = message.get("id")
        method = str(message.get("method")) if business_known else "unknown"
        # Fail closed for approvals, user input, MCP elicitation, and any new
        # server request added by a future Codex version. The bridge never
        # fabricates permission/input answers.
        try:
            await self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": "server request denied by DSH fail-closed policy",
                    },
                },
                expected_process=process,
                generation=generation,
            )
        except CodexError:
            pass
        denial_sequence = self._next_denial_sequence
        self._next_denial_sequence = (
            1 if denial_sequence >= 0xFFFFFFFFFFFFFFFF else denial_sequence + 1
        )
        params: dict[str, Any] = {
            # Request ids may be arbitrary server-controlled strings. The
            # wire denial echoes the exact id, but the observable event uses a
            # bounded local correlation and never reflects the raw value into
            # browser-visible data. Distinct requests therefore cannot
            # coalesce into one critical queue entry merely because their
            # safe marker was constant.
            "requestId": f"server-request-{denial_sequence:016x}",
            "method": method,
        }
        # Preserve only routing identifiers from the original request.  Never
        # echo arbitrary request params (which may contain prompt/tool data).
        for key in ("threadId", "turnId"):
            value = message.get("params")
            if isinstance(value, Mapping):
                value = value.get(key)
            safe_value = _safe_route_identifier(value)
            if safe_value is not None:
                params[key] = safe_value
        if not self._owns_process(process, generation):
            return
        await self._broadcast(
            {
                "method": "server/request/denied",
                "params": params,
            }
        )

    async def _answer_chatgpt_token_refresh(
        self,
        message: Mapping[str, Any],
        *,
        process: asyncio.subprocess.Process,
        generation: int,
    ) -> None:
        """Answer the one pinned external-auth request on private stdio."""

        if not self._owns_process(process, generation) or self._chatgpt_token_refresh is None:
            return
        try:
            value = dict(await self._chatgpt_token_refresh(message.get("params", {})))
            if (
                set(value) != {"accessToken", "chatgptAccountId", "chatgptPlanType"}
                or not isinstance(value.get("accessToken"), str)
                or not 0 < len(value["accessToken"]) <= 32_768
                or not isinstance(value.get("chatgptAccountId"), str)
                or not 0 < len(value["chatgptAccountId"]) <= 256
                or not (
                    value.get("chatgptPlanType") is None
                    or isinstance(value.get("chatgptPlanType"), str)
                )
            ):
                raise ValueError("invalid external ChatGPT token refresh result")
            response: dict[str, Any] = {"id": message.get("id"), "result": value}
        except Exception:
            response = {
                "id": message.get("id"),
                "error": {
                    "code": -32002,
                    "message": "external ChatGPT authentication refresh failed",
                },
            }
        try:
            await self._send(
                response,
                expected_process=process,
                generation=generation,
            )
        except CodexError:
            pass

    async def _broadcast(self, message: dict[str, Any]) -> None:
        overflowed = False
        for queue in tuple(self._subscribers):
            queue.put_notification(message)
            overflowed = overflowed or queue.overflowed
        if overflowed and message.get("method") not in {
            INTERNAL_APP_SERVER_EXITED,
            INTERNAL_APP_SERVER_ISOLATION_FAILED,
        }:
            # A critical overflow invalidates the subscriber's wire order for
            # the whole shared app-server.  Isolate first; _mark_exit then
            # force-delivers the internal verified-exit marker to every queue.
            await self.isolate("subscriber queue saturated")

    async def _mark_exit(
        self,
        reason: str,
        *,
        process: asyncio.subprocess.Process | None = None,
        generation: int | None = None,
    ) -> None:
        if (
            process is not None
            and generation is not None
            and not self._owns_generation(process, generation)
        ):
            return
        if self._exit_seen:
            return
        self._exit_seen = True
        self._last_error = reason
        error = CodexProcessError("codex app-server process exited")
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
        message = {"method": INTERNAL_APP_SERVER_EXITED, "params": {"reason": reason}}
        for queue in tuple(self._subscribers):
            self._publish_authoritative_terminal(queue, message)

    async def _mark_isolation_failed(
        self,
        *,
        process: asyncio.subprocess.Process | None = None,
        generation: int | None = None,
    ) -> None:
        """Publish only a non-authoritative process-group failure marker."""

        if (
            process is not None
            and generation is not None
            and not self._owns_generation(process, generation)
        ):
            return
        if self._exit_seen:
            return
        self._exit_seen = True
        self._last_error = "isolation_failed"
        error = CodexProcessError("codex app-server process isolation failed")
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()
        message = {
            "method": INTERNAL_APP_SERVER_ISOLATION_FAILED,
            "params": {"reason": "isolation_failed"},
        }
        for queue in tuple(self._subscribers):
            self._publish_authoritative_terminal(queue, message)

    @staticmethod
    def _publish_authoritative_terminal(
        queue: NotificationSubscriber,
        message: dict[str, Any],
    ) -> None:
        """Publish an app-server terminal even when the queue fills on this put.

        ``put_notification`` sets ``overflowed`` when a critical message
        discovers that both the ordinary and reserved lanes are full.  A
        verified app-server terminal cannot wait for another broadcast to
        trigger the existing force path: the provider would otherwise wait
        forever for a terminal that was dropped on this exact put. Preserve
        the queued prefix when the terminal fits, and replace the queue
        immediately when this write makes its state lossy.
        """

        queue.put_notification(message)
        if queue.overflowed:
            queue.force_notification(message)

    async def _terminate_generation(
        self,
        process: asyncio.subprocess.Process,
        generation: int,
        *,
        retry_failed: bool = False,
    ) -> bool:
        """Join the single in-flight termination attempt for one generation."""

        if not self._owns_process(process, generation):
            return True
        task = self._termination_task
        matching_task = (
            task is not None
            and self._termination_process is process
            and self._termination_generation == generation
        )
        retry_complete_failure = False
        if matching_task and task is not None and task.done() and retry_failed:
            retry_complete_failure = (
                task.cancelled()
                or task.exception() is not None
                or task.result() is False
            )
        if (
            task is None
            or not matching_task
            or retry_complete_failure
        ):
            task = asyncio.create_task(
                self._terminate_process(process, generation=generation)
            )
            self._termination_task = task
            self._termination_process = process
            self._termination_generation = generation
        # A canceled reader must not cancel the process-wide teardown that
        # the waiter, isolate path, or shutdown path also owns. Keep the
        # completed outcome generation-sticky for late callbacks; only an
        # explicit shutdown reconciliation may retry a failed verification.
        return await asyncio.shield(task)

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        generation: int | None = None,
    ) -> bool:
        def still_owned() -> bool:
            return generation is None or self._owns_process(process, generation)

        if not still_owned():
            return True
        # One bounded shutdown budget covers both the graceful TERM phase and
        # the forced KILL/reap phase. Reserve half for SIGKILL so a hung
        # ``process.wait()`` cannot consume the whole budget before the owned
        # process group is force-isolated.
        shutdown_budget = max(0.05, self.config.shutdown_timeout)
        shutdown_deadline = time.monotonic() + shutdown_budget
        graceful_deadline = time.monotonic() + (shutdown_budget / 2)

        # Signal the original process group even when the parent already
        # exited: an app-server can leave a tool child behind in that group.
        # The pid is captured from our own subprocess, never a broad pattern.
        if not still_owned():
            return True
        try:
            self._signal_process_group(process, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        if process.returncode is None:
            remaining = graceful_deadline - time.monotonic()
            try:
                if remaining > 0:
                    await asyncio.wait_for(process.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            except (ProcessLookupError, OSError):
                pass
        if not still_owned():
            return True
        while os.name != "nt" and time.monotonic() < graceful_deadline:
            if not still_owned():
                return True
            if not self._process_group_exists(process.pid):
                # ``returncode`` is asyncio's proof that the parent was
                # reaped. A missing group plus an unreaped/hung parent remains
                # indeterminate and proceeds to the bounded KILL phase.
                if process.returncode is not None:
                    return True
                break
            await asyncio.sleep(max(0.0, min(0.02, graceful_deadline - time.monotonic())))
            if not still_owned():
                return True
        if not still_owned():
            return True
        try:
            self._signal_process_group(process, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        reaped = process.returncode is not None
        remaining = shutdown_deadline - time.monotonic()
        if not reaped and remaining > 0:
            try:
                await asyncio.wait_for(process.wait(), timeout=remaining)
                reaped = True
            except asyncio.TimeoutError:
                pass
            except (ProcessLookupError, OSError):
                pass
        if not still_owned():
            return True
        if not reaped:
            return False
        if os.name == "nt":
            return True
        # A successful wait() only proves that the app-server parent exited;
        # tool descendants can still hold the process group.  Do not let an
        # isolate caller claim interrupt_isolated until the exact group is
        # observed gone.
        while time.monotonic() < shutdown_deadline:
            if not still_owned():
                return True
            if not self._process_group_exists(process.pid):
                return True
            await asyncio.sleep(max(0.0, min(0.02, shutdown_deadline - time.monotonic())))
            if not still_owned():
                return True
        if not still_owned():
            return True
        if not self._process_group_exists(process.pid):
            return True
        return False

    @staticmethod
    def _process_group_exists(pid: int) -> bool:
        if os.name == "nt":
            return False
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            # ESRCH is the only portable proof that the target group is gone.
            # EPERM/EACCES and all other OS errors are lack of authority or an
            # indeterminate state; fail closed and keep treating the group as
            # live so an isolate caller cannot publish a false terminal.
            if exc.errno == errno.ESRCH:
                return False
            return True
        return True

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, sig: int) -> None:
        if os.name == "nt":
            if sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
            return
        # start_new_session=True makes process.pid the process-group id.
        # Never use a shell or a broad `pkill`: this targets only our child
        # group and is safe to call again when the process is racing shutdown.
        os.killpg(process.pid, sig)

    async def _shutdown_locked(self) -> bool:
        self._closing = True
        process = self._process
        generation = self._process_generation
        # Queue-overflow isolation can be initiated by the stdout reader
        # itself.  Never gather the current task: doing so creates a
        # self-await deadlock before the process group can be verified gone.
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (self._reader_task, self._stderr_task, self._wait_task)
            if task is not None and task is not current
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = self._stderr_task = self._wait_task = None
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(CodexProcessError("codex app-server stopped"))
        self._pending.clear()
        if process is not None:
            terminated = await self._terminate_generation(
                process,
                generation,
                retry_failed=True,
            )
            if not self._owns_process(process, generation):
                # A lifecycle owner may only mutate the process handle and
                # poison state for the generation it captured.
                return terminated
            # Retain the exact process handle when group verification fails so
            # an explicit close/reconciliation can retry the same target;
            # never silently drop an unisolated process group.
            if terminated:
                self._process = None
                self._isolation_failed = False
            else:
                self._process = process
                self._isolation_failed = True
                self._last_error = "codex app-server process group did not exit"
            return terminated
        self._isolation_failed = False
        return True

    async def close(self) -> None:
        async with self._lifecycle_lock:
            terminated = await self._shutdown_locked()
            if not terminated:
                raise CodexProcessError("codex app-server process group could not be isolated")

    async def isolate(self, reason: str = "interrupt timeout") -> bool:
        """Fail closed by terminating this app-server process group.

        A missing authoritative terminal makes the rollout state uncertain.
        The safe recovery is to isolate the shared process (all active turns
        receive the internal ``dsh/app-server/exited`` sentinel) and allow a
        later request to lazy-restart a fresh process.  ``reason`` is reduced
        to a boundary-owned marker and never forwarded to the browser.
        """

        _ = reason  # reason is intentionally not emitted across the boundary.
        if self._isolation_task is None:
            self._isolation_task = asyncio.create_task(self._isolate_once())
        return await asyncio.shield(self._isolation_task)

    async def _isolate_once(self) -> bool:
        async with self._lifecycle_lock:
            process = self._process
            generation = self._process_generation
            if process is None:
                return False
            terminated = await self._shutdown_locked()
            if not terminated:
                raise CodexProcessError("codex app-server process group could not be isolated")
            # Do not publish the internal app-server terminal until the exact
            # process group has been observed gone. A parent wait alone is
            # insufficient when a tool descendant survives.
            if not self._exit_seen:
                await self._mark_exit(
                    self._exit_hint or "isolated app-server",
                    process=process,
                    generation=generation,
                )
            return True

    async def health(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "pending_requests": len(self._pending),
            "last_error": _safe_health_error(self._last_error),
            "protocol": {
                "cli_version": self._protocol_info.cli_version if self._protocol_info else None,
                "schema_sha256": self._protocol_info.schema_sha256 if self._protocol_info else None,
            },
        }


def _safe_health_error(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    if "malformed" in normalized or "invalid stdout" in normalized:
        return "malformed_stdout"
    if "isolation" in normalized and "failed" in normalized:
        return "isolation_failed"
    if "isolat" in normalized:
        return "isolated"
    if "exit" in normalized or "closed" in normalized:
        return "process_exit"
    return "process_error"
