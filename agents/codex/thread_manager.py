"""Session-to-thread persistence and stable thread lifecycle helpers.

The app-server thread id is an implementation detail, but it must survive a
bridge restart so a DSH session can resume its Codex context.  The mapping
file deliberately stores only a versioned thread id and a one-way fingerprint
of the resolved workspace; it never stores the raw cwd or credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .app_server_client import AppServerClient, JsonRpcError
from .types import CodexError


MAPPING_VERSION = 2
_MAX_MAPPING_ID = 512
class _DuplicateMappingKey(ValueError):
    """Internal parse failure for an ambiguous JSON object."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateMappingKey
        value[key] = item
    return value


@dataclass(frozen=True)
class ThreadMapping:
    """One persisted session entry.

    ``cwd_fingerprint`` is ``sha256:<hex>`` of the resolved path.  A path is
    intentionally not serialized: the bridge only needs equality checking,
    and exposing paths in runtime state is unnecessary information.
    """

    thread_id: str
    cwd_fingerprint: str | None = None
    durable: bool = True
    legacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "thread_id": self.thread_id,
            "durable": bool(self.durable),
        }
        if self.cwd_fingerprint:
            payload["cwd_fingerprint"] = self.cwd_fingerprint
        return payload


class ThreadMappingStore:
    """Atomic, versioned JSON mapping store with v1 compatibility."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": MAPPING_VERSION, "sessions": {}}
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateMappingKey):
            # Never silently rebuild a corrupt cache: doing so could select a
            # new remote thread and fork durable history. The caller must
            # quarantine/reconcile this session explicitly.
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt") from None
        if not isinstance(raw, Mapping):
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        version = raw.get("version")
        if type(version) is not int:
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        if version not in (1, MAPPING_VERSION):
            raise CodexError("Codex thread mapping version is unsupported", code="mapping_version_unsupported")
        root_keys = set(raw)
        if version == 1:
            if root_keys != {"version", "sessions"}:
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        elif not {"version", "sessions"}.issubset(root_keys) or not root_keys.issubset(
            {"version", "sessions", "updated_at"}
        ):
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        updated_at = raw.get("updated_at")
        if "updated_at" in raw and (type(updated_at) is not int or updated_at < 0):
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        sessions = raw.get("sessions")
        if not isinstance(sessions, Mapping):
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        return dict(raw)

    def read_entries(self) -> dict[str, ThreadMapping]:
        return self._entries_from_payload(self._read_payload())

    @staticmethod
    def _entries_from_payload(payload: Mapping[str, Any]) -> dict[str, ThreadMapping]:
        version = payload["version"]
        sessions = payload.get("sessions", {})
        entries: dict[str, ThreadMapping] = {}
        if not isinstance(sessions, Mapping):
            raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
        for session_id, value in sessions.items():
            if not isinstance(session_id, str) or not session_id or len(session_id) > _MAX_MAPPING_ID:
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            if version == 1 and isinstance(value, str):
                # Version 1 was {"sessions": {session_id: thread_id}}.
                if not value or len(value) > _MAX_MAPPING_ID:
                    raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
                entries[session_id] = ThreadMapping(value, legacy=True)
                continue
            if version == 1:
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            if not isinstance(value, Mapping):
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            allowed = {"thread_id", "cwd_fingerprint", "durable"}
            if any(key not in allowed for key in value):
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            thread_id = value.get("thread_id")
            fingerprint = value.get("cwd_fingerprint")
            durable = value.get("durable")
            if not isinstance(thread_id, str) or not thread_id or len(thread_id) > _MAX_MAPPING_ID:
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            if not isinstance(durable, bool):
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            if fingerprint is not None and (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
            ):
                raise CodexError("Codex thread mapping is corrupt", code="mapping_corrupt")
            entries[session_id] = ThreadMapping(
                thread_id,
                cwd_fingerprint=fingerprint,
                durable=durable,
                legacy=version < MAPPING_VERSION,
            )
        return entries

    def read(self) -> dict[str, str]:
        """Compatibility view used by older callers/tests."""

        return {session: entry.thread_id for session, entry in self.read_entries().items()}

    def entry(self, session_id: str) -> ThreadMapping | None:
        return self.read_entries().get(session_id)

    async def set(
        self,
        session_id: str,
        thread_id: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        cwd_fingerprint: str | None = None,
        durable: bool = True,
    ) -> None:
        if (
            not isinstance(session_id, str)
            or not isinstance(thread_id, str)
            or not session_id
            or not thread_id
            or len(session_id) > _MAX_MAPPING_ID
            or len(thread_id) > _MAX_MAPPING_ID
        ):
            raise ValueError("session_id and thread_id are required")
        if type(durable) is not bool:
            raise ValueError("durable must be a boolean")
        if cwd_fingerprint is None and cwd is not None:
            _resolved, cwd_fingerprint = cwd_metadata(cwd)
        if cwd_fingerprint is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", cwd_fingerprint) is None:
            raise ValueError("cwd_fingerprint must be a sha256 fingerprint")
        mapping = ThreadMapping(thread_id, cwd_fingerprint, durable=durable)
        async with self._lock:
            payload = self._read_payload()
            entries = self._entries_from_payload(payload)
            sessions = {
                owner: entry.to_dict()
                for owner, entry in entries.items()
            }
            sessions[session_id] = mapping.to_dict()
            payload = {
                "version": MAPPING_VERSION,
                "sessions": sessions,
                "updated_at": int(time.time()),
            }
            self._atomic_write(payload)

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            payload = self._read_payload()
            entries = self._entries_from_payload(payload)
            if session_id not in entries:
                return
            del entries[session_id]
            payload = {
                "version": MAPPING_VERSION,
                "sessions": {
                    owner: entry.to_dict()
                    for owner, entry in entries.items()
                },
                "updated_at": int(time.time()),
            }
            self._atomic_write(payload)

    def _atomic_write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            # A successful rename is not enough for crash recovery: persist
            # the parent directory entry before advertising the new mapping.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                raise CodexError("Codex thread mapping could not be durably written", code="mapping_commit_failed") from None
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def cwd_metadata(cwd: str | os.PathLike[str] | None) -> tuple[str | None, str | None]:
    if cwd is None:
        return None, None
    try:
        path = Path(cwd).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError):
        raise ValueError("invalid Codex workspace") from None
    if not path.is_dir():
        raise ValueError("Codex workspace must be an existing directory")
    resolved = str(path)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return resolved, f"sha256:{digest}"


def same_workspace(entry: ThreadMapping | None, cwd_fingerprint: str | None) -> bool:
    if entry is None:
        return False
    # Legacy v1 entries did not bind a workspace.  Reuse them only when the
    # caller also omitted cwd; a configured workspace must create a fresh
    # thread rather than accidentally resuming an unscoped rollout.
    if entry.legacy or not entry.cwd_fingerprint:
        return cwd_fingerprint is None
    return entry.cwd_fingerprint == cwd_fingerprint


def is_missing_rollout_error(error: JsonRpcError, expected_thread_id: str) -> bool:
    """Classify only the pinned exact, request-bound rollout grammar."""

    if not isinstance(error.message, str) or not isinstance(expected_thread_id, str):
        return False
    if not expected_thread_id or len(expected_thread_id) > _MAX_MAPPING_ID:
        return False
    # Pinned Codex 0.149 reports an empty, non-materialized thread with this
    # exact message. Bind the classifier to the requested durable id: a
    # provider diagnostic containing a different/embedded id must never cause
    # DSH to delete and rebuild the local ownership mapping.
    message = error.message.strip()
    canonical = f"no rollout found for thread id {expected_thread_id}"
    return message in {canonical, canonical + "."}


class ThreadManager:
    """Create/resume stable threads and commit after successful terminal."""

    def __init__(
        self,
        client: AppServerClient,
        store: ThreadMappingStore,
        *,
        sandbox: str = "read-only",
        approval_policy: str = "never",
    ) -> None:
        # Until a typed approval gateway exists, coding-capable sandboxes are
        # intentionally unavailable.  A server-request denial is not a safe
        # substitute when a thread is configured with approvalPolicy=never.
        if sandbox != "read-only":
            raise ValueError("Codex command execution is disabled; credential read-deny isolation is unavailable")
        # No ApprovalGateway is wired into the host seam yet. Accept exactly
        # `never`; server-request denial alone is not an approval protocol and
        # must not silently turn this path into a write-capable mode.
        if approval_policy != "never":
            raise ValueError("Codex approval policy must be never until ApprovalGateway is wired")
        self.client = client
        self.store = store
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self._locks: dict[str, asyncio.Lock] = {}
        self._provisional: dict[str, ThreadMapping] = {}
        self._provisional_lock = asyncio.Lock()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def ensure_thread(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        developer_instructions: str | None = None,
    ) -> str:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise ValueError("invalid session_id")
        if developer_instructions is not None and (
            not isinstance(developer_instructions, str)
            or not developer_instructions
            or len(developer_instructions) > 8_000
        ):
            raise ValueError("invalid developer instructions")
        resolved_cwd, fingerprint = cwd_metadata(cwd)
        async with self._session_lock(session_id):
            async with self._provisional_lock:
                provisional = self._provisional.get(session_id)
            if provisional is not None:
                if same_workspace(provisional, fingerprint):
                    return provisional.thread_id
                async with self._provisional_lock:
                    self._provisional.pop(session_id, None)

            entry = self.store.entry(session_id)
            # A corrupted/manual file can assign one remote thread to two
            # DSH sessions. Never choose a winner or delete the other owner:
            # continuity requires explicit operator reconciliation.
            if entry is not None:
                owners = [
                    owner
                    for owner, candidate in self.store.read_entries().items()
                    if candidate.thread_id == entry.thread_id
                ]
                if len(owners) > 1:
                    raise CodexError("Codex thread ownership conflict", code="thread_ownership_conflict")
            if entry is not None and not same_workspace(entry, fingerprint):
                # Cwd-bound entries cannot be safely resumed in another
                # workspace.  Remove only this session's stale local mapping.
                await self.store.remove(session_id)
                entry = None
            if entry is not None:
                try:
                    params: dict[str, Any] = {
                        "threadId": entry.thread_id,
                        "sandbox": self.sandbox,
                        "approvalPolicy": self.approval_policy,
                        # Explicit null clears a persona when the user changes
                        # this same DSH conversation back to the generic role.
                        "developerInstructions": developer_instructions,
                    }
                    if resolved_cwd is not None:
                        params["cwd"] = resolved_cwd
                    result = await self.client.request("thread/resume", params)
                    thread_id = _thread_id(result)
                    # The persisted id is the durable ownership authority.
                    # A missing or different id can mean the server resumed a
                    # foreign thread; never silently rebind this DSH session.
                    if thread_id != entry.thread_id:
                        raise CodexError(
                            "Codex thread resume identity mismatch",
                            code="thread_ownership_conflict",
                        )
                    await self._claim_provisional(session_id, thread_id, fingerprint)
                    return thread_id
                except JsonRpcError as error:
                    if not is_missing_rollout_error(error, entry.thread_id):
                        raise
                    await self.store.remove(session_id)

            params = {
                "ephemeral": False,
                "sandbox": self.sandbox,
                "approvalPolicy": self.approval_policy,
                "developerInstructions": developer_instructions,
            }
            if resolved_cwd is not None:
                params["cwd"] = resolved_cwd
            result = await self.client.request("thread/start", params)
            thread_id = _thread_id(result)
            if thread_id is None:
                raise RuntimeError("Codex thread/start did not return a thread id")
            await self._claim_provisional(session_id, thread_id, fingerprint)
            return thread_id

    async def _claim_provisional(self, session_id: str, thread_id: str, fingerprint: str | None) -> None:
        """Atomically check persisted/provisional owners and claim the thread."""

        async with self._provisional_lock:
            # The ownership check and provisional write must share one lock.
            # Reading/checking first and remembering in a second critical
            # section lets two sessions both accept the same server thread
            # during a forced network contention race.
            entries = self.store.read_entries()
            persisted_owners = sorted(
                owner for owner, candidate in entries.items()
                if candidate.thread_id == thread_id and owner != session_id
            )
            provisional_owners = sorted(
                owner for owner, candidate in self._provisional.items()
                if candidate.thread_id == thread_id and owner != session_id
            )
            if persisted_owners or provisional_owners:
                raise CodexError("Codex thread ownership conflict", code="thread_ownership_conflict")
            self._provisional[session_id] = ThreadMapping(
                thread_id,
                cwd_fingerprint=fingerprint,
                durable=False,
            )

    async def commit_thread(self, session_id: str, thread_id: str, *, cwd: str | None = None) -> None:
        """Persist only after a successful authoritative turn terminal."""

        _resolved, fingerprint = cwd_metadata(cwd)
        await self.store.set(session_id, thread_id, cwd_fingerprint=fingerprint, durable=True)
        async with self._provisional_lock:
            current = self._provisional.get(session_id)
            if current is None or current.thread_id == thread_id:
                self._provisional.pop(session_id, None)

    async def discard_provisional(self, session_id: str, thread_id: str | None = None) -> None:
        async with self._provisional_lock:
            current = self._provisional.get(session_id)
            if current is not None and (thread_id is None or current.thread_id == thread_id):
                self._provisional.pop(session_id, None)

    async def invalidate(self, session_id: str, thread_id: str | None = None) -> None:
        """Remove uncertain local state after a process isolation boundary."""

        entry = self.store.entry(session_id)
        if entry is not None and (thread_id is None or entry.thread_id == thread_id):
            await self.store.remove(session_id)
        await self.discard_provisional(session_id, thread_id)

    async def _assert_unique_thread(self, session_id: str, thread_id: str) -> None:
        async with self._provisional_lock:
            entries = self.store.read_entries()
            owners = sorted(
                owner for owner, candidate in entries.items()
                if candidate.thread_id == thread_id and owner != session_id
            )
            provisional_owners = sorted(
                owner
                for owner, candidate in self._provisional.items()
                if candidate.thread_id == thread_id and owner != session_id
            )
            if owners or provisional_owners:
                raise CodexError("Codex thread ownership conflict", code="thread_ownership_conflict")

    async def mapping(self, session_id: str) -> str | None:
        async with self._provisional_lock:
            provisional = self._provisional.get(session_id)
        if provisional is not None:
            return provisional.thread_id
        entry = self.store.entry(session_id)
        return entry.thread_id if entry is not None else None

    async def all_mappings(self) -> dict[str, str]:
        entries = self.store.read_entries()
        async with self._provisional_lock:
            for session_id, entry in self._provisional.items():
                entries[session_id] = entry
        return {session_id: entry.thread_id for session_id, entry in entries.items()}


def _thread_id(result: Mapping[str, Any]) -> str | None:
    thread = result.get("thread")
    if not isinstance(thread, Mapping):
        return None
    value = thread.get("id")
    return value if isinstance(value, str) and value else None
