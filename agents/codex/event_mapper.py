"""Map Codex app-server v2 notifications to the public agent event set."""

from __future__ import annotations

from typing import Any, Mapping

from .types import AgentEvent, AgentEventType


def _params(message: Mapping[str, Any]) -> Mapping[str, Any]:
    value = message.get("params")
    return value if isinstance(value, Mapping) else {}


def _turn(params: Mapping[str, Any]) -> Mapping[str, Any]:
    value = params.get("turn")
    return value if isinstance(value, Mapping) else {}


def _item(params: Mapping[str, Any]) -> Mapping[str, Any]:
    value = params.get("item")
    return value if isinstance(value, Mapping) else {}


def _safe_item_data(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose activity metadata without echoing command/output/user content."""

    safe: dict[str, Any] = {}
    for key in ("id", "type", "status", "phase", "tool", "server", "name"):
        value = item.get(key)
        if isinstance(value, (str, int, float, bool)):
            safe[key] = value
    # The stable AgentMessageThreadItem schema exposes only its bounded final
    # text snapshot. Never copy command/file/tool output into the public log.
    if item.get("type") == "agentMessage":
        text = item.get("text")
        if isinstance(text, str) and len(text) <= 16_000:
            safe["text"] = text
    return safe


def map_notification(
    message: Mapping[str, Any],
    *,
    session_id: str,
    correlation_id: str,
) -> AgentEvent | None:
    """Map a single wire message; unknown notifications are ignored safely."""

    method = message.get("method")
    if not isinstance(method, str):
        return None
    params = _params(message)
    thread_id = params.get("threadId")
    thread = str(thread_id) if thread_id is not None else None
    turn = _turn(params)
    turn_id_value = params.get("turnId") or turn.get("id")
    turn_id = str(turn_id_value) if turn_id_value is not None else None
    common = {
        "session_id": session_id,
        "thread_id": thread,
        "turn_id": turn_id,
        "correlation_id": correlation_id,
    }

    if method == "turn/started":
        return AgentEvent(
            AgentEventType.STARTED,
            status="started",
            data={"source": method},
            **common,
        )
    if method == "item/agentMessage/delta":
        delta = params.get("delta")
        if not isinstance(delta, str):
            return None
        item_id = params.get("itemId")
        return AgentEvent(
            AgentEventType.TEXT_DELTA,
            text=delta,
            item_id=str(item_id) if isinstance(item_id, str) else None,
            data={"source": method},
            **common,
        )
    if method in {"item/started", "item/completed"}:
        item = _item(params)
        status = "started" if method == "item/started" else "completed"
        item_id = item.get("id")
        safe_item_id = item_id if isinstance(item_id, str) and 0 < len(item_id) <= 256 else None
        return AgentEvent(
            AgentEventType.TOOL_ACTIVITY,
            status=status,
            activity=str(item.get("type") or "item"),
            item_id=safe_item_id,
            data={"source": method, "item": _safe_item_data(item)},
            **common,
        )
    if method == "turn/completed":
        status = str(turn.get("status") or "failed")
        if status == "interrupted":
            return AgentEvent(
                AgentEventType.INTERRUPTED,
                status=status,
                data={"source": method},
                **common,
            )
        if status == "completed":
            return AgentEvent(
                AgentEventType.FINISHED,
                status=status,
                data={"source": method},
                **common,
            )
        error = turn.get("error")
        return AgentEvent(
            AgentEventType.ERROR,
            status=status,
            error=_safe_error(error),
            data={"source": method},
            **common,
        )
    if method == "server/request/denied":
        return AgentEvent(
            AgentEventType.TOOL_ACTIVITY,
            status="denied",
            activity="server_request",
            error="server request denied by DSH fail-closed policy",
            data={
                "source": method,
                "request_method": str(params.get("method") or "unknown"),
                "request_id": str(params.get("requestId") or ""),
            },
            **common,
        )
    return None


def _safe_error(value: Any) -> str:
    """Map provider failures to a fixed public category.

    App-server messages can contain command output, paths, prompt fragments,
    or account diagnostics.  Even a bounded substring is not safe to forward.
    """

    candidate: str | None = None
    if isinstance(value, Mapping):
        for key in ("code", "type", "status"):
            raw = value.get(key)
            if isinstance(raw, str):
                candidate = raw.lower()
                break
    if candidate and "timeout" in candidate:
        return "codex_turn_timeout"
    if candidate and "cancel" in candidate:
        return "codex_turn_canceled"
    return "codex_turn_failed"
