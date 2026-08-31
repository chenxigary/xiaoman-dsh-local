"""Direct, product-facing Codex agent service.

The service deliberately stops at the official app-server boundary.  It owns
session/thread correlation and event normalization, but it does not implement
an LLM provider or DSH's native agent tool loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Literal, Mapping

from .app_server_client import (
    INTERNAL_APP_SERVER_EXITED,
    INTERNAL_APP_SERVER_ISOLATION_FAILED,
    AppServerClient,
    CodexAmbiguousRequestError,
)
from .event_mapper import map_notification
from .persona import developer_instructions_for
from .thread_manager import ThreadManager
from .types import (
    AgentEvent,
    AgentEventType,
    CodexError,
    CodexProcessError,
    CodexTimeoutError,
)


class CodexBusyError(CodexError):
    """A session/thread already has an in-flight turn."""

    def __init__(self, message: str = "a Codex turn is already active for this session") -> None:
        super().__init__(message, code="turn_in_progress")


class _TurnAlreadyTerminal(Exception):
    """Internal control flow: an early isolation terminal won the race."""


class _NotificationBufferOverflow(CodexError):
    """A pre-response notification buffer crossed its protocol budget."""

    def __init__(self, state: "_TurnState") -> None:
        super().__init__("Codex notification buffer exceeded its limit", code="invalid_response")
        self.state = state


class _TurnQueueOverflow(CodexError):
    """A per-turn consumer queue lost wire order under backpressure."""

    def __init__(self, state: "_TurnState") -> None:
        super().__init__("Codex turn event queue exceeded its limit", code="invalid_response")
        self.state = state


class _TurnTerminalConflict(CodexError):
    """Two generated-valid terminals contradicted for one exact turn pair."""

    def __init__(self) -> None:
        super().__init__("conflicting Codex turn terminal", code="protocol_conflict")


class _FinishedTurnTerminalConflict(Exception):
    """A late terminal contradicted an exact cached terminal authority."""

    def __init__(
        self,
        *,
        pair: tuple[str, str],
        execution_key: tuple[str, str],
        terminal: AgentEvent,
    ) -> None:
        super().__init__("conflicting cached Codex turn terminal")
        self.pair = pair
        self.execution_key = execution_key
        self.terminal = terminal


MAX_VISIBLE_CHARS = 16_000
MAX_UNKNOWN_BUFFER_CHARS = 65_536
ExecutionReleaseStatus = Literal["released", "poisoned", "pending", "unknown"]


@dataclass
class _TurnState:
    session_id: str
    thread_id: str | None
    correlation_id: str
    queue_size: int
    cwd: str | None = None
    turn_id: str | None = None
    started_seen: bool = False
    terminal: AgentEvent | None = None
    interrupt_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    # Set under the provider state lock immediately before stream_turn may
    # call ThreadManager/AppServer. False therefore proves this reservation
    # has never spawned or dispatched process-facing work.
    dispatch_may_have_started: bool = False
    mapping_committed: bool = False
    release_task: asyncio.Task[None] | None = None
    # This is an explicit authority bit, never a derivation from a terminal
    # status. It becomes true only after an authoritative rejection/terminal
    # or verified process quiescence plus successful mapping cleanup.
    release_authoritative: bool = False
    # Poison is sticky for the execution: one failed group verification or
    # mapping cleanup cannot be overwritten by a racing success path.
    release_poisoned: bool = False
    # In-memory ownership may be retired only after the current release fence
    # is authoritative. This is distinct from the sticky ledger success bit:
    # a later verified reconciliation may safely remove a poisoned state while
    # the execution ledger continues to record the earlier failure as false.
    retirement_authorized: bool = False
    release_fence_active: bool = False
    terminal_wire_digest: str | None = None

    def __post_init__(self) -> None:
        self.queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self.queue_size)
        self.terminal_event = asyncio.Event()
        self.turn_ready_event = asyncio.Event()
        # Host isolation acknowledgement is not complete until every local
        # mapping for this execution has crossed the same cleanup fence as a
        # normal async-generator `finally`.
        self.released_event = asyncio.Event()
        # A turn/start response can bind the authoritative pair while the
        # app-server notification dispatcher is already holding a later
        # terminal.  Serializing replay and live delivery per state preserves
        # wire order across that response boundary.
        self.notification_lock = asyncio.Lock()
        self.text_parts: list[str] = []
        self.visible_chars = 0
        self.unknown_buffer_chars = 0
        self.item_phases: dict[str, str] = {}
        self.unknown_item_text: dict[str, list[str]] = {}
        self.item_text: dict[str, list[str]] = {}
        # Notifications may beat the turn/start response. They are buffered
        # by exact (thread, turn) pair and replayed only after the response's
        # turn id becomes authoritative; a late old pair is then dropped.
        self.buffered_notifications: list[tuple[Any, dict[str, Any]]] = []
        self.buffered_notification_counts: dict[tuple[str, str], int] = {}

    @property
    def key(self) -> tuple[str, str] | None:
        if self.turn_id is None:
            return None
        if self.thread_id is None:
            return None
        return (self.thread_id, self.turn_id)

    def push(self, event: AgentEvent) -> None:
        """Queue an event without ever blocking the app-server reader."""

        if self.terminal is not None:
            return
        if event.type is AgentEventType.STARTED:
            if self.started_seen:
                return
            self.started_seen = True
        elif event.type is AgentEventType.TEXT_DELTA and event.text and event.speakable:
            next_visible_chars = self.visible_chars + len(event.text)
            if next_visible_chars > MAX_VISIBLE_CHARS:
                raise CodexError("Codex visible text exceeded its limit", code="invalid_response")
            self.visible_chars = next_visible_chars
            self.text_parts.append(event.text)
        if event.terminal:
            # Keep the complete assistant text on the terminal event so a REST
            # caller that only observes the final event still has the answer.
            if event.type is AgentEventType.FINISHED and self.text_parts:
                final_text = "".join(self.text_parts)
                event = replace(event, text=final_text, final_text=final_text)
            self.terminal = event
            self.terminal_event.set()
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            if not event.terminal:
                # Dropping a middle delta silently can make the terminal
                # snapshot non-contiguous.  The dispatcher converts this
                # signal into process isolation and a safe terminal instead.
                raise _TurnQueueOverflow(self)
            # Terminal visibility is still mandatory. Evict one queued
            # nonterminal event only for the terminal itself; no terminal is
            # ever discarded in favor of a later event.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(event)

    def confirm_release_authority(self) -> None:
        if self.release_fence_active:
            return
        self.retirement_authorized = True
        if not self.release_poisoned:
            self.release_authoritative = True

    def begin_release_fence(self) -> None:
        self.release_fence_active = True
        self.retirement_authorized = False
        self.release_authoritative = False

    def complete_release_fence(self) -> None:
        self.release_fence_active = False
        self.retirement_authorized = True
        if not self.release_poisoned:
            self.release_authoritative = True

    def poison_release_authority(self) -> None:
        self.release_fence_active = True
        self.retirement_authorized = False
        self.release_poisoned = True
        self.release_authoritative = False


class CodexAgentService:
    """Manage concurrent direct Codex turns over one app-server process."""

    # The private M4 distribution is deliberately local-only. Tests that cover
    # the retained protocol implementation opt in through an explicit subclass;
    # production code cannot start a subscription-backed turn.
    _TURN_EXECUTION_ENABLED = False

    def __init__(
        self,
        client: AppServerClient,
        thread_manager: ThreadManager,
        *,
        turn_timeout: float = 1800.0,
        event_queue_size: int = 256,
        finished_cache_size: int = 256,
    ) -> None:
        if (
            isinstance(turn_timeout, bool)
            or not isinstance(turn_timeout, (int, float))
            or not math.isfinite(turn_timeout)
            or not 0 < turn_timeout <= 7200.0
        ):
            raise ValueError("turn_timeout is outside the audited bound")
        if (
            isinstance(event_queue_size, bool)
            or not isinstance(event_queue_size, int)
            or not 1 <= event_queue_size <= 4096
        ):
            raise ValueError("event_queue_size is outside the audited bound")
        if (
            isinstance(finished_cache_size, bool)
            or not isinstance(finished_cache_size, int)
            or not 1 <= finished_cache_size <= 4096
        ):
            raise ValueError("finished_cache_size is outside the audited bound")
        self.client = client
        self.thread_manager = thread_manager
        self.turn_timeout = turn_timeout
        self.event_queue_size = event_queue_size
        self.finished_cache_size = finished_cache_size
        self._notification_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._pending_by_thread: dict[str, _TurnState] = {}
        self._pending_by_execution: dict[tuple[str, str], _TurnState] = {}
        self._active: dict[tuple[str, str], _TurnState] = {}
        self._finished: OrderedDict[tuple[str, str], tuple[str, AgentEvent]] = OrderedDict()
        self._finished_terminal_digests: OrderedDict[
            tuple[str, str],
            tuple[tuple[str, str], str],
        ] = OrderedDict()
        # Early cancellation can finish before a thread/turn pair exists. Keep
        # that exact (session, correlation) terminal so a delayed stream/start
        # cannot create a ghost rollout.
        # The boolean is part of the release ledger, not an inferred property
        # of the terminal event: an `isolation_failed` outcome must never be
        # accepted as the idempotent success ACK used by Host HTTP fallback.
        self._finished_by_execution: OrderedDict[tuple[str, str], tuple[AgentEvent, bool]] = OrderedDict()

    async def _ensure_dispatcher(self) -> None:
        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            return
        if self._notification_queue is not None:
            self.client.unsubscribe(self._notification_queue)
        self._notification_queue = self.client.subscribe(maxsize=self.event_queue_size)
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop(self._notification_queue))

    async def _dispatch_loop(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                message = await queue.get()
                method = message.get("method")
                if method in {
                    INTERNAL_APP_SERVER_EXITED,
                    INTERNAL_APP_SERVER_ISOLATION_FAILED,
                }:
                    event_targets = await self._all_states()
                    exit_params = message.get("params")
                    exit_params = exit_params if isinstance(exit_params, Mapping) else {}
                    exit_reason = exit_params.get("reason")
                    isolation_failed = (
                        method == INTERNAL_APP_SERVER_ISOLATION_FAILED
                        or exit_reason == "isolation_failed"
                    )
                    for state in event_targets:
                        # The internal app-server terminal is terminal-like and
                        # must serialize with authoritative turn/start binding
                        # and buffered replay. Otherwise it can overtake a
                        # pre-response final delta and truncate the answer.
                        async with state.notification_lock:
                            durable_completed = (
                                state.mapping_committed
                                and state.terminal is not None
                                and state.terminal.type is AgentEventType.FINISHED
                                and state.terminal.status == "completed"
                            )
                            if not isolation_failed and durable_completed:
                                # The completed turn is already durable. A
                                # later verified AppServer exit ends only the
                                # process generation; deleting this mapping
                                # would make the next request silently fork
                                # instead of resuming the committed history.
                                state.complete_release_fence()
                                continue
                            cleanup_failed = isolation_failed
                            if not isolation_failed:
                                try:
                                    await self.thread_manager.invalidate(state.session_id, state.thread_id)
                                except Exception:
                                    cleanup_failed = True
                            if cleanup_failed:
                                state.poison_release_authority()
                                state_exit_status = "isolation_failed"
                                state_exit_error = (
                                    "codex app-server process isolation failed"
                                    if isolation_failed
                                    else "codex app-server state cleanup failed"
                                )
                            else:
                                state.complete_release_fence()
                                state_exit_status = "process_exit"
                                state_exit_error = "codex app-server process exited"
                            state.push(
                                AgentEvent(
                                    AgentEventType.ERROR,
                                    session_id=state.session_id,
                                    thread_id=state.thread_id,
                                    turn_id=state.turn_id,
                                    correlation_id=state.correlation_id,
                                    status=state_exit_status,
                                    error=state_exit_error,
                                    data={
                                        "source": method,
                                        "isolated": not isolation_failed,
                                        "mapping_clean": not cleanup_failed,
                                    },
                                )
                            )
                    continue

                params = message.get("params")
                params = params if isinstance(params, Mapping) else {}
                try:
                    state_targets = await self._states_for_notification(method, params)
                except _FinishedTurnTerminalConflict as exc:
                    await self._fail_closed_finished_terminal(exc)
                    continue
                except _NotificationBufferOverflow as exc:
                    # The lookup runs under the global state lock, so route
                    # overflow only after it returns; the fail-closed path
                    # may acquire that lock again while isolating all turns.
                    await self._fail_closed_notification(exc.state, exc)
                    continue
                for state in state_targets:
                    try:
                        async with state.notification_lock:
                            await self._bind_notification_ids(state, params)
                            await self._dispatch_notification(state, method, message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - protocol boundary
                        await self._fail_closed_notification(state, exc)
        except asyncio.CancelledError:
            raise

    async def _fail_closed_notification(self, state: _TurnState, cause: Exception) -> None:
        """Isolate malformed/inconsistent event streams before a safe terminal."""

        terminal_conflict = isinstance(cause, _TurnTerminalConflict)
        if state.terminal is not None and not terminal_conflict:
            return
        if terminal_conflict:
            # The first terminal may already have been delivered. It cannot
            # be retracted, so poison its release ledger before isolation;
            # Host must never treat that first-wins payload as safe release.
            state.poison_release_authority()
        try:
            await self.isolate("invalid Codex notification")
        except Exception:
            # The process client is already considered uncertain.  The safe
            # verified app-server terminal below is still the only public outcome.
            pass
        if state.terminal is None:
            state.push(
                _error_event(
                    state,
                    CodexError("invalid Codex notification", code="invalid_response"),
                    status="protocol_error",
                )
            )

    async def _fail_closed_finished_terminal(
        self,
        conflict: _FinishedTurnTerminalConflict,
    ) -> None:
        """Poison a cached release before isolating a late contradiction."""

        async with self._state_lock:
            current = self._finished.get(conflict.pair)
            digest_entry = self._finished_terminal_digests.get(conflict.pair)
            if (
                current is None
                or current[1] is not conflict.terminal
                or (
                    digest_entry is not None
                    and digest_entry[0] != conflict.execution_key
                )
            ):
                return
            # A release frame may already have been lost or delivered. Keep a
            # sticky false exact ledger so every later Host reconciliation is
            # denied rather than silently accepting the cached first terminal.
            self._finished_by_execution[conflict.execution_key] = (
                conflict.terminal,
                False,
            )
            self._finished_by_execution.move_to_end(conflict.execution_key)
            while len(self._finished_by_execution) > self.finished_cache_size:
                self._finished_by_execution.popitem(last=False)
        try:
            await self.isolate("conflicting cached Codex terminal")
        except Exception:
            return
        try:
            await self.thread_manager.invalidate(
                conflict.execution_key[0],
                conflict.pair[0],
            )
        except Exception:
            # The false ledger is already authoritative for quarantine. Never
            # upgrade it when durable cleanup cannot be confirmed.
            return

    async def _dispatch_notification(
        self,
        state: _TurnState,
        method: Any,
        message: Mapping[str, Any],
    ) -> None:
        """Map one notification while the owning turn's delivery lock is held."""

        if method == "turn/completed":
            terminal_digest = _turn_terminal_digest(message)
            if state.terminal_wire_digest is not None:
                if state.terminal_wire_digest == terminal_digest:
                    # A separately decoded semantic duplicate carries no new
                    # authority. Top-level emittedAtMs is intentionally not
                    # part of the params digest.
                    return
                state.poison_release_authority()
                raise _TurnTerminalConflict()
            if state.terminal is not None:
                state.poison_release_authority()
                raise _TurnTerminalConflict()
            state.terminal_wire_digest = terminal_digest

        # `item/completed` is the only authoritative snapshot repair point.
        # Validate the closed agentMessage shape before the mapper's safe
        # projection can intentionally omit oversized text; otherwise a
        # malformed/oversized snapshot could fall back to untrusted buffered
        # deltas and be published as a plausible final answer.
        if method == "item/completed":
            params = message.get("params")
            item = params.get("item") if isinstance(params, Mapping) else None
            if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                item_id = item.get("id")
                text = item.get("text")
                phase = item.get("phase")
                if (
                    not isinstance(item_id, str)
                    or not item_id
                    or len(item_id) > 256
                    or not isinstance(text, str)
                    or len(text) > 16_000
                    or (phase is not None and phase not in {"commentary", "final_answer"})
                ):
                    raise CodexError("invalid agent message snapshot", code="invalid_response")

        event = map_notification(
            message,
            session_id=state.session_id,
            correlation_id=state.correlation_id,
        )
        if event is None:
            return
        # Server notifications are authoritative for terminal state and
        # include exact IDs; prefer state-owned IDs if an older server omits
        # one of them.
        event = replace(
            event,
            thread_id=state.thread_id,
            turn_id=state.turn_id or event.turn_id,
        )
        event = self._enrich_event(state, event)
        # Unknown-phase agent deltas are retained only in the bounded internal
        # buffer.  They are not public events: a Host bridge may only accept
        # commentary/final_answer and must never accidentally speak an
        # unclassified fragment.  `_reconcile_item` emits a single final
        # snapshot delta after item/completed confirms the phase.
        if event.type is AgentEventType.TEXT_DELTA and event.phase == "unknown":
            return
        if (
            method == "turn/completed"
            and event.type is AgentEventType.FINISHED
            and event.status == "completed"
            and not state.mapping_committed
            and state.thread_id is not None
        ):
            # A successful authoritative terminal is the first durable point.
            # Start-only, failed, and interrupted turns leave no persisted
            # mapping.
            try:
                await self.thread_manager.commit_thread(
                    state.session_id,
                    state.thread_id,
                    cwd=state.cwd,
                )
                state.mapping_committed = True
            except Exception:
                # A successful answer without a durable thread mapping would
                # silently fork the next turn onto a new thread. Remove any
                # partial/provisional entry and publish only a fixed terminal
                # error; never expose the completed event as if continuity
                # were durable.
                cleanup_failed = False
                try:
                    await self.thread_manager.invalidate(state.session_id, state.thread_id)
                except Exception:
                    cleanup_failed = True
                if cleanup_failed:
                    state.poison_release_authority()
                    state.push(
                        _error_event(
                            state,
                            CodexError("Codex state cleanup failed", code="isolation_failed"),
                            status="isolation_failed",
                        )
                    )
                    return
                state.confirm_release_authority()
                state.push(
                    _error_event(
                        state,
                        CodexError("Codex thread mapping could not be persisted", code="mapping_commit_failed"),
                        status="mapping_commit_failed",
                    )
                )
                return
        if event.terminal:
            # A generated-schema-valid turn terminal is authoritative even
            # though the shared App Server remains alive for later turns.
            state.confirm_release_authority()
        state.push(event)
        for reconciled in self._reconcile_item(state, event):
            state.push(reconciled)

    async def _all_states(self) -> list[_TurnState]:
        async with self._state_lock:
            unique: dict[int, _TurnState] = {}
            for state in (
                *self._pending_by_thread.values(),
                *self._pending_by_execution.values(),
                *self._active.values(),
            ):
                unique[id(state)] = state
            return list(unique.values())

    async def _bind_notification_ids(self, state: _TurnState, params: Mapping[str, Any]) -> None:
        """Bind exact server IDs, including the early-interrupt race window."""

        thread_id = _string_id(params.get("threadId"))
        turn_id = _string_id(params.get("turnId"))
        if turn_id is None:
            turn = params.get("turn")
            if isinstance(turn, Mapping):
                turn_id = _string_id(turn.get("id"))
        async with self._state_lock:
            if state.thread_id is None and thread_id is not None:
                state.thread_id = thread_id
            if state.thread_id is not None and thread_id is not None and state.thread_id != thread_id:
                return
            if state.turn_id is None and turn_id is not None:
                state.turn_id = turn_id
            if state.turn_id is not None:
                self._pending_by_thread.pop(state.thread_id or "", None)
                self._pending_by_execution.pop((state.session_id, state.correlation_id), None)
                self._active[(state.thread_id or "", state.turn_id)] = state
                state.turn_ready_event.set()

    async def _states_for_notification(
        self,
        method: Any,
        params: Mapping[str, Any],
    ) -> list[_TurnState]:
        thread_id = _string_id(params.get("threadId"))
        turn_id = _string_id(params.get("turnId"))
        if turn_id is None:
            turn = params.get("turn")
            if isinstance(turn, Mapping):
                turn_id = _string_id(turn.get("id"))
        async with self._state_lock:
            if thread_id is not None and turn_id is not None:
                state = self._active.get((thread_id, turn_id))
                if state is not None:
                    return [state]
                # A completed old pair is stale, even when a provisional
                # same-thread reservation is waiting for a newer turn/start
                # response. Never let it consume that reservation's buffer.
                pair = (thread_id, turn_id)
                if pair in self._finished:
                    if method == "turn/completed":
                        finished_owner, terminal = self._finished[pair]
                        recorded = self._finished_terminal_digests.get(pair)
                        incoming_digest = _turn_terminal_digest({"params": params})
                        if recorded is None or recorded[1] != incoming_digest:
                            execution_id = terminal.correlation_id
                            if isinstance(execution_id, str) and execution_id:
                                raise _FinishedTurnTerminalConflict(
                                    pair=pair,
                                    execution_key=(finished_owner, execution_id),
                                    terminal=terminal,
                                )
                    return []
                pending = self._pending_by_thread.get(thread_id)
                if pending is not None and pending.turn_id is None:
                    pair = (thread_id, turn_id)
                    pair_count = pending.buffered_notification_counts.get(pair, 0)
                    if len(pending.buffered_notifications) >= 256 or pair_count >= 64:
                        raise _NotificationBufferOverflow(pending)
                    pending.buffered_notifications.append((method, dict(params)))
                    pending.buffered_notification_counts[pair] = pair_count + 1
                    # Do not let a notification choose the turn id. The
                    # turn/start response owns that identity.
                    return []
                # An exact pair that is not owned by this bridge is stale or
                # foreign. Never guess among concurrent reservations.
                return []
            if thread_id is not None:
                active = [state for key, state in self._active.items() if key[0] == thread_id]
                return active
            # Normal notifications without a thread id are not safely
            # attributable when multiple sessions reserve turns.  Drop them
            # closed; only an ID-bearing denial may fan out below.
            # A denied request from an older server may not carry IDs.  It is
            # safer to surface it to every in-flight turn than to hide a
            # fail-closed denial; normal turn events always have IDs.
            if method == "server/request/denied":
                unique: dict[int, _TurnState] = {}
                for state in (
                    *self._pending_by_thread.values(),
                    *self._pending_by_execution.values(),
                    *self._active.values(),
                ):
                    unique[id(state)] = state
                return list(unique.values())
            return []

    @staticmethod
    def _enrich_event(state: _TurnState, event: AgentEvent) -> AgentEvent:
        """Carry agent-message phase from item metadata to every delta."""

        if event.type is AgentEventType.TOOL_ACTIVITY:
            item = event.data.get("item")
            if isinstance(item, Mapping):
                item_id = item.get("id")
                item_type = item.get("type")
                phase = item.get("phase")
                if isinstance(item_id, str):
                    if phase in {"commentary", "final_answer"}:
                        state.item_phases[item_id] = phase
            return event
        if event.type is not AgentEventType.TEXT_DELTA:
            return event
        item_id = event.item_id or "unknown"
        phase = state.item_phases.get(item_id, "unknown")
        if phase == "unknown" and event.text:
            buffered = state.unknown_item_text.setdefault(item_id, [])
            next_unknown_chars = state.unknown_buffer_chars + len(event.text)
            if next_unknown_chars > MAX_UNKNOWN_BUFFER_CHARS:
                raise CodexError("Codex unknown item buffer exceeded its limit", code="invalid_response")
            buffered.append(event.text)
            state.unknown_buffer_chars = next_unknown_chars
        elif phase == "final_answer" and event.text:
            received = state.item_text.setdefault(item_id, [])
            received.append(event.text)
        return replace(event, phase=phase, speakable=phase == "final_answer")

    @staticmethod
    def _reconcile_item(state: _TurnState, event: AgentEvent) -> list[AgentEvent]:
        """Flush an unknown item only after completed metadata confirms phase."""

        if event.type is not AgentEventType.TOOL_ACTIVITY or event.status != "completed":
            return []
        item = event.data.get("item")
        if not isinstance(item, Mapping):
            return []
        item_id = item.get("id")
        item_type = item.get("type")
        phase = item.get("phase")
        if not isinstance(item_id, str):
            return []
        # Stable App Server schemas permit a null phase on an
        # `item/completed` snapshot. Reuse the phase learned from the matching
        # `item/started` record; never infer final-answer from text alone.
        if phase not in {"commentary", "final_answer"}:
            phase = state.item_phases.get(item_id)
        if phase not in {"commentary", "final_answer"}:
            return []
        visible = "".join(state.item_text.pop(item_id, []))
        pending = "".join(state.unknown_item_text.pop(item_id, []))
        state.unknown_buffer_chars = max(0, state.unknown_buffer_chars - len(pending))
        # Only the closed AgentMessageThreadItem schema carries an assistant
        # answer.  Command/tool items may contain arbitrary output in the
        # upstream payload; never turn their text or a forged final phase into
        # a speakable durable delta.
        if item_type != "agentMessage":
            return []
        snapshot = item.get("finalText")
        if snapshot is None:
            snapshot = item.get("text")
        if phase != "final_answer":
            return []
        if item_type == "agentMessage":
            # The raw notification was validated before mapping. Keep this
            # assertion at the reconciliation seam too so future mappers
            # cannot reintroduce a buffered-delta fallback.
            if not isinstance(snapshot, str) or len(snapshot) > 16_000:
                raise CodexError("invalid agent message snapshot", code="invalid_response")
        if isinstance(snapshot, str) and len(snapshot) <= 16_000:
            # AgentMessageThreadItem.text is the closed-schema final snapshot.
            # Emit only the suffix not already observed, so a dropped delta is
            # repaired without replaying text that was already spoken.
            if visible and not snapshot.startswith(visible):
                # The caller treats an incompatible snapshot as a protocol
                # violation; do not silently speak an unrelated answer.
                raise CodexError("agent message snapshot does not extend deltas", code="invalid_response")
            pending = snapshot[len(visible):]
        elif not pending:
            return []
        if not pending:
            return []
        return [
            AgentEvent(
                AgentEventType.TEXT_DELTA,
                session_id=state.session_id,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                correlation_id=state.correlation_id,
                text=pending,
                item_id=item_id,
                phase="final_answer",
                speakable=True,
                data={"source": "item/completed", "reconciled": True},
            )
        ]

    async def reserve_turn(
        self,
        session_id: str,
        correlation_id: str,
        *,
        cwd: str | None = None,
    ) -> _TurnState:
        """Reserve a correlation before dispatching ``turn/start``.

        The bridge uses this small reservation window so an interrupt arriving
        immediately after a browser ``turn/start`` can record intent before a
        thread id or turn id exists.  The returned state is internal and must
        be consumed by :meth:`stream_turn` with the same session/correlation.
        """

        if not self._TURN_EXECUTION_ENABLED:
            raise CodexError(
                "Codex turn execution is unavailable",
                code="security_isolation_unavailable",
            )
        if not _valid_id(session_id) or not _valid_id(correlation_id):
            raise ValueError("session_id and correlation_id are required")
        await self._ensure_dispatcher()
        key = (session_id, correlation_id)
        async with self._state_lock:
            existing = self._pending_by_execution.get(key)
            if existing is not None:
                return existing
            cached = self._finished_by_execution.get(key)
            if cached is not None:
                raise CodexError("turn already finished", code="turn_not_found")
            if any(state.session_id == session_id for state in self._all_state_values_locked()):
                raise CodexBusyError()
            state = _TurnState(
                session_id=session_id,
                thread_id=None,
                correlation_id=correlation_id,
                queue_size=self.event_queue_size,
                cwd=cwd,
            )
            self._pending_by_execution[key] = state
            return state

    async def cancel_reservation(self, session_id: str, correlation_id: str) -> bool:
        """Atomically retire a reservation proven never to have dispatched."""

        async with self._state_lock:
            state = self._pending_by_execution.get((session_id, correlation_id))
            if state is None:
                return False
            return self._cancel_pristine_reservation_locked(state)

    def _cancel_pristine_reservation_locked(self, state: _TurnState) -> bool:
        """Write a safe release ledger while ``_state_lock`` is held."""

        key = (state.session_id, state.correlation_id)
        if (
            self._pending_by_execution.get(key) is not state
            or state.dispatch_may_have_started
            or state.thread_id is not None
            or state.turn_id is not None
            or state.terminal is not None
            or state.release_fence_active
            or state.release_poisoned
            or state.mapping_committed
        ):
            # Terminal/poisoned states retain their exact ownership. Deleting
            # them here would turn a known unsafe execution into "unknown".
            return False
        state.confirm_release_authority()
        state.turn_ready_event.set()
        state.push(
            AgentEvent(
                AgentEventType.ERROR,
                session_id=state.session_id,
                correlation_id=state.correlation_id,
                status="start_canceled",
                error="Codex turn was canceled before dispatch",
                data={"source": "reservation", "dispatched": False},
            )
        )
        terminal = state.terminal
        assert terminal is not None
        self._pending_by_execution.pop(key, None)
        self._finished_by_execution[key] = (terminal, True)
        self._finished_by_execution.move_to_end(key)
        while len(self._finished_by_execution) > self.finished_cache_size:
            self._finished_by_execution.popitem(last=False)
        state.released_event.set()
        return True

    def _all_state_values_locked(self) -> list[_TurnState]:
        unique: dict[int, _TurnState] = {}
        for state in (
            *self._pending_by_thread.values(),
            *self._pending_by_execution.values(),
            *self._active.values(),
        ):
            unique[id(state)] = state
        return list(unique.values())

    async def list_models(self) -> dict[str, list[dict[str, object]]]:
        """Return a bounded, picker-safe projection of the live App Server catalog."""

        result = await self.client.request(
            "model/list",
            {"limit": 100, "includeHidden": False},
        )
        raw_models = result.get("data")
        if not isinstance(raw_models, list):
            raise CodexError("Codex model catalog is invalid", code="invalid_response")

        def text(value: object, limit: int) -> str | None:
            return value if isinstance(value, str) and 0 < len(value) <= limit else None

        models: list[dict[str, object]] = []
        for raw in raw_models[:32]:
            if not isinstance(raw, Mapping) or raw.get("hidden") is True:
                continue
            model_id = text(raw.get("model") or raw.get("id"), 128)
            display_name = text(raw.get("displayName"), 128)
            description = text(raw.get("description"), 512)
            default_effort = text(raw.get("defaultReasoningEffort"), 32)
            raw_efforts = raw.get("supportedReasoningEfforts")
            raw_tiers = raw.get("serviceTiers")
            if None in {model_id, display_name, description, default_effort} or not isinstance(raw_efforts, list) or not isinstance(raw_tiers, list):
                continue
            efforts: list[dict[str, str]] = []
            for option in raw_efforts[:16]:
                if not isinstance(option, Mapping):
                    continue
                effort_id = text(option.get("reasoningEffort"), 32)
                effort_description = text(option.get("description"), 256)
                if effort_id is not None and effort_description is not None:
                    efforts.append({"id": effort_id, "description": effort_description})
            if not any(option["id"] == default_effort for option in efforts):
                continue
            tiers: list[dict[str, str]] = []
            for option in raw_tiers[:8]:
                if not isinstance(option, Mapping):
                    continue
                tier_id = text(option.get("id"), 64)
                name = text(option.get("name"), 64)
                tier_description = text(option.get("description"), 256)
                if tier_id is not None and name is not None and tier_description is not None:
                    tiers.append({"id": tier_id, "name": name, "description": tier_description})
            models.append({
                "id": model_id,
                "displayName": display_name,
                "description": description,
                "defaultReasoningEffort": default_effort,
                "supportedReasoningEfforts": efforts,
                "serviceTiers": tiers,
            })
        if not models:
            raise CodexError("Codex model catalog is empty", code="invalid_response")
        return {"models": models}

    async def stream_turn(
        self,
        session_id: str,
        text: str,
        *,
        correlation_id: str | None = None,
        cwd: str | None = None,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str = "low",
        service_tier: str | None = None,
        character: str = "default",
    ) -> AsyncIterator[AgentEvent]:
        """Start one turn and yield normalized events through its terminal."""

        if not self._TURN_EXECUTION_ENABLED:
            raise CodexError(
                "Codex turn execution is unavailable",
                code="security_isolation_unavailable",
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError("turn text must not be empty")
        if not _valid_id(session_id):
            raise ValueError("session_id is required")
        correlation = correlation_id or str(uuid.uuid4())
        if not _valid_id(correlation):
            raise ValueError("invalid correlation_id")
        if not isinstance(model, str) or not model or len(model) > 128:
            raise ValueError("invalid Codex model")
        if not isinstance(reasoning_effort, str) or not reasoning_effort or len(reasoning_effort) > 32:
            raise ValueError("invalid Codex reasoning effort")
        if service_tier is not None and (not isinstance(service_tier, str) or not service_tier or len(service_tier) > 64):
            raise ValueError("invalid Codex service tier")
        developer_instructions = developer_instructions_for(character)
        await self._ensure_dispatcher()
        key = (session_id, correlation)
        cached_event: AgentEvent | None = None
        async with self._state_lock:
            state = self._pending_by_execution.get(key)
            if state is None:
                cached = self._finished_by_execution.get(key)
                if cached is not None:
                    self._finished_by_execution.move_to_end(key)
                    cached_event = cached[0]
                else:
                    if any(existing.session_id == session_id for existing in self._all_state_values_locked()):
                        raise CodexBusyError()
                    state = _TurnState(
                        session_id=session_id,
                        thread_id=None,
                        correlation_id=correlation,
                        queue_size=self.event_queue_size,
                        cwd=cwd,
                    )
                    self._pending_by_execution[key] = state
            elif state.cwd is None:
                state.cwd = cwd
            if cached_event is None and state is not None and state.terminal is None:
                # This transition shares the exact lock used by isolate_turn.
                # Once visible, cancellation must conservatively assume that
                # ThreadManager/AppServer dispatch may occur.
                state.dispatch_may_have_started = True

        # Never yield while holding the global state lock: a consumer may
        # await the next turn/event and the release path needs the same lock.
        if cached_event is not None:
            yield cached_event
            return

        if state.terminal is not None:
            try:
                event = await state.queue.get()
                yield event
            finally:
                await self._finish_state(state)
            return

        turn_start_accepted = False
        try:
            try:
                thread_id = await self.thread_manager.ensure_thread(
                    session_id,
                    cwd=state.cwd,
                    developer_instructions=developer_instructions,
                )
                async with self._state_lock:
                    if any(
                        existing is not state and existing.thread_id == thread_id
                        for existing in self._all_state_values_locked()
                    ):
                        raise CodexBusyError()
                    state.thread_id = thread_id
                    self._pending_by_thread[thread_id] = state
                if state.terminal is not None:
                    # An early interrupt may have isolated the process before
                    # this coroutine obtained the thread lock.  Do not send a
                    # late turn/start that could create a ghost rollout.
                    state.turn_ready_event.set()
                    raise _TurnAlreadyTerminal()
                result = await self.client.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": text}],
                        "model": model,
                        "effort": reasoning_effort,
                        "serviceTier": service_tier,
                    },
                )
                # From this point forward the App Server explicitly accepted
                # turn/start. Any malformed business result or local replay
                # failure must be treated as a live/uncertain turn, not as a
                # pre-dispatch start rejection.
                turn_start_accepted = True
                turn = result.get("turn")
                turn_id = turn.get("id") if isinstance(turn, Mapping) else None
                if not isinstance(turn_id, str) or not turn_id:
                    raise CodexError("codex turn/start did not return a turn id", code="invalid_response")
                await self._bind_turn_ids(state, thread_id, turn_id)
                # The stable protocol sends turn/started, but synthesize a
                # single started event if a compatible server omits it.
                state.push(
                    AgentEvent(
                        AgentEventType.STARTED,
                        session_id=state.session_id,
                        thread_id=state.thread_id,
                        turn_id=state.turn_id,
                        correlation_id=state.correlation_id,
                        status="started",
                        data={"source": "turn/start"},
                    )
                )
                state.turn_ready_event.set()
                await self._schedule_interrupt_if_requested(state)
            except _TurnAlreadyTerminal:
                pass
            except CodexAmbiguousRequestError as exc:
                if exc.method == "turn/start":
                    # Do not publish/release a start failure until the exact
                    # shared process group is verified quiescent. The helper
                    # keeps this fence intact even if the stream is canceled
                    # while process termination is in flight.
                    await self._fence_uncertain_start(
                        state,
                        "ambiguous turn/start response",
                    )
                else:
                    # initialize/thread setup can fail after its own write,
                    # but no turn/start frame exists for this execution.
                    state.confirm_release_authority()
                    state.turn_ready_event.set()
                    state.push(_error_event(state, exc, status="start_failed"))
            except Exception as exc:  # noqa: BLE001 - public boundary
                if turn_start_accepted:
                    # A valid success response proves the turn exists; a later
                    # bind/replay failure has the same isolation requirement
                    # as a lost response.
                    await self._fence_uncertain_start(
                        state,
                        "turn/start post-response failure",
                    )
                else:
                    # Includes validation/startup failures and a generated-
                    # schema-valid JSON-RPC rejection. No turn/start success
                    # was observed, so this execution is safe to retire.
                    state.confirm_release_authority()
                    state.turn_ready_event.set()
                    state.push(_error_event(state, exc, status="start_failed"))

            while True:
                try:
                    event = await asyncio.wait_for(state.queue.get(), timeout=self.turn_timeout)
                except asyncio.TimeoutError:
                    if state.turn_id is not None and state.thread_id is not None:
                        try:
                            await self._interrupt_state(state)
                        except CodexError as exc:
                            state.push(_error_event(state, exc, status="timeout"))
                    else:
                        state.push(
                            _error_event(
                                state,
                                CodexTimeoutError("Codex turn exceeded its deadline"),
                                status="timeout",
                            )
                        )
                    event = await state.queue.get()
                yield event
                if event.terminal:
                    break
        except asyncio.CancelledError:
            # A disconnected WS must not abandon an in-flight turn.  The
            # cancellation intent also covers the early no-turn-id window.
            if state.terminal is None:
                try:
                    await asyncio.shield(self._interrupt_state(state))
                except Exception:
                    pass
            raise
        finally:
            await self._finish_state(state)

    async def _fence_uncertain_start(self, state: _TurnState, reason: str) -> None:
        """Finish a process isolation attempt before an uncertain start retires."""

        isolation_task = asyncio.create_task(self.isolate(reason))
        try:
            await asyncio.shield(isolation_task)
        except asyncio.CancelledError:
            # The app-server may already have accepted the turn. Preserve the
            # caller's cancellation, but only after the shared isolation and
            # mapping-cleanup fence has reached an authoritative outcome.
            try:
                await asyncio.shield(isolation_task)
            except Exception:
                pass
            raise
        except Exception:
            # isolate() already wrote a fixed isolation_failed terminal and a
            # poisoned release ledger; no raw failure crosses this boundary.
            pass
        finally:
            state.turn_ready_event.set()

    async def _bind_turn_ids(self, state: _TurnState, thread_id: str, turn_id: str) -> None:
        buffered: list[tuple[Any, dict[str, Any]]]
        # Hold the per-state lock across authoritative binding and replay. A
        # later terminal already in the shared subscriber queue therefore
        # cannot overtake the buffered started/delta sequence.
        async with state.notification_lock:
            async with self._state_lock:
                # The response is authoritative. A notification may have
                # supplied a different pair while the request was in flight;
                # never replace this id from the notification path.
                state.thread_id = thread_id
                state.turn_id = turn_id
                self._pending_by_thread.pop(thread_id, None)
                self._pending_by_execution.pop((state.session_id, state.correlation_id), None)
                self._active[(thread_id, turn_id)] = state
                state.turn_ready_event.set()
                buffered = state.buffered_notifications[:]
                state.buffered_notifications.clear()
                state.buffered_notification_counts.clear()
            # Only replay notifications whose exact pair matches the
            # authoritative response.  A stale old-turn terminal remains
            # dropped and cannot settle/re-key this reservation.
            for method, params in buffered:
                if self._notification_pair(params) != (thread_id, turn_id):
                    continue
                await self._dispatch_notification(
                    state,
                    method,
                    {"method": method, "params": params},
                )

    @staticmethod
    def _notification_pair(params: Mapping[str, Any]) -> tuple[str | None, str | None]:
        thread_id = _string_id(params.get("threadId"))
        turn_id = _string_id(params.get("turnId"))
        if turn_id is None:
            turn = params.get("turn")
            if isinstance(turn, Mapping):
                turn_id = _string_id(turn.get("id"))
        return thread_id, turn_id

    async def _schedule_interrupt_if_requested(self, state: _TurnState) -> None:
        async with self._state_lock:
            if state.cancel_requested and state.terminal is None and state.turn_id is not None:
                if state.interrupt_task is None:
                    state.interrupt_task = asyncio.create_task(self._send_interrupt(state))

    async def isolate(
        self,
        reason: str = "uncertain Codex turn",
        *,
        target: _TurnState | None = None,
    ) -> bool:
        """Isolate the shared process and invalidate every in-flight mapping."""

        async with self._state_lock:
            states = self._all_state_values_locked()
            # Block a stream generator's `finally` from retiring state while
            # process-group quiescence is still unknown. `_finish_state_once`
            # rechecks this bit under the same lock before removing indexes.
            for state in states:
                state.begin_release_fence()
        try:
            # AppServerClient only returns after the exact process group has
            # been verified gone.  A failed kill is not an
            # ``interrupt_isolated`` outcome: publishing that status would
            # let Host release maintenance while a ghost app-server remains.
            isolated = await self.client.isolate(reason)
            if not isolated:
                raise CodexError(
                    "Codex process isolation is not confirmed",
                    code="isolation_failed",
                )
        except Exception:
            for state in states:
                state.poison_release_authority()
                if state.terminal is None:
                    state.push(
                        AgentEvent(
                            AgentEventType.ERROR,
                            session_id=state.session_id,
                            thread_id=state.thread_id,
                            turn_id=state.turn_id,
                            correlation_id=state.correlation_id,
                            status="isolation_failed",
                            error="codex app-server process isolation failed",
                            data={"source": "host_isolate", "isolated": False},
                        )
                    )
                # Keep exact state/index/mapping ownership for a later
                # AppServerClient.close or isolate retry. The consumer may
                # observe the fixed terminal, but its generator `finally`
                # cannot publish a release ledger before reconciliation.
            raise
        for state in states:
            cleanup_failed = False
            # A turn/completed handler holds notification_lock across its
            # durable commit. Serialize invalidate -> authority -> terminal
            # behind that same lock, so an isolate cannot delete first, ACK,
            # and then let the blocked commit recreate a stale mapping.
            async with state.notification_lock:
                try:
                    await self.thread_manager.invalidate(state.session_id, state.thread_id)
                except Exception:
                    cleanup_failed = True
                    state.poison_release_authority()
                else:
                    state.complete_release_fence()
                # The verified internal app-server exit sentinel is normally
                # delivered by the subscriber task; make the safe terminal
                # explicit before retiring mappings so a disconnected Host
                # cannot receive an isolation ACK while a turn is still live.
                if state.terminal is None:
                    terminal_status = (
                        "isolation_failed"
                        if cleanup_failed
                        else "interrupt_isolated" if target is state else "process_exit"
                    )
                    terminal_error = (
                        "codex app-server state cleanup failed"
                        if cleanup_failed
                        else "codex app-server process was isolated"
                    )
                    state.push(
                        AgentEvent(
                            AgentEventType.ERROR,
                            session_id=state.session_id,
                            thread_id=state.thread_id,
                            turn_id=state.turn_id,
                            correlation_id=state.correlation_id,
                            status=terminal_status,
                            error=terminal_error,
                            data={
                                "source": "host_isolate",
                                "isolated": True,
                                "mapping_clean": not cleanup_failed,
                            },
                        )
                    )
            await self._finish_state(state)
        return True

    async def isolate_turn(
        self,
        session_id: str,
        execution_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        reason: str = "browser disconnect",
    ) -> Literal["released", "isolated"]:
        """Typed host/bridge seam for disconnect cancellation.

        Validation is session+correlation scoped, but isolation is intentionally
        process-wide because all turns share one app-server process.
        """

        if (
            not _valid_id(session_id)
            or not _valid_id(execution_id)
            or (thread_id is None) != (turn_id is None)
            or (thread_id is not None and not _valid_id(thread_id))
            or (turn_id is not None and not _valid_id(turn_id))
        ):
            raise CodexError("turn not found", code="turn_not_found")
        async with self._state_lock:
            state = self._pending_by_execution.get((session_id, execution_id))
            if state is None:
                state = next(
                    (
                        candidate
                        for candidate in self._all_state_values_locked()
                        if candidate.session_id == session_id and candidate.correlation_id == execution_id
                    ),
                    None,
                )
            if state is None:
                # Provider cleanup may have completed immediately before the
                # WS `turn/released` frame was delivered.  The exact bounded
                # execution ledger is an idempotent release/isolation ACK;
                # unknown identities still fail closed below.
                finished = self._finished_by_execution.get((session_id, execution_id))
                if finished is not None:
                    self._finished_by_execution.move_to_end((session_id, execution_id))
                    terminal = finished[0]
                    if thread_id is not None and (
                        terminal.thread_id != thread_id or terminal.turn_id != turn_id
                    ):
                        raise CodexError("turn not found", code="turn_not_found")
                    if not finished[1]:
                        raise CodexError(
                            "Codex process isolation is not confirmed",
                            code="isolation_failed",
                        )
                    return "released"
                raise CodexError("turn not found", code="turn_not_found")
            if thread_id is not None and (
                state.thread_id != thread_id or state.turn_id != turn_id
            ):
                raise CodexError("turn not found", code="turn_not_found")
            if self._cancel_pristine_reservation_locked(state):
                # No process or provisional mapping ever existed for this
                # execution. The atomic ledger is the exact idempotent ACK;
                # killing a shared AppServer here could disrupt unrelated auth.
                return "released"
        isolated = await self.isolate(reason, target=state)
        # `isolate()` performs the state cleanup itself. Awaiting this signal
        # documents and enforces the typed ACK contract for callers/tests.
        await asyncio.wait_for(state.released_event.wait(), timeout=self.client.config.shutdown_timeout)
        async with self._state_lock:
            finished = self._finished_by_execution.get((session_id, execution_id))
            release_safe = finished is not None and finished[1]
        if not release_safe:
            raise CodexError(
                "Codex process isolation is not confirmed",
                code="isolation_failed",
            )
        if not isolated:
            raise CodexError(
                "Codex process isolation is not confirmed",
                code="isolation_failed",
            )
        return "isolated"

    async def wait_for_execution_release(
        self,
        session_id: str,
        execution_id: str,
        *,
        timeout: float = 0.0,
    ) -> ExecutionReleaseStatus:
        """Return exact release authority for one public execution.

        Generator exhaustion is intentionally absent from this contract. A
        release exists only when ``_finish_state_once`` has atomically written
        the exact execution ledger and set its release event. Poison is sticky:
        an execution that exposed ``isolation_failed`` can later reconcile its
        process/mapping ownership, but it can never be upgraded into a normal
        Host release acknowledgement.
        """

        if not _valid_id(session_id) or not _valid_id(execution_id):
            raise ValueError("session_id and execution_id are required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 <= timeout <= self.client.config.shutdown_timeout
        ):
            raise ValueError("release wait timeout is outside the audited bound")

        async def snapshot() -> tuple[ExecutionReleaseStatus, asyncio.Event | None]:
            async with self._state_lock:
                key = (session_id, execution_id)
                finished = self._finished_by_execution.get(key)
                if finished is not None:
                    self._finished_by_execution.move_to_end(key)
                    return ("released" if finished[1] else "poisoned"), None
                state = self._pending_by_execution.get(key)
                if state is None:
                    state = next(
                        (
                            candidate
                            for candidate in self._all_state_values_locked()
                            if candidate.session_id == session_id
                            and candidate.correlation_id == execution_id
                        ),
                        None,
                    )
                if state is None:
                    return "unknown", None
                if state.release_poisoned:
                    return "poisoned", state.released_event
                return "pending", state.released_event

        status, released_event = await snapshot()
        if status != "pending" or timeout == 0 or released_event is None:
            return status
        try:
            await asyncio.wait_for(released_event.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return "pending"
        status, _released_event = await snapshot()
        return status

    async def interrupt(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str,
        execution_id: str | None = None,
    ) -> AgentEvent:
        """Interrupt exactly ``threadId`` + ``turnId`` and await terminal state."""

        if (
            not _valid_id(session_id)
            or not _valid_id(thread_id)
            or not _valid_id(turn_id)
            or (execution_id is not None and not _valid_id(execution_id))
        ):
            raise ValueError("session_id, thread_id and turn_id are required")
        key = (thread_id, turn_id)
        async with self._state_lock:
            state = self._active.get(key)
            if state is None:
                state = self._pending_by_thread.get(thread_id)
                if state is not None and state.turn_id != turn_id:
                    state = None
            if state is None:
                finished = self._finished.get(key)
                if (
                    finished is not None
                    and finished[0] == session_id
                    and (
                        execution_id is None
                        or finished[1].correlation_id == execution_id
                    )
                ):
                    self._finished.move_to_end(key)
                    return finished[1]
                raise CodexError("turn not found", code="turn_not_found")
            if state.session_id != session_id or (
                execution_id is not None and state.correlation_id != execution_id
            ):
                raise CodexError("turn/session mismatch", code="turn_not_found")
        return await self._interrupt_state(state)

    async def _interrupt_state(self, state: _TurnState) -> AgentEvent:
        """Record cancellation, resolve exact ids, and await terminal once."""

        state.cancel_requested = True
        if state.terminal is not None:
            return state.terminal
        try:
            await asyncio.wait_for(
                state.turn_ready_event.wait(),
                timeout=self.client.config.request_timeout,
            )
        except asyncio.TimeoutError as exc:
            return await self._isolate_uncertain_turn(state, cause=exc)
        if state.terminal is not None:
            return state.terminal
        if state.thread_id is None or state.turn_id is None:
            if state.terminal is not None:
                return state.terminal
            raise CodexError("turn not found", code="turn_not_found")
        async with self._state_lock:
            if state.terminal is not None:
                return state.terminal
            if state.interrupt_task is None:
                state.interrupt_task = asyncio.create_task(self._send_interrupt(state))
            interrupt_task = state.interrupt_task
        try:
            await asyncio.shield(interrupt_task)
        except Exception:
            # The authoritative terminal (or process exit) decides the public
            # outcome; never synthesize an interrupted event from a response.
            pass
        try:
            await asyncio.wait_for(state.terminal_event.wait(), timeout=self.client.config.request_timeout)
        except asyncio.TimeoutError as exc:
            # A responsive interrupt RPC without an authoritative terminal is
            # an uncertain rollout.  Isolate the shared process so every
            # active/pending state receives a safe process-exit terminal, then
            # invalidate all local mappings before any future resume.
            return await self._isolate_uncertain_turn(state, cause=exc)
        assert state.terminal is not None
        return state.terminal

    async def _isolate_uncertain_turn(self, state: _TurnState, *, cause: Exception) -> AgentEvent:
        """Terminate the shared process and produce only a safe error terminal."""

        try:
            await self.isolate("interrupt timeout", target=state)
        except Exception as isolate_error:
            # A failed process-group kill is not authoritative isolation. The
            # provider's isolate() path normally records this terminal for all
            # states; keep a local fallback for a failure before that write.
            if state.terminal is None:
                state.push(
                    AgentEvent(
                        AgentEventType.ERROR,
                        session_id=state.session_id,
                        thread_id=state.thread_id,
                        turn_id=state.turn_id,
                        correlation_id=state.correlation_id,
                        status="isolation_failed",
                        error="codex app-server process isolation failed",
                        data={"source": "interrupt", "isolated": False},
                    )
                )
            if state.terminal is not None:
                return state.terminal
            raise CodexError("Codex process isolation failed", code="isolation_failed") from isolate_error
        try:
            await asyncio.wait_for(
                state.terminal_event.wait(),
                timeout=max(0.1, min(self.client.config.request_timeout, self.client.config.shutdown_timeout)),
            )
        except asyncio.TimeoutError:
            if state.terminal is None:
                state.push(
                    AgentEvent(
                        AgentEventType.ERROR,
                        session_id=state.session_id,
                        thread_id=state.thread_id,
                        turn_id=state.turn_id,
                        correlation_id=state.correlation_id,
                        status="interrupt_isolated",
                        error="Codex turn terminal was not confirmed; app-server isolated",
                        data={"source": "interrupt", "isolated": True},
                    )
                )
        if state.terminal is not None:
            return state.terminal
        raise CodexTimeoutError("Codex turn was isolated after terminal timeout") from cause

    async def interrupt_by_reference(
        self,
        session_id: str,
        *,
        execution_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> AgentEvent:
        """Resolve a browser execution reference to exact app-server IDs.

        ``thread_id`` + ``turn_id`` is preferred.  For compatibility with the
        existing browser adapter, ``execution_id`` (the public correlation id)
        can resolve one and only one in-flight turn for the same DSH session;
        the service then calls :meth:`interrupt` with the exact pair.  A
        caller-supplied turn id is additionally checked, so a stale/mixed
        session cannot interrupt another turn.
        """

        if (thread_id is None) != (turn_id is None):
            raise CodexError("turn not found", code="turn_not_found")
        if thread_id is not None and turn_id is not None:
            return await self.interrupt(
                session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                execution_id=execution_id,
            )
        if not _valid_id(session_id) or not _valid_id(execution_id):
            raise CodexError("execution_id is required when thread_id is omitted", code="turn_not_found")
        async with self._state_lock:
            early_state: _TurnState | None = None
            candidates = [
                state
                for state in self._all_state_values_locked()
                if state.session_id == session_id
                and state.correlation_id == execution_id
                and (turn_id is None or state.turn_id == turn_id)
            ]
            if not candidates:
                finished_keys = [
                    (thread, finished_turn)
                    for (thread, finished_turn), (owner, event) in self._finished.items()
                    if owner == session_id
                    and event.correlation_id == execution_id
                    and (turn_id is None or finished_turn == turn_id)
                ]
                if len(finished_keys) == 1:
                    exact_thread_id, exact_turn_id = finished_keys[0]
                    # `interrupt` returns the cached authoritative terminal;
                    # no second app-server request is sent.
                    candidates = []
                else:
                    exact_thread_id = exact_turn_id = None
            else:
                exact_thread_id = exact_turn_id = None
            if len(candidates) != 1:
                if exact_thread_id is None or exact_turn_id is None:
                    raise CodexError("turn not found", code="turn_not_found")
            else:
                state = candidates[0]
                if state.turn_id is None:
                    # Early interrupt: preserve intent and wait for stream_turn
                    # to bind the exact server ids.
                    early_state = state
                    exact_thread_id = exact_turn_id = None
                else:
                    exact_thread_id, exact_turn_id = state.thread_id, state.turn_id
                    early_state = None
        if early_state is not None:
            return await self._interrupt_state(early_state)
        return await self.interrupt(
            session_id,
            thread_id=exact_thread_id,
            turn_id=exact_turn_id,
        )

    async def _send_interrupt(self, state: _TurnState) -> None:
        assert state.turn_id is not None
        await self.client.request(
            "turn/interrupt",
            {"threadId": state.thread_id, "turnId": state.turn_id},
        )

    async def _finish_state(self, state: _TurnState) -> None:
        while True:
            if (
                state.released_event.is_set()
                or state.release_fence_active
                or not state.retirement_authorized
            ):
                return
            # Several owners can converge on the same state: the stream
            # generator finally, an isolate operation, and a late internal
            # app-server terminal. Share one complete cleanup task so the
            # execution ledger is not published between mapping cleanup and
            # released_event.
            task = state.release_task
            if task is None:
                task = asyncio.create_task(self._finish_state_once(state))
                state.release_task = task
            try:
                await asyncio.shield(task)
            except BaseException:
                # A failed discard must remain retryable for reconciliation,
                # but concurrent callers still share the failed attempt.
                if state.release_task is task:
                    state.release_task = None
                raise
            if state.released_event.is_set():
                return
            if state.release_task is task:
                # An isolation fence may have revoked retirement while this
                # task was awaiting provisional cleanup. If a verified fence
                # already restored authority, loop and perform a fresh,
                # complete release attempt; otherwise retain ownership.
                state.release_task = None

    async def _finish_state_once(self, state: _TurnState) -> None:
        if state.release_fence_active or not state.retirement_authorized:
            return
        terminal = state.terminal
        # Complete the mapping cleanup before removing active/pending indexes
        # or publishing the execution ledger. A lost `turn/released` frame can
        # then safely use the ledger as an idempotent ACK only after this fence.
        if not state.mapping_committed:
            await self.thread_manager.discard_provisional(state.session_id, state.thread_id)
        async with self._state_lock:
            if state.released_event.is_set():
                return
            if state.release_fence_active or not state.retirement_authorized:
                return
            self._pending_by_execution.pop((state.session_id, state.correlation_id), None)
            if state.thread_id is not None:
                if self._pending_by_thread.get(state.thread_id) is state:
                    self._pending_by_thread.pop(state.thread_id, None)
            if state.thread_id is not None and state.turn_id is not None:
                self._active.pop((state.thread_id, state.turn_id), None)
                if terminal is not None:
                    pair = (state.thread_id, state.turn_id)
                    execution_key = (state.session_id, state.correlation_id)
                    self._finished[pair] = (state.session_id, terminal)
                    self._finished.move_to_end(pair)
                    if state.terminal_wire_digest is not None:
                        self._finished_terminal_digests[pair] = (
                            execution_key,
                            state.terminal_wire_digest,
                        )
                        self._finished_terminal_digests.move_to_end(pair)
                    else:
                        self._finished_terminal_digests.pop(pair, None)
                    while len(self._finished) > self.finished_cache_size:
                        evicted_pair, _evicted = self._finished.popitem(last=False)
                        self._finished_terminal_digests.pop(evicted_pair, None)
            if terminal is not None:
                # The success bit is explicit and sticky. In particular,
                # start_failed says nothing about whether turn/start bytes
                # reached the peer, and a mapping-cleanup failure cannot be
                # repaired by merely relabeling the terminal.
                release_succeeded = (
                    state.release_authoritative and not state.release_poisoned
                )
                key = (state.session_id, state.correlation_id)
                self._finished_by_execution[key] = (terminal, release_succeeded)
                self._finished_by_execution.move_to_end(key)
                while len(self._finished_by_execution) > self.finished_cache_size:
                    self._finished_by_execution.popitem(last=False)
            # Set only after both mapping cleanup and the typed ledger write.
            state.released_event.set()

    async def health(self) -> dict[str, Any]:
        payload = await self.client.health()
        async with self._state_lock:
            payload.update(
                {
                    "active_turns": len(self._active) + len(self._pending_by_thread),
                    "thread_mappings": len(await self.thread_manager.all_mappings()),
                }
            )
        return payload

    async def close(self) -> None:
        async with self._state_lock:
            states = self._all_state_values_locked()
            owned_states = [
                state
                for state in states
                if not state.released_event.is_set()
                and (
                    state.terminal is None
                    or state.release_poisoned
                    or not state.retirement_authorized
                )
            ]
            for state in owned_states:
                state.begin_release_fence()
        task = self._dispatcher_task
        self._dispatcher_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._notification_queue is not None:
            self.client.unsubscribe(self._notification_queue)
            self._notification_queue = None

        if not owned_states:
            return

        # Shutdown owns one shared App Server, so one process-wide fence is
        # bounded independently of the number of turns. The fallback retries
        # the same retained process handle through AppServerClient.close(); it
        # is not a second per-state timeout. Keep enough budget for both of the
        # client's individually bounded termination attempts.
        shutdown_budget = max(0.1, self.client.config.shutdown_timeout)
        deadline = asyncio.get_running_loop().time() + (shutdown_budget * 2) + 0.2
        quiescent = False
        try:
            await asyncio.wait_for(
                self.client.isolate("provider close"),
                timeout=min(shutdown_budget + 0.1, deadline - asyncio.get_running_loop().time()),
            )
            quiescent = True
        except Exception:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(self.client.close(), timeout=remaining)
                    quiescent = True
                except Exception:
                    quiescent = False

        if not quiescent:
            # No terminal, index removal, mapping deletion, or ledger write is
            # allowed without proven process quiescence. Retaining the exact
            # states and durable mappings is the reconciliation authority for
            # a later close retry.
            for state in owned_states:
                state.poison_release_authority()
            raise CodexProcessError("codex app-server shutdown could not be verified")

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            for state in owned_states:
                state.poison_release_authority()
            raise CodexProcessError("codex app-server state cleanup timed out")
        async def cleanup_owned_state(state: _TurnState) -> None:
            async with state.notification_lock:
                try:
                    await self.thread_manager.invalidate(state.session_id, state.thread_id)
                except BaseException:
                    state.poison_release_authority()
                    raise
                state.complete_release_fence()
                if state.terminal is None:
                    state.push(
                        AgentEvent(
                            AgentEventType.ERROR,
                            session_id=state.session_id,
                            thread_id=state.thread_id,
                            turn_id=state.turn_id,
                            correlation_id=state.correlation_id,
                            status="process_exit",
                            error="codex app-server process was isolated",
                            data={
                                "source": "provider_close",
                                "isolated": True,
                                "mapping_clean": True,
                            },
                        )
                    )

        try:
            cleanup_results = await asyncio.wait_for(
                asyncio.gather(
                    *(cleanup_owned_state(state) for state in owned_states),
                    return_exceptions=True,
                ),
                timeout=remaining,
            )
        except Exception as exc:
            for state in owned_states:
                state.poison_release_authority()
            raise CodexProcessError("codex app-server state cleanup timed out") from exc
        cleanup_failed = False
        for state, result in zip(owned_states, cleanup_results, strict=True):
            if isinstance(result, BaseException):
                state.poison_release_authority()
                cleanup_failed = True
                continue
            await self._finish_state(state)
        if cleanup_failed:
            # Failed states deliberately remain indexed and mapping-owned;
            # successful peers have already crossed their complete fence.
            raise CodexProcessError("codex app-server state cleanup failed")


def _turn_terminal_digest(message: Mapping[str, Any]) -> str:
    """Hash the generated terminal params without retaining or logging them."""

    params = message.get("params")
    encoded = json.dumps(
        params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512


def _string_id(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _error_event(state: _TurnState, exc: Exception, *, status: str) -> AgentEvent:
    if isinstance(exc, CodexProcessError):
        error = "codex app-server process exited"
    elif isinstance(exc, CodexTimeoutError):
        error = "codex operation timed out"
    elif isinstance(exc, CodexError):
        safe_codes = {
            "turn_not_found": "turn not found",
            "turn_in_progress": "a Codex turn is already active",
            "codex_version_unsupported": "unsupported Codex app-server version",
            "invalid_response": "invalid Codex app-server response",
            "thread_ownership_conflict": "Codex thread ownership conflict",
            "mapping_commit_failed": "Codex thread mapping could not be persisted",
            "mapping_corrupt": "Codex thread mapping could not be read",
            "mapping_version_unsupported": "Codex thread mapping version is unsupported",
            "security_isolation_unavailable": "Codex turn execution is unavailable",
            "isolation_failed": "Codex process isolation failed",
        }
        error = safe_codes.get(str(exc.code), "Codex operation failed")
    elif isinstance(exc, ValueError):
        error = "invalid request"
    else:
        error = "Codex operation failed"
    return AgentEvent(
        AgentEventType.ERROR,
        session_id=state.session_id,
        thread_id=state.thread_id,
        turn_id=state.turn_id,
        correlation_id=state.correlation_id,
        status=status,
        error=error,
        data={"source": "provider"},
    )
