"""Public, secret-free types for the direct Codex agent boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class AgentEventType(str, Enum):
    """Events exposed to the browser/bridge consumer."""

    STARTED = "AgentStarted"
    TEXT_DELTA = "AgentTextDelta"
    TOOL_ACTIVITY = "AgentToolActivity"
    FINISHED = "AgentFinished"
    INTERRUPTED = "AgentInterrupted"
    ERROR = "AgentError"


@dataclass(frozen=True)
class AgentEvent:
    """Normalized event independent of Codex's wire schema."""

    type: AgentEventType
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    correlation_id: str | None = None
    text: str | None = None
    status: str | None = None
    activity: str | None = None
    error: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    item_id: str | None = None
    phase: str | None = None
    speakable: bool | None = None
    final_text: str | None = None

    @property
    def terminal(self) -> bool:
        if self.data.get("terminal") is False:
            return False
        return self.type in {
            AgentEventType.FINISHED,
            AgentEventType.INTERRUPTED,
            AgentEventType.ERROR,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the safe public event envelope for REST/WS consumers."""

        payload: dict[str, Any] = {"type": self.type.value}
        for key, value in (
            ("session_id", self.session_id),
            ("thread_id", self.thread_id),
            ("turn_id", self.turn_id),
            ("correlation_id", self.correlation_id),
            ("text", self.text),
            ("status", self.status),
            ("activity", self.activity),
            ("error", self.error),
            ("item_id", self.item_id),
            ("phase", self.phase),
            ("speakable", self.speakable),
        ):
            if value is not None:
                payload[key] = value
        # The browser adapter uses execution_id/final_text aliases while the
        # Python API keeps correlation_id/text as the canonical names.  Both
        # are safe scalar aliases, never JSON-RPC fields.
        if self.correlation_id is not None:
            payload["execution_id"] = self.correlation_id
        if self.final_text is not None:
            payload["final_text"] = self.final_text
        # Error status is not generally a wire error code. Only the closed
        # safe vocabulary is copied into the explicit field consumed by the
        # Host parser; arbitrary backend status/message text never crosses.
        if self.type is AgentEventType.ERROR and self.status in {
            "not_authenticated",
            "bridge_unavailable",
            "bridge_protocol",
            "turn_in_progress",
            "turn_failed",
            "interrupt_timeout",
            "invalid_request",
            "approval_unavailable",
            "host_restart",
            "interrupt_isolated",
            "isolation_failed",
            "mapping_commit_failed",
            "security_isolation_unavailable",
        }:
            payload["error_code"] = self.status
        elif self.type is AgentEventType.ERROR and self.status == "process_exit":
            # Process exit is a distinct closed terminal discriminant, not a
            # generic status string that a Host parser may infer from.
            payload["error_code"] = "process_exit"
        if self.data:
            payload["data"] = dict(self.data)
        return payload


class LoginStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class LoginState:
    login_id: str
    status: LoginStatus
    auth_url: str | None = None
    success: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "login_id": self.login_id,
            "status": self.status.value,
        }
        if self.auth_url is not None:
            payload["auth_url"] = self.auth_url
        if self.success is not None:
            payload["success"] = self.success
        if self.error is not None:
            payload["error"] = self.error
        return payload


class CodexError(RuntimeError):
    """Base error surfaced by the direct Codex boundary."""

    def __init__(self, message: str, *, code: int | str | None = None) -> None:
        super().__init__(message)
        self.code = code


class CodexProcessError(CodexError):
    """The app-server process exited, failed to start, or lost stdio."""


class CodexTimeoutError(CodexError):
    """A request or turn exceeded its configured deadline."""


class CodexCompatibilityError(CodexError):
    """The app-server failed the pinned stable-protocol compatibility gate."""
