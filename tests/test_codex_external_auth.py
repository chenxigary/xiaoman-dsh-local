"""Tests for the split-process ChatGPT Subscription credential boundary."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.codex import ChatgptSubscriptionBroker, CodexError, prepare_isolated_home
from agents.codex.app_server_client import AppServerConfig, scrubbed_child_environment
from agents.codex.schema_validator import CodexSchemaValidationError, StableProtocolValidator


class _ManagedClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method, params):
        self.calls.append((method, params))
        return {
            "account": {"type": "chatgpt", "email": None, "planType": "plus"},
            "requiresOpenaiAuth": True,
        }


class _ExecutionClient:
    def __init__(self) -> None:
        self.params: dict | None = None

    async def request(self, method, params, *, ensure_started=True):
        if method != "account/login/start" or ensure_started is not False:
            raise AssertionError("unexpected execution request")
        self.params = params
        return {"type": "chatgptAuthTokens"}


class CodexExternalAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_refreshes_managed_auth_and_keeps_execution_home_credential_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_home = root / "managed"
            auth_home.mkdir(mode=0o700)
            auth_file = auth_home / "auth.json"
            auth_file.write_text(
                json.dumps({
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": "header.payload.signature",
                        "account_id": "account-1",
                        "id_token": "unused",
                        "refresh_token": "never-forwarded",
                    },
                }),
                encoding="utf-8",
            )
            auth_file.chmod(0o600)
            execution_home = prepare_isolated_home(root / "execution")
            managed = _ManagedClient()
            execution = _ExecutionClient()
            broker = ChatgptSubscriptionBroker(managed, auth_file, execution_home)  # type: ignore[arg-type]

            await broker.bootstrap(execution)  # type: ignore[arg-type]

            self.assertEqual(managed.calls, [("account/read", {"refreshToken": True})])
            assert execution.params is not None
            self.assertEqual(execution.params["type"], "chatgptAuthTokens")
            self.assertEqual(execution.params["chatgptPlanType"], "plus")
            self.assertNotIn("refreshToken", execution.params)
            self.assertFalse((execution_home / ".codex/auth.json").exists())

    async def test_broker_fails_closed_for_permissive_or_persisted_auth_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_file = root / "auth.json"
            auth_file.write_text(
                json.dumps({"tokens": {"access_token": "secret-token", "account_id": "account-1"}}),
                encoding="utf-8",
            )
            auth_file.chmod(0o644)
            execution_home = prepare_isolated_home(root / "execution")
            broker = ChatgptSubscriptionBroker(_ManagedClient(), auth_file, execution_home)  # type: ignore[arg-type]
            with self.assertRaises(CodexError):
                await broker.tokens()

            auth_file.chmod(0o600)
            (execution_home / ".codex/state.db").write_text("secret-token", encoding="utf-8")
            with self.assertRaises(CodexError) as context:
                await broker.bootstrap(_ExecutionClient())  # type: ignore[arg-type]
            self.assertEqual(context.exception.code, "security_isolation_unavailable")

    async def test_broker_ignores_a_transient_file_removed_during_residue_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth_file = root / "auth.json"
            auth_file.write_text(
                json.dumps({"tokens": {"access_token": "secret-token", "account_id": "account-1"}}),
                encoding="utf-8",
            )
            auth_file.chmod(0o600)
            execution_home = prepare_isolated_home(root / "execution")
            vanished = execution_home / ".codex/.tmp/git-vanished"
            broker = ChatgptSubscriptionBroker(_ManagedClient(), auth_file, execution_home)  # type: ignore[arg-type]

            with patch.object(Path, "rglob", return_value=iter((vanished,))):
                await broker.bootstrap(_ExecutionClient())  # type: ignore[arg-type]

    async def test_isolated_environment_replaces_both_home_locators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prior_home = os.environ.get("HOME")
            prior_codex_home = os.environ.get("CODEX_HOME")
            try:
                os.environ["HOME"] = "/managed/home"
                os.environ["CODEX_HOME"] = "/managed/codex"
                environment = scrubbed_child_environment(isolated_home=temporary)
            finally:
                if prior_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = prior_home
                if prior_codex_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = prior_codex_home
            self.assertEqual(environment["HOME"], temporary)
            self.assertEqual(environment["CODEX_HOME"], str(Path(temporary) / ".codex"))

    async def test_external_auth_protocol_is_explicitly_scoped(self) -> None:
        external = StableProtocolValidator(external_chatgpt_auth=True)
        stable = StableProtocolValidator()
        initialize = {
            "clientInfo": {"name": "xiaoman-dsh", "title": "Xiaoman", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        }
        self.assertEqual(external.validate_request("initialize", initialize), initialize)
        login = {
            "type": "chatgptAuthTokens",
            "accessToken": "header.payload.signature",
            "chatgptAccountId": "account-1",
            "chatgptPlanType": "plus",
        }
        self.assertEqual(external.validate_request("account/login/start", login), login)
        with self.assertRaises(CodexSchemaValidationError):
            stable.validate_request("account/login/start", login)

    async def test_config_rejects_mixed_managed_and_external_auth(self) -> None:
        with self.assertRaises(ValueError):
            AppServerConfig(external_chatgpt_auth=True, managed_token_refresh=True)


if __name__ == "__main__":
    unittest.main()
