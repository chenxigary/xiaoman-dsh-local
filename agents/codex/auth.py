"""Secret-free account and browser-login facade over Codex app-server."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from .app_server_client import (
    INTERNAL_APP_SERVER_EXITED,
    INTERNAL_APP_SERVER_ISOLATION_FAILED,
    AppServerClient,
    CodexAmbiguousRequestError,
)
from .types import CodexError, LoginState, LoginStatus


_PLAN_TYPES = {
    "free",
    "go",
    "plus",
    "pro",
    "prolite",
    "team",
    "self_serve_business_prolite",
    "self_serve_business_usage_based",
    "business",
    "ent26",
    "enterprise_cbp_automation",
    "enterprise_cbp_usage_based",
    "enterprise",
    "edu",
    "unknown",
}
_LOGIN_STATE_LIMIT = 64
_LOGIN_STATE_TTL_SECONDS = 10 * 60
_LOGIN_OPERATION_LIMIT = 64
_LOGIN_OPERATION_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


@dataclass
class _LoginOperation:
    """One process-lifetime idempotency owner for browser login start."""

    task: asyncio.Task[LoginState]
    login_id: str | None = None
    state: LoginState | None = None


class CodexAuthService:
    """Expose account state and ChatGPT browser login without token handling."""

    def __init__(self, client: AppServerClient) -> None:
        self.client = client
        self._states: OrderedDict[str, LoginState] = OrderedDict()
        self._state_expiry: dict[str, float] = {}
        self._early_completions: OrderedDict[str, LoginState] = OrderedDict()
        self._early_expiry: dict[str, float] = {}
        # Operation owners are never TTL/LRU-evicted. The fixed capacity
        # fails closed instead of forgetting an accepted id and accidentally
        # starting a second remote browser flow on a late HTTP retry.
        self._operations: OrderedDict[str, _LoginOperation] = OrderedDict()
        self._operation_by_login: dict[str, str] = {}
        self._state_lock = asyncio.Lock()
        # Start/cancel are process-facing ownership operations. Serialize them
        # so two callers cannot create an untracked remote login between the
        # local pending-state check and response registration.
        self._login_operation_lock = asyncio.Lock()
        self._notification_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._closing = False

    async def _ensure_listener(self) -> None:
        if self._notification_task is not None and not self._notification_task.done():
            return
        if self._notification_queue is not None:
            self.client.unsubscribe(self._notification_queue)
        self._notification_queue = self.client.subscribe()
        self._notification_task = asyncio.create_task(self._notification_loop(self._notification_queue))

    async def account_read(self) -> dict[str, Any]:
        # Keep the App Server capability contract explicit. Omitting this flag
        # lets a server choose token-refresh behavior that this safe facade
        # must never expose or implicitly authorize.
        result = await self.client.request("account/read", {"refreshToken": False})
        account = result.get("account")
        safe_account: dict[str, Any] | None = None
        if _valid_chatgpt_account(account):
            # Explicit allowlist: never return access/refresh tokens, API keys,
            # cookies, auth file paths, email/identity fields, or arbitrary
            # app-server account fields.  The browser only needs account type
            # and plan capability; identity belongs inside Codex.
            safe_fields = {
                key: account[key]
                for key in ("type", "planType")
                if isinstance(account.get(key), str)
            }
            safe_account = safe_fields or None
        requires_auth = result.get("requiresOpenaiAuth")
        requires_auth = requires_auth if isinstance(requires_auth, bool) else True
        return {
            "account": safe_account,
            "requires_openai_auth": requires_auth,
            "logged_in": safe_account is not None,
            # Stable browser-facing status spelling; account fields remain
            # explicitly allow-listed above.
            "state": "ready" if safe_account is not None else "signed_out",
        }

    async def login_start(self, operation_id: str | None = None) -> LoginState:
        """Start or reconcile one exact browser-login operation.

        A bridge caller supplies a canonical UUIDv4. Legacy in-process callers
        get a fresh UUID, but all paths enter the same bounded registry. The
        registry task, rather than an individual HTTP request task, owns the
        App Server write so caller cancellation cannot create an untracked
        remote login flow.
        """

        operation_id = _validate_login_operation_id(
            operation_id if operation_id is not None else str(uuid.uuid4())
        )
        async with self._state_lock:
            if self._closing:
                raise CodexError("Codex auth service is shutting down", code="shutting_down")
        await self._ensure_listener()
        async with self._state_lock:
            if self._closing:
                raise CodexError("Codex auth service is shutting down", code="shutting_down")
            existing = self._operations.get(operation_id)
            if existing is not None:
                task = existing.task
            else:
                self._prune_locked()
                if any(
                    state.status is LoginStatus.PENDING
                    for state in self._states.values()
                ) or any(not owner.task.done() for owner in self._operations.values()):
                    raise CodexError(
                        "a browser login is already pending",
                        code="login_in_progress",
                    )
                if len(self._operations) >= _LOGIN_OPERATION_LIMIT:
                    raise CodexError(
                        "browser login operation capacity is exhausted",
                        code="login_operation_capacity",
                    )
                task = asyncio.create_task(self._login_start_once(operation_id))
                # Retries still observe the task's exact fixed exception, but
                # retrieving it here avoids an unhandled-task warning if the
                # only HTTP waiter disconnects before a failure is recorded.
                task.add_done_callback(_consume_login_operation_exception)
                self._operations[operation_id] = _LoginOperation(task=task)

        # Shield the registry-owned task from an HTTP caller abort/timeout.
        state = await asyncio.shield(task)
        async with self._state_lock:
            owner = self._operations.get(operation_id)
            if owner is not None and owner.state is not None:
                return owner.state
        return state

    async def _login_start_once(self, operation_id: str) -> LoginState:
        async with self._login_operation_lock:
            # Only the browser ChatGPT flow is exposed. API-key and raw-token
            # login variants are intentionally not accepted by this boundary.
            try:
                result = await self.client.request(
                    "account/login/start",
                    {"type": "chatgpt", "appBrand": "chatgpt"},
                )
            except CodexAmbiguousRequestError:
                # The remote flow may exist even though no loginId/authUrl was
                # received. Isolate the exact current AppServer generation;
                # never retry and create a second unowned flow.
                await self._handle_ambiguous_login_request()
                raise
            login_id = result.get("loginId")
            auth_url = result.get("authUrl")
            if not isinstance(login_id, str) or not isinstance(auth_url, str):
                raise RuntimeError("codex browser login did not return an auth URL")
            login_id = _validate_login_id(login_id)
            state = LoginState(login_id=login_id, status=LoginStatus.PENDING, auth_url=auth_url)
            operation_conflict = False
            async with self._state_lock:
                self._prune_locked()
                prior_owner = self._operation_by_login.get(login_id)
                if prior_owner is not None and prior_owner != operation_id:
                    operation_conflict = True
                else:
                    early = self._early_completions.pop(login_id, None)
                    self._early_expiry.pop(login_id, None)
                    if early is not None:
                        state = early
                    self._remember_locked(state)
                    owner = self._operations.get(operation_id)
                    current_task = asyncio.current_task()
                    if owner is None or owner.task is not current_task:
                        operation_conflict = True
                    else:
                        owner.login_id = login_id
                        owner.state = state
                        self._operation_by_login[login_id] = operation_id
            if operation_conflict:
                # A reused remote loginId or lost local owner means the new
                # remote flow cannot be reconciled safely. Kill the exact
                # App Server generation before returning a fixed conflict.
                await self._handle_ambiguous_login_request()
                raise CodexError(
                    "browser login operation conflicts with an existing flow",
                    code="login_operation_conflict",
                )
            return state

    async def login_cancel(self, login_id: str) -> LoginState:
        login_id = _validate_login_id(login_id)
        async with self._login_operation_lock:
            async with self._state_lock:
                self._prune_locked()
                current = self._states.get(login_id)
                if current is None:
                    return LoginState(login_id=login_id, status=LoginStatus.NOT_FOUND)
                if current.status is not LoginStatus.PENDING:
                    return current
            try:
                result = await self.client.request(
                    "account/login/cancel",
                    {"loginId": login_id},
                )
            except CodexAmbiguousRequestError:
                # Cancel may have taken effect, or the remote login may still
                # be alive. Only process isolation closes both possibilities.
                await self._handle_ambiguous_login_request()
                raise
            wire_status = result.get("status")
            if wire_status not in {"canceled", "notFound"}:
                raise RuntimeError("codex browser login cancellation returned an invalid status")
            state = LoginState(
                login_id=login_id,
                status=(
                    LoginStatus.CANCELED
                    if wire_status == "canceled"
                    else LoginStatus.NOT_FOUND
                ),
                success=False,
            )
            async with self._state_lock:
                # A completion may race cancel; never overwrite a terminal state.
                current = self._states.get(login_id)
                if current is not None and current.status is not LoginStatus.PENDING:
                    return current
                self._remember_locked(state)
            return state

    async def _handle_ambiguous_login_request(self) -> None:
        """Boundedly isolate an auth operation whose write may have landed."""

        shutdown_timeout = self.client.config.shutdown_timeout
        failure = "process_exit"
        # isolate() joins the exact generation's single-flight teardown when
        # the reader has already detected malformed/schema-invalid output.
        # Do not release the operation lock until process-group quiescence is
        # verified, even when the pending response future failed first.
        try:
            await asyncio.wait_for(
                self.client.isolate("ambiguous browser login request"),
                timeout=shutdown_timeout + 0.25,
            )
        except Exception:
            failure = "isolation_failed"
        async with self._state_lock:
            self._early_completions.clear()
            self._early_expiry.clear()
            for login_id, current in tuple(self._states.items()):
                if current.status is LoginStatus.PENDING:
                    self._remember_locked(
                        LoginState(
                            login_id=login_id,
                            status=LoginStatus.FAILED,
                            success=False,
                            error=failure,
                        )
                    )

    async def login_status(self, login_id: str) -> LoginState:
        login_id = _validate_login_id(login_id)
        async with self._state_lock:
            self._prune_locked()
            return self._states.get(
                login_id,
                LoginState(login_id=login_id, status=LoginStatus.NOT_FOUND),
            )

    async def _notification_loop(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                message = await queue.get()
                method = message.get("method")
                if method in {
                    INTERNAL_APP_SERVER_EXITED,
                    INTERNAL_APP_SERVER_ISOLATION_FAILED,
                }:
                    error = (
                        "isolation_failed"
                        if method == INTERNAL_APP_SERVER_ISOLATION_FAILED
                        else "process_exit"
                    )
                    async with self._state_lock:
                        self._early_completions.clear()
                        self._early_expiry.clear()
                        for login_id, current in tuple(self._states.items()):
                            if current.status is LoginStatus.PENDING:
                                self._remember_locked(
                                    LoginState(
                                        login_id=login_id,
                                        status=LoginStatus.FAILED,
                                        success=False,
                                        error=error,
                                    )
                                )
                    continue
                if method != "account/login/completed":
                    continue
                params = message.get("params")
                if not isinstance(params, Mapping):
                    continue
                login_id_value = params.get("loginId")
                if not isinstance(login_id_value, str):
                    continue
                success = params.get("success")
                if not isinstance(success, bool):
                    continue
                state = LoginState(
                    login_id=login_id_value,
                    # The generated stable surface uses one completion method:
                    # success=false is failure. Cancellation is authoritative
                    # only through account/login/cancel's generated response.
                    status=LoginStatus.COMPLETED if success else LoginStatus.FAILED,
                    success=success,
                    error=_safe_error(params.get("error")) if not success else None,
                )
                conflict = False
                async with self._state_lock:
                    self._prune_locked()
                    current = self._states.get(login_id_value)
                    # The app-server may share a notification stream with
                    # another Host session. Foreign/late/duplicate IDs must
                    # not create unbounded state or mutate a terminal flow.
                    if current is not None and current.status is LoginStatus.PENDING:
                        self._remember_locked(state)
                        continue
                    if current is not None and current.status in {
                        LoginStatus.COMPLETED,
                        LoginStatus.FAILED,
                    }:
                        if _terminal_signature(current) != _terminal_signature(state):
                            conflict = True
                            self._remember_locked(_login_protocol_conflict(login_id_value))
                    elif current is None:
                        early = self._early_completions.get(login_id_value)
                        if early is None:
                            self._remember_early_locked(state)
                        elif _terminal_signature(early) != _terminal_signature(state):
                            conflict = True
                            self._remember_early_locked(
                                _login_protocol_conflict(login_id_value)
                            )
                if conflict:
                    try:
                        await self.client.isolate("conflicting login terminal")
                    except CodexError:
                        # State remains a fixed fail-closed protocol conflict;
                        # the AppServerClient retains its own isolation poison.
                        pass
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        async with self._state_lock:
            self._closing = True
            operation_tasks = [owner.task for owner in self._operations.values()]
        task = self._notification_task
        self._notification_task = None
        tasks = [candidate for candidate in operation_tasks if not candidate.done()]
        if task is not None and not task.done():
            tasks.append(task)
        for candidate in tasks:
            candidate.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._notification_queue is not None:
            self.client.unsubscribe(self._notification_queue)
            self._notification_queue = None
        async with self._state_lock:
            self._operations.clear()
            self._operation_by_login.clear()

    def _remember_locked(self, state: LoginState) -> None:
        self._states[state.login_id] = state
        self._states.move_to_end(state.login_id)
        self._state_expiry[state.login_id] = time.monotonic() + _LOGIN_STATE_TTL_SECONDS
        operation_id = self._operation_by_login.get(state.login_id)
        if operation_id is not None:
            owner = self._operations.get(operation_id)
            if owner is not None:
                owner.state = state
        while len(self._states) > _LOGIN_STATE_LIMIT:
            evictable = next(
                (
                    login_id
                    for login_id, candidate in self._states.items()
                    if candidate.status is not LoginStatus.PENDING
                ),
                None,
            )
            if evictable is None:
                break
            self._states.pop(evictable, None)
            self._state_expiry.pop(evictable, None)

    def _remember_early_locked(self, state: LoginState) -> None:
        self._early_completions[state.login_id] = state
        self._early_completions.move_to_end(state.login_id)
        self._early_expiry[state.login_id] = time.monotonic() + _LOGIN_STATE_TTL_SECONDS
        while len(self._early_completions) > _LOGIN_STATE_LIMIT:
            login_id, _state = self._early_completions.popitem(last=False)
            self._early_expiry.pop(login_id, None)

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            login_id
            for login_id, deadline in self._state_expiry.items()
            if deadline <= now
            and self._states.get(login_id, LoginState(login_id, LoginStatus.NOT_FOUND)).status
            is not LoginStatus.PENDING
        ]
        for login_id in expired:
            self._states.pop(login_id, None)
            self._state_expiry.pop(login_id, None)
        early_expired = [
            login_id
            for login_id, deadline in self._early_expiry.items()
            if deadline <= now
        ]
        for login_id in early_expired:
            self._early_completions.pop(login_id, None)
            self._early_expiry.pop(login_id, None)


def _validate_login_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid login id")
    return value


def _validate_login_operation_id(value: str) -> str:
    if not isinstance(value, str) or _LOGIN_OPERATION_ID.fullmatch(value) is None:
        raise ValueError("invalid login operation id")
    return value


def _consume_login_operation_exception(task: asyncio.Task[LoginState]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def _terminal_signature(state: LoginState) -> tuple[LoginStatus, bool, str | None]:
    return state.status, state.success, state.error


def _login_protocol_conflict(login_id: str) -> LoginState:
    return LoginState(
        login_id=login_id,
        status=LoginStatus.FAILED,
        success=False,
        error="protocol_conflict",
    )


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    # Login failures can contain provider diagnostics, URLs, paths, or
    # account material.  Expose a stable category only; raw app-server text
    # never crosses the browser boundary.
    if isinstance(value, Mapping):
        code = value.get("code") or value.get("type")
        if isinstance(code, str):
            normalized = code.lower()
            if "cancel" in normalized:
                return "login_canceled"
            if "timeout" in normalized:
                return "login_timeout"
            if "auth" in normalized or "login" in normalized:
                return "login_failed"
    return "login_failed"


def _valid_chatgpt_account(value: Any) -> bool:
    """Validate the stable ChatGPT Account union before reporting ready."""

    if not isinstance(value, Mapping) or value.get("type") != "chatgpt":
        return False
    # The wire schema requires these keys even though email is intentionally
    # omitted from the public response.
    if "email" not in value or "planType" not in value:
        return False
    email = value.get("email")
    plan = value.get("planType")
    return (
        (email is None or isinstance(email, str))
        and isinstance(plan, str)
        and plan in _PLAN_TYPES
    )
