"""Credential-isolated ChatGPT Subscription authentication for Codex turns.

The managed Codex process owns browser login and token refresh in the real
Codex home, but never starts a thread or turn.  A second execution process gets
only a short-lived access token over private stdio and runs with an empty HOME
and CODEX_HOME.  Consequently shell/read tools cannot reach the operator's
refresh token or managed ``auth.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .app_server_client import AppServerClient
from .types import CodexError


_MAX_AUTH_FILE_BYTES = 1_048_576
_MAX_ACCESS_TOKEN_CHARS = 32_768
_MAX_ACCOUNT_ID_CHARS = 256
_MAX_PLAN_CHARS = 128
_RESIDUE_SCAN_FILE_BYTES = 16 * 1024 * 1024


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


@dataclass(frozen=True)
class ExternalChatgptTokens:
    access_token: str
    account_id: str
    plan_type: str | None

    def login_params(self) -> dict[str, Any]:
        return {
            "type": "chatgptAuthTokens",
            "accessToken": self.access_token,
            "chatgptAccountId": self.account_id,
            "chatgptPlanType": self.plan_type,
        }

    def refresh_result(self) -> dict[str, Any]:
        return {
            "accessToken": self.access_token,
            "chatgptAccountId": self.account_id,
            "chatgptPlanType": self.plan_type,
        }


def managed_auth_file() -> Path:
    """Resolve the operator-owned Codex auth file before environment isolation."""

    raw_home = os.environ.get("CODEX_HOME")
    root = Path(raw_home).expanduser() if raw_home else Path.home() / ".codex"
    return root.resolve(strict=False) / "auth.json"


def prepare_isolated_home(path: Path) -> Path:
    """Create and validate one credential-free execution home."""

    resolved = path.expanduser().resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    codex_home = resolved / ".codex"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    for candidate in (resolved, codex_home):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CodexError("Codex execution home is unsafe", code="security_isolation_unavailable")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise CodexError("Codex execution home is unsafe", code="security_isolation_unavailable")
        os.chmod(candidate, 0o700)
    if (codex_home / "auth.json").exists():
        raise CodexError("Codex execution home contains credentials", code="security_isolation_unavailable")
    return resolved


class ChatgptSubscriptionBroker:
    """Issue externally managed access tokens without exposing refresh state."""

    def __init__(
        self,
        managed_client: AppServerClient,
        auth_file: Path,
        execution_home: Path,
    ) -> None:
        self.managed_client = managed_client
        self.auth_file = auth_file
        self.execution_home = execution_home
        self._lock = asyncio.Lock()

    async def tokens(self) -> ExternalChatgptTokens:
        async with self._lock:
            # The managed process is the sole refresh-token owner. A proactive
            # refresh updates its private auth file before the bridge reads the
            # new access token for the isolated execution process.
            result = await self.managed_client.request("account/read", {"refreshToken": True})
            account = result.get("account")
            plan = account.get("planType") if isinstance(account, Mapping) else None
            if plan is not None and (not isinstance(plan, str) or len(plan) > _MAX_PLAN_CHARS):
                raise CodexError("ChatGPT subscription metadata is invalid", code="not_authenticated")
            return await asyncio.to_thread(self._read_tokens, plan)

    async def bootstrap(self, execution_client: AppServerClient) -> None:
        issued = await self.tokens()
        result = await execution_client.request(
            "account/login/start",
            issued.login_params(),
            ensure_started=False,
        )
        if result != {"type": "chatgptAuthTokens"}:
            raise CodexError("external ChatGPT login failed", code="not_authenticated")
        await asyncio.to_thread(self._assert_no_persisted_token, issued.access_token)

    async def refresh(self, _params: Mapping[str, Any]) -> Mapping[str, Any]:
        issued = await self.tokens()
        return issued.refresh_result()

    def _read_tokens(self, plan_type: str | None) -> ExternalChatgptTokens:
        path = self.auth_file
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > _MAX_AUTH_FILE_BYTES
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                or info.st_mode & 0o077
            ):
                raise OSError
            raw = path.read_text(encoding="utf-8")
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey):
            raise CodexError("managed ChatGPT login is unavailable", code="not_authenticated") from None
        tokens = value.get("tokens") if isinstance(value, Mapping) else None
        access = tokens.get("access_token") if isinstance(tokens, Mapping) else None
        account_id = tokens.get("account_id") if isinstance(tokens, Mapping) else None
        if (
            not isinstance(access, str)
            or not 0 < len(access) <= _MAX_ACCESS_TOKEN_CHARS
            or not isinstance(account_id, str)
            or not 0 < len(account_id) <= _MAX_ACCOUNT_ID_CHARS
        ):
            raise CodexError("managed ChatGPT login is unavailable", code="not_authenticated")
        return ExternalChatgptTokens(access, account_id, plan_type)

    def _assert_no_persisted_token(self, access_token: str) -> None:
        codex_home = self.execution_home / ".codex"
        if (codex_home / "auth.json").exists():
            raise CodexError("isolated Codex persisted credentials", code="security_isolation_unavailable")
        needle = access_token.encode("utf-8")
        try:
            candidates = tuple(codex_home.rglob("*"))
        except OSError:
            raise CodexError("Codex execution home could not be verified", code="security_isolation_unavailable") from None
        for candidate in candidates:
            try:
                info = candidate.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_size <= _RESIDUE_SCAN_FILE_BYTES
                    and needle in candidate.read_bytes()
                ):
                    raise CodexError("isolated Codex persisted credentials", code="security_isolation_unavailable")
            except CodexError:
                raise
            except FileNotFoundError:
                # App Server creates and removes short-lived `.tmp/git-*`
                # directories while bootstrap is still settling.  A path
                # that vanished after the bounded snapshot cannot retain the
                # issued token; treating this normal unlink race as an
                # unverifiable home makes every fresh external-auth process
                # fail closed before its first turn.
                continue
            except OSError:
                raise CodexError("Codex execution home could not be verified", code="security_isolation_unavailable") from None
