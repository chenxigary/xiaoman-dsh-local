"""Fail-closed runtime validation for the pinned stable Codex protocol.

The generated schema tree is an audited build artifact, not a discovery
mechanism.  Outbound calls deliberately use a nine-method business allowlist
and one schema per method; the 95-method generated ``ClientRequest`` union is
never accepted as the runtime capability surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from jsonschema import Draft7Validator

from .compatibility import (
    EXPECTED_CLI_VERSION,
    STABLE_PROTOCOL_MANIFEST,
    STABLE_SCHEMA_BUNDLE_SHA256,
)
from .types import CodexCompatibilityError


_SCHEMA_ROOT = Path(__file__).with_name("generated") / "stable"
_BUNDLE_RELATIVE = "codex_app_server_protocol.v2.schemas.json"

_MAX_THREAD_OR_TURN_ID = 512
_MAX_ITEM_OR_LOGIN_ID = 256
_MAX_AUTH_URL = 2048

_REQUEST_SCHEMA_FILES: Mapping[str, str] = {
    "initialize": "v1/InitializeParams.json",
    "account/read": "v2/GetAccountParams.json",
    "account/login/start": "v2/LoginAccountParams.json",
    "account/login/cancel": "v2/CancelLoginAccountParams.json",
    "model/list": "v2/ModelListParams.json",
    "thread/start": "v2/ThreadStartParams.json",
    "thread/resume": "v2/ThreadResumeParams.json",
    "turn/start": "v2/TurnStartParams.json",
    "turn/steer": "v2/TurnSteerParams.json",
    "turn/interrupt": "v2/TurnInterruptParams.json",
}

_RESPONSE_SCHEMA_FILES: Mapping[str, str] = {
    "initialize": "v1/InitializeResponse.json",
    "account/read": "v2/GetAccountResponse.json",
    "account/login/start": "v2/LoginAccountResponse.json",
    "account/login/cancel": "v2/CancelLoginAccountResponse.json",
    "model/list": "v2/ModelListResponse.json",
    "thread/start": "v2/ThreadStartResponse.json",
    "thread/resume": "v2/ThreadResumeResponse.json",
    "turn/start": "v2/TurnStartResponse.json",
    "turn/steer": "v2/TurnSteerResponse.json",
    "turn/interrupt": "v2/TurnInterruptResponse.json",
}

_SERVER_REQUEST_ALLOWLIST = frozenset(
    {
        "account/chatgptAuthTokens/refresh",
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
    }
)

INERT_SERVER_NOTIFICATION_METHODS = frozenset(
    {
        # Codex 0.149 emits this generated-stable status snapshot before the
        # account/read response. DSH neither exposes nor acts on its remote
        # identity fields; accepting it is required for the auth handshake,
        # and dropping it after full generated-envelope validation keeps the
        # browser/turn business surface closed.
        "remoteControl/status/changed",
        # External-token login emits this identity-bearing snapshot after the
        # typed login response. It is fully schema-validated and then dropped;
        # browser account state continues to come from the managed auth client.
        "account/updated",
        "account/rateLimits/updated",
        "mcpServer/startupStatus/updated",
        # The pinned CLI emits this empty generated notification after its
        # asynchronous skill scan settles. DSH does not consume skill
        # inventory from App Server, so validate the exact envelope and drop
        # it without widening the browser-facing event vocabulary.
        "skills/changed",
        # Resuming a persisted Codex thread replays its generated goal state.
        # DSH owns goals separately, so accept and discard only the exact
        # pinned notification instead of projecting it into the session.
        "thread/goal/cleared",
        # Thread creation/resume publishes the generated settings snapshot
        # before the turn starts.  DSH already owns the effective read-only
        # sandbox, model, and approval settings, so validate the complete
        # pinned envelope and discard this duplicate projection.
        "thread/settings/updated",
        "thread/status/changed",
        "thread/started",
        "thread/tokenUsage/updated",
    }
)

_SERVER_NOTIFICATION_ALLOWLIST = INERT_SERVER_NOTIFICATION_METHODS | frozenset(
    {
        "account/login/completed",
        "turn/started",
        "turn/completed",
        "item/started",
        "item/completed",
        "item/agentMessage/delta",
        "serverRequest/resolved",
    }
)

_REQUIRED_SCHEMA_FILES = frozenset(
    {
        _BUNDLE_RELATIVE,
        "ClientNotification.json",
        "JSONRPCError.json",
        "ServerNotification.json",
        "ServerRequest.json",
        *_REQUEST_SCHEMA_FILES.values(),
        *_RESPONSE_SCHEMA_FILES.values(),
    }
)


class CodexSchemaValidationError(CodexCompatibilityError):
    """A fixed, secret-free stable protocol validation failure."""


def _schema_error(kind: str) -> CodexSchemaValidationError:
    messages = {
        "artifact": "Codex stable schema artifact validation failed",
        "request": "Codex stable protocol request validation failed",
        "response": "Codex stable protocol response validation failed",
        "notification": "Codex stable protocol notification validation failed",
        "server_request": "Codex stable protocol server request validation failed",
    }
    codes = {
        "artifact": "codex_schema_artifact_invalid",
        "request": "codex_schema_request_invalid",
        "response": "codex_schema_response_invalid",
        "notification": "codex_schema_notification_invalid",
        "server_request": "codex_schema_server_request_invalid",
    }
    return CodexSchemaValidationError(messages[kind], code=codes[kind])


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _manifest_methods(value: object) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(method, str) or not method for method in value)
        or len(value) != len(set(value))
    ):
        raise _schema_error("artifact")
    return frozenset(value)


def _load_json(data: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _schema_error("artifact") from None
    if not isinstance(value, Mapping):
        raise _schema_error("artifact")
    return value


def _validator(schema: Mapping[str, Any]) -> Draft7Validator:
    try:
        Draft7Validator.check_schema(schema)
        return Draft7Validator(schema)
    except Exception:
        raise _schema_error("artifact") from None


def _schema_methods(schema: Mapping[str, Any]) -> frozenset[str]:
    methods: set[str] = set()
    alternatives = schema.get("oneOf")
    if not isinstance(alternatives, list):
        raise _schema_error("artifact")
    for alternative in alternatives:
        if not isinstance(alternative, Mapping):
            raise _schema_error("artifact")
        properties = alternative.get("properties")
        if not isinstance(properties, Mapping):
            raise _schema_error("artifact")
        method_schema = properties.get("method")
        if not isinstance(method_schema, Mapping):
            raise _schema_error("artifact")
        values = method_schema.get("enum")
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
            or not values[0]
        ):
            raise _schema_error("artifact")
        methods.add(values[0])
    return frozenset(methods)


@dataclass(frozen=True)
class _SchemaSet:
    request_params: Mapping[str, Draft7Validator]
    response_results: Mapping[str, Draft7Validator]
    client_notification: Draft7Validator
    server_request: Draft7Validator
    server_notification: Draft7Validator
    error_response: Draft7Validator
    generated_server_requests: frozenset[str]


@lru_cache(maxsize=1)
def _load_schema_set() -> _SchemaSet:
    """Load one immutable snapshot after proving its checked-in provenance."""

    tree = STABLE_PROTOCOL_MANIFEST.get("generatedSchemaTree")
    if not isinstance(tree, Mapping):
        raise _schema_error("artifact")
    expected_tree_hash = tree.get("treeSha256")
    expected_count = _manifest_int(tree.get("fileCount"))
    expected_bytes = _manifest_int(tree.get("byteCount"))
    if (
        not isinstance(expected_tree_hash, str)
        or len(expected_tree_hash) != 64
        or expected_count is None
        or expected_bytes is None
    ):
        raise _schema_error("artifact")

    if _SCHEMA_ROOT.is_symlink() or not _SCHEMA_ROOT.is_dir():
        raise _schema_error("artifact")

    records: list[bytes] = []
    selected: dict[str, bytes] = {}
    file_count = 0
    byte_count = 0
    try:
        paths = sorted(
            _SCHEMA_ROOT.rglob("*.json"),
            key=lambda path: path.relative_to(_SCHEMA_ROOT).as_posix(),
        )
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise _schema_error("artifact")
            relative = path.relative_to(_SCHEMA_ROOT).as_posix()
            data = path.read_bytes()
            file_count += 1
            byte_count += len(data)
            records.append(f"{relative}\0{_digest(data)}\n".encode("utf-8"))
            if relative in _REQUIRED_SCHEMA_FILES:
                selected[relative] = data
    except CodexSchemaValidationError:
        raise
    except OSError:
        raise _schema_error("artifact") from None

    if (
        file_count != expected_count
        or byte_count != expected_bytes
        or _digest(b"".join(records)) != expected_tree_hash
        or set(selected) != _REQUIRED_SCHEMA_FILES
    ):
        raise _schema_error("artifact")

    bundle = selected[_BUNDLE_RELATIVE]
    if _digest(bundle) != STABLE_SCHEMA_BUNDLE_SHA256:
        raise _schema_error("artifact")
    # The bundle is the manifest's provenance anchor even though the runtime
    # uses narrower per-method schemas from the same tree.
    _validator(_load_json(bundle))

    request_params = {
        method: _validator(_load_json(selected[relative]))
        for method, relative in _REQUEST_SCHEMA_FILES.items()
    }
    response_results = {
        method: _validator(_load_json(selected[relative]))
        for method, relative in _RESPONSE_SCHEMA_FILES.items()
    }
    client_notification_schema = _load_json(selected["ClientNotification.json"])
    server_request_schema = _load_json(selected["ServerRequest.json"])
    server_notification_schema = _load_json(selected["ServerNotification.json"])
    error_response_schema = _load_json(selected["JSONRPCError.json"])

    generated_requests = _schema_methods(server_request_schema)
    generated_notifications = _schema_methods(server_notification_schema)
    required_wire = STABLE_PROTOCOL_MANIFEST.get("requiredWire")
    if not isinstance(required_wire, Mapping):
        raise _schema_error("artifact")
    if _manifest_methods(required_wire.get("clientRequests")) != frozenset(
        _REQUEST_SCHEMA_FILES
    ):
        raise _schema_error("artifact")
    if _manifest_methods(required_wire.get("clientNotifications")) != {"initialized"}:
        raise _schema_error("artifact")
    if _manifest_methods(required_wire.get("serverRequests")) != _SERVER_REQUEST_ALLOWLIST:
        raise _schema_error("artifact")
    if _manifest_methods(required_wire.get("serverNotifications")) != _SERVER_NOTIFICATION_ALLOWLIST:
        raise _schema_error("artifact")
    if not _SERVER_REQUEST_ALLOWLIST.issubset(generated_requests):
        raise _schema_error("artifact")
    if not _SERVER_NOTIFICATION_ALLOWLIST.issubset(generated_notifications):
        raise _schema_error("artifact")

    return _SchemaSet(
        request_params=request_params,
        response_results=response_results,
        client_notification=_validator(client_notification_schema),
        server_request=_validator(server_request_schema),
        server_notification=_validator(server_notification_schema),
        error_response=_validator(error_response_schema),
        generated_server_requests=generated_requests,
    )


def _is_valid(validator: Draft7Validator, value: object) -> bool:
    try:
        return validator.is_valid(value)
    except Exception:
        return False


def _mapping(value: Mapping[str, Any] | None, *, kind: str) -> dict[str, Any]:
    try:
        return dict(value or {})
    except Exception:
        raise _schema_error(kind) from None


def _exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> bool:
    optional = optional or set()
    keys = set(value)
    return required.issubset(keys) and keys.issubset(required | optional)


def _bounded_string(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def _bounded_optional_string(value: object, maximum: int) -> bool:
    return value is None or _bounded_string(value, maximum)


def _valid_auth_url(value: object) -> bool:
    """Match the Host's exact browser-navigation allowlist before state."""

    if not isinstance(value, str) or not 0 < len(value) <= _MAX_AUTH_URL:
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    if (
        parsed.scheme == "https"
        and hostname == "auth.openai.com"
        and port in {None, 443}
        and parsed.path == "/oauth/authorize"
    ):
        return True
    return (
        parsed.scheme == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.path == "/oauth/callback"
    )


def _valid_request_policy(
    method: str,
    params: Mapping[str, Any],
    *,
    external_chatgpt_auth: bool,
    managed_token_refresh: bool,
) -> bool:
    if method == "initialize":
        if not _exact_keys(params, {"clientInfo", "capabilities"}):
            return False
        client_info = params.get("clientInfo")
        capabilities = params.get("capabilities")
        return (
            isinstance(client_info, Mapping)
            and _exact_keys(client_info, {"name", "title", "version"})
            and isinstance(capabilities, Mapping)
            and _exact_keys(capabilities, {"experimentalApi", "requestAttestation"})
            and capabilities.get("experimentalApi") is external_chatgpt_auth
            and capabilities.get("requestAttestation") is False
        )
    if method == "account/read":
        return (
            set(params) == {"refreshToken"}
            and type(params.get("refreshToken")) is bool
            and (params.get("refreshToken") is False or managed_token_refresh)
        )
    if method == "account/login/start":
        if external_chatgpt_auth:
            return (
                _exact_keys(
                    params,
                    {"type", "accessToken", "chatgptAccountId"},
                    {"chatgptPlanType"},
                )
                and params.get("type") == "chatgptAuthTokens"
                and _bounded_string(params.get("accessToken"), 32_768)
                and _bounded_string(params.get("chatgptAccountId"), _MAX_ITEM_OR_LOGIN_ID)
                and _bounded_optional_string(params.get("chatgptPlanType"), 128)
            )
        return (
            set(params) == {"type", "appBrand"}
            and params.get("type") == "chatgpt"
            and params.get("appBrand") == "chatgpt"
        )
    if method == "account/login/cancel":
        return (
            set(params) == {"loginId"}
            and _bounded_string(params.get("loginId"), _MAX_ITEM_OR_LOGIN_ID)
        )
    if method == "model/list":
        return (
            _exact_keys(params, {"limit", "includeHidden"})
            and type(params.get("limit")) is int
            and 1 <= params.get("limit", 0) <= 100
            and params.get("includeHidden") is False
        )
    if method == "thread/start":
        return (
            _exact_keys(
                params,
                {"ephemeral", "sandbox", "approvalPolicy", "developerInstructions"},
                {"cwd"},
            )
            and params.get("ephemeral") is False
            and params.get("sandbox") == "read-only"
            and params.get("approvalPolicy") == "never"
            and _bounded_optional_string(params.get("developerInstructions"), 8_000)
        )
    if method == "thread/resume":
        return (
            _exact_keys(
                params,
                {"threadId", "sandbox", "approvalPolicy", "developerInstructions"},
                {"cwd"},
            )
            and _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and params.get("sandbox") == "read-only"
            and params.get("approvalPolicy") == "never"
            and _bounded_optional_string(params.get("developerInstructions"), 8_000)
        )
    if method == "turn/start":
        return (
            _exact_keys(params, {"threadId", "input", "model", "effort", "serviceTier"})
            and _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and _bounded_string(params.get("model"), 128)
            and _bounded_string(params.get("effort"), 32)
            and (params.get("serviceTier") is None or _bounded_string(params.get("serviceTier"), 64))
        )
    if method == "turn/steer":
        return _exact_keys(
            params,
            {"threadId", "expectedTurnId", "input"},
            {"clientUserMessageId"},
        ) and all(
            (
                _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID),
                _bounded_string(params.get("expectedTurnId"), _MAX_THREAD_OR_TURN_ID),
                _bounded_optional_string(
                    params.get("clientUserMessageId"),
                    _MAX_ITEM_OR_LOGIN_ID,
                ),
            )
        )
    if method == "turn/interrupt":
        return (
            set(params) == {"threadId", "turnId"}
            and _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and _bounded_string(params.get("turnId"), _MAX_THREAD_OR_TURN_ID)
        )
    return False


def _valid_response_policy(
    method: str,
    result: Mapping[str, Any],
    request_params: Mapping[str, Any] | None = None,
) -> bool:
    if method == "account/login/start":
        if request_params is not None and request_params.get("type") == "chatgptAuthTokens":
            return set(result) == {"type"} and result.get("type") == "chatgptAuthTokens"
        return (
            result.get("type") == "chatgpt"
            and _bounded_string(result.get("loginId"), _MAX_ITEM_OR_LOGIN_ID)
            and _valid_auth_url(result.get("authUrl"))
        )
    if method in {"thread/start", "thread/resume"}:
        sandbox = result.get("sandbox")
        thread = result.get("thread")
        if not (
            result.get("approvalPolicy") == "never"
            and isinstance(sandbox, Mapping)
            and sandbox.get("type") == "readOnly"
            and isinstance(thread, Mapping)
            and _bounded_string(thread.get("id"), _MAX_THREAD_OR_TURN_ID)
            and thread.get("ephemeral") is False
            and thread.get("cliVersion") == EXPECTED_CLI_VERSION
            and isinstance(result.get("cwd"), str)
            and result.get("cwd") == thread.get("cwd")
        ):
            return False
        if request_params is None:
            return True
        requested_cwd = request_params.get("cwd")
        if requested_cwd is not None and result.get("cwd") != requested_cwd:
            return False
        if (
            method == "thread/resume"
            and thread.get("id") != request_params.get("threadId")
        ):
            return False
        return True
    if method == "turn/start":
        turn = result.get("turn")
        return (
            isinstance(turn, Mapping)
            and turn.get("status") == "inProgress"
            and _bounded_string(turn.get("id"), _MAX_THREAD_OR_TURN_ID)
        )
    if method == "turn/steer":
        return _bounded_string(result.get("turnId"), _MAX_THREAD_OR_TURN_ID)
    if method == "turn/interrupt":
        return not result
    return True


def _valid_server_request_policy(method: str, params: Mapping[str, Any]) -> bool:
    if method == "account/chatgptAuthTokens/refresh":
        return (
            set(params).issubset({"reason", "previousAccountId"})
            and isinstance(params.get("reason"), str)
            and _bounded_optional_string(params.get("previousAccountId"), _MAX_ITEM_OR_LOGIN_ID)
        )
    if method not in _SERVER_REQUEST_ALLOWLIST:
        return True
    if not _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID):
        return False
    if "turnId" in params and not _bounded_optional_string(
        params.get("turnId"),
        _MAX_THREAD_OR_TURN_ID,
    ):
        return False
    for key in ("itemId", "approvalId", "elicitationId"):
        if key in params and not _bounded_optional_string(
            params.get(key),
            _MAX_ITEM_OR_LOGIN_ID,
        ):
            return False
    return True


def _valid_server_notification_policy(method: str, params: Mapping[str, Any]) -> bool:
    if method in INERT_SERVER_NOTIFICATION_METHODS:
        return True
    if method == "account/login/completed":
        return _bounded_optional_string(params.get("loginId"), _MAX_ITEM_OR_LOGIN_ID)
    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn")
        return (
            _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and isinstance(turn, Mapping)
            and _bounded_string(turn.get("id"), _MAX_THREAD_OR_TURN_ID)
        )
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        return (
            _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and _bounded_string(params.get("turnId"), _MAX_THREAD_OR_TURN_ID)
            and isinstance(item, Mapping)
            and _bounded_string(item.get("id"), _MAX_ITEM_OR_LOGIN_ID)
        )
    if method == "item/agentMessage/delta":
        return (
            _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and _bounded_string(params.get("turnId"), _MAX_THREAD_OR_TURN_ID)
            and _bounded_string(params.get("itemId"), _MAX_ITEM_OR_LOGIN_ID)
        )
    if method == "serverRequest/resolved":
        request_id = params.get("requestId")
        return (
            _bounded_string(params.get("threadId"), _MAX_THREAD_OR_TURN_ID)
            and (
                (isinstance(request_id, int) and not isinstance(request_id, bool))
                or _bounded_string(request_id, _MAX_ITEM_OR_LOGIN_ID)
            )
        )
    return False


def _request_shape(message: Mapping[str, Any]) -> tuple[str, bool] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params")
    if (
        not isinstance(method, str)
        or not method
        or len(method) > 256
        or isinstance(request_id, bool)
        or not isinstance(request_id, (int, str))
        or (isinstance(request_id, str) and not _bounded_string(request_id, _MAX_ITEM_OR_LOGIN_ID))
        or not isinstance(params, Mapping)
        or "result" in message
        or "error" in message
    ):
        return None
    return method, method in _SERVER_REQUEST_ALLOWLIST


class StableProtocolValidator:
    """Validate the exact stable generated closure plus DSH policy."""

    def __init__(
        self,
        *,
        external_chatgpt_auth: bool = False,
        managed_token_refresh: bool = False,
    ) -> None:
        if type(external_chatgpt_auth) is not bool or type(managed_token_refresh) is not bool:
            raise ValueError("Codex protocol capability flags must be boolean")
        self.external_chatgpt_auth = external_chatgpt_auth
        self.managed_token_refresh = managed_token_refresh
        self._schemas = _load_schema_set()

    @property
    def schema_sha256(self) -> str:
        return STABLE_SCHEMA_BUNDLE_SHA256

    def validate_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload = _mapping(params, kind="request")
        validator = self._schemas.request_params.get(method)
        if (
            validator is None
            or not _is_valid(validator, payload)
            or not _valid_request_policy(
                method,
                payload,
                external_chatgpt_auth=self.external_chatgpt_auth,
                managed_token_refresh=self.managed_token_refresh,
            )
        ):
            raise _schema_error("request")
        return payload

    def validate_response(
        self,
        method: str,
        result: object,
        request_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        validator = self._schemas.response_results.get(method)
        if (
            validator is None
            or not isinstance(result, Mapping)
            or not _is_valid(validator, result)
            or not _valid_response_policy(method, result, request_params)
        ):
            raise _schema_error("response")
        return dict(result)

    def validate_error_response(self, message: Mapping[str, Any]) -> None:
        if "result" in message or not _is_valid(self._schemas.error_response, message):
            raise _schema_error("response")

    def validate_client_notification(
        self,
        method: str,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload = _mapping(params, kind="notification")
        if method != "initialized" or payload:
            raise _schema_error("notification")
        message = {"method": method}
        if not _is_valid(self._schemas.client_notification, message):
            raise _schema_error("notification")
        return message

    def validate_server_request(self, message: Mapping[str, Any]) -> bool:
        """Return whether a typed-denied request is in DSH's known surface.

        Generated requests are always checked against the complete stable
        envelope. A future method absent from the pinned schema is still
        denied when its correlation shape is safe enough to answer; accepting
        it would be unsafe, while killing the process before sending a denial
        could leave an approval/tool request unresolved. Malformed known
        methods are isolated instead.
        """

        shape = _request_shape(message)
        if shape is None:
            raise _schema_error("server_request")
        method, business_known = shape
        if method == "account/chatgptAuthTokens/refresh" and not self.external_chatgpt_auth:
            business_known = False
        if method in self._schemas.generated_server_requests:
            if (
                not _is_valid(self._schemas.server_request, message)
                or not _valid_server_request_policy(method, message["params"])
            ):
                raise _schema_error("server_request")
        # Unknown future request methods take the typed-denial fallback
        # described above. Validation never interprets their payload fields;
        # the caller may retain only separately allow-listed routing ids.
        return business_known

    def validate_server_notification(self, message: Mapping[str, Any]) -> str:
        method = message.get("method")
        if (
            not isinstance(method, str)
            or not method
            or len(method) > 256
            or "id" in message
            or "result" in message
            or "error" in message
            or not isinstance(message.get("params"), Mapping)
            or not _is_valid(self._schemas.server_notification, message)
            or method not in _SERVER_NOTIFICATION_ALLOWLIST
            or not _valid_server_notification_policy(method, message["params"])
        ):
            raise _schema_error("notification")
        return method


def verify_checked_in_stable_schema() -> str:
    """Verify and return the pinned bundle digest without starting Codex."""

    StableProtocolValidator()
    return STABLE_SCHEMA_BUNDLE_SHA256
