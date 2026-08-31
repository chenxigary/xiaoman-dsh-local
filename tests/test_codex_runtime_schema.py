"""Runtime tests for the pinned generated stable Codex schema closure."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agents.codex.app_server_client import (
    INTERNAL_APP_SERVER_EXITED,
    AppServerClient,
    AppServerConfig,
    CodexAmbiguousRequestError,
    scrubbed_child_environment,
)
from agents.codex.compatibility import EXPECTED_CLI_VERSION, STABLE_PROTOCOL_MANIFEST
from agents.codex.provider import CodexAgentService
from agents.codex.schema_validator import (
    CodexSchemaValidationError,
    StableProtocolValidator,
    verify_checked_in_stable_schema,
)
from agents.codex.thread_manager import ThreadManager, ThreadMappingStore


EXPECTED_SCHEMA_SHA256 = "9b3de71a5a2ffc980b792a18aa8f8dec3f85f48829560222a0264fe494b679a9"


SCHEMA_TEST_SERVER = r'''
import json
import sys

mode = sys.argv[1]
audit_path = sys.argv[2]

def audit(value):
    with open(audit_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def send_raw(value):
    sys.stdout.write(value + "\n")
    sys.stdout.flush()

for raw in sys.stdin:
    message = json.loads(raw)
    audit(message)
    method = message.get("method")
    if method == "initialize":
        send({
            "id": message.get("id"),
            "result": {
                "codexHome": "/tmp/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "test",
                "userAgent": "fake",
            },
        })
    elif method == "initialized":
        if mode == "unknown-notification":
            send({"method": "app/list/updated", "params": {"data": []}})
        elif mode == "malformed-notification":
            send({
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"status": "completed"}},
            })
        elif mode == "wire-process-exited":
            send({
                "method": "process/exited",
                "params": {
                    "exitCode": 0,
                    "processHandle": "spawn-1",
                    "stderr": "",
                    "stderrCapReached": False,
                    "stdout": "",
                    "stdoutCapReached": False,
                },
            })
        elif mode == "unknown-request":
            send({
                "id": "opaque-secret-request-id",
                "method": "future/request",
                "params": {"raw": "/secret/path", "threadId": "/secret/path"},
            })
        elif mode == "oversized-notification":
            send({
                "method": "account/login/completed",
                "params": {
                    "loginId": "l" * 257,
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            })
        elif mode == "junk-envelope":
            send_raw("{}")
        elif mode == "id-only-envelope":
            send_raw('{"id":1}')
        elif mode == "result-only-envelope":
            send_raw('{"result":{}}')
        elif mode == "bool-response-id":
            send_raw('{"id":true,"result":{}}')
        elif mode == "string-response-id":
            send_raw('{"id":"1","result":{}}')
        elif mode == "unknown-response-id":
            send_raw('{"id":999999,"result":{}}')
        elif mode == "mixed-envelope":
            send_raw('{"id":1,"method":"turn/completed","params":{},"result":{}}')
        elif mode == "duplicate-id":
            send_raw('{"id":1,"id":1,"result":{}}')
        elif mode == "duplicate-method":
            send_raw('{"method":"skills/changed","method":"turn/completed","params":{}}')
        elif mode == "duplicate-nested-id":
            send_raw('{"method":"turn/completed","params":{"threadId":"thread-1","turn":{"id":"turn-1","id":"turn-2","items":[],"status":"completed"}}}')
        elif mode == "nonfinite-json":
            send_raw('{"method":"account/login/completed","params":{"loginId":"login-1","success":true,"error":NaN,"onboardingEntrypoint":null}}')
    elif method == "account/read":
        if mode == "response-mismatch":
            send({
                "id": message.get("id"),
                "result": {"raw": "/secret/path", "turnId": "not-an-account"},
            })
        elif mode == "auth-bootstrap-notification":
            send({
                "method": "remoteControl/status/changed",
                "emittedAtMs": 1,
                "params": {
                    "status": "disabled",
                    "serverName": "fake-server",
                    "installationId": "fake-installation",
                    "environmentId": None,
                },
            })
            send({
                "id": message.get("id"),
                "result": {"account": None, "requiresOpenaiAuth": True},
            })
    elif method == "account/login/start" and mode == "auth-bootstrap-notification":
        send({
            "id": message.get("id"),
            "result": {
                "type": "chatgpt",
                "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake",
                "loginId": "fake-login",
            },
        })
    elif method == "account/login/start" and mode == "oversized-auth-response":
        send({
            "id": message.get("id"),
            "result": {
                "type": "chatgpt",
                "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake",
                "loginId": "l" * 257,
            },
        })
    elif method == "account/login/cancel" and mode == "auth-bootstrap-notification":
        send({"id": message.get("id"), "result": {"status": "canceled"}})
'''


def _thread(thread_id: str = "thread-1") -> dict[str, object]:
    return {
        "cliVersion": "0.149.0-alpha.4.1",
        "createdAt": 1,
        "cwd": "/tmp/fake-workspace",
        "ephemeral": False,
        "id": thread_id,
        "modelProvider": "openai",
        "preview": "",
        "projectId": None,
        "sessionId": "session-1",
        "source": "appServer",
        "status": {"type": "idle"},
        "turns": [],
        "updatedAt": 1,
    }


def _thread_response(thread_id: str = "thread-1") -> dict[str, object]:
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": "/tmp/fake-workspace",
        "model": "gpt-5",
        "modelProvider": "openai",
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "thread": _thread(thread_id),
    }


class StableProtocolValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = StableProtocolValidator()

    def test_checked_in_generated_tree_and_bundle_are_exact(self) -> None:
        self.assertEqual(verify_checked_in_stable_schema(), EXPECTED_SCHEMA_SHA256)
        self.assertEqual(self.validator.schema_sha256, EXPECTED_SCHEMA_SHA256)

    def test_ten_method_business_allowlist_accepts_generated_params(self) -> None:
        requests = {
            "initialize": {
                "clientInfo": {
                    "name": "xiaoman-dsh",
                    "title": "Xiaoman DSH direct Codex agent",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "requestAttestation": False,
                },
            },
            "account/read": {"refreshToken": False},
            "account/login/start": {"type": "chatgpt", "appBrand": "chatgpt"},
            "account/login/cancel": {"loginId": "login-1"},
            "model/list": {"limit": 100, "includeHidden": False},
            "thread/start": {
                "ephemeral": False,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": None,
            },
            "thread/resume": {
                "threadId": "thread-1",
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "developerInstructions": None,
            },
            "turn/start": {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "model": "gpt-5.4-mini",
                "effort": "low",
                "serviceTier": None,
            },
            "turn/steer": {
                "threadId": "thread-1",
                "expectedTurnId": "turn-1",
                "input": [{"type": "text", "text": "more"}],
            },
            "turn/interrupt": {"threadId": "thread-1", "turnId": "turn-1"},
        }
        self.assertEqual(len(requests), 10)
        for method, params in requests.items():
            with self.subTest(method=method):
                self.assertEqual(self.validator.validate_request(method, params), params)

        with self.assertRaises(CodexSchemaValidationError) as context:
            self.validator.validate_request("model/list", {"limit": 101, "includeHidden": False})
        self.assertEqual(str(context.exception), "Codex stable protocol request validation failed")

    def test_business_policy_rejects_generated_but_unsafe_variants(self) -> None:
        invalid = (
            ("account/read", {"refreshToken": True}),
            ("account/login/start", {"type": "apiKey", "apiKey": "secret"}),
            (
                "thread/start",
                {
                    "ephemeral": False,
                    "sandbox": "danger-full-access",
                    "approvalPolicy": "on-request",
                    "cwd": "/secret/path",
                },
            ),
            (
                "turn/start",
                {
                    "threadId": "thread-1",
                    "input": [{"type": "text", "text": "hello"}],
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                },
            ),
        )
        for method, params in invalid:
            with self.subTest(method=method):
                with self.assertRaises(CodexSchemaValidationError) as context:
                    self.validator.validate_request(method, params)
                self.assertEqual(
                    str(context.exception),
                    "Codex stable protocol request validation failed",
                )
                self.assertNotIn("secret", str(context.exception))
                self.assertNotIn("path", str(context.exception).lower())

    def test_generated_response_schema_is_selected_by_original_method(self) -> None:
        responses = {
            "initialize": {
                "codexHome": "/tmp/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
                "userAgent": "codex-cli 0.149.0-alpha.4.1",
            },
            "account/read": {
                "account": {"type": "chatgpt", "email": None, "planType": "pro"},
                "requiresOpenaiAuth": True,
            },
            "account/login/start": {
                "type": "chatgpt",
                "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake",
                "loginId": "login-1",
            },
            "account/login/cancel": {"status": "canceled"},
            "thread/start": _thread_response(),
            "thread/resume": _thread_response(),
            "turn/start": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}},
            "turn/steer": {"turnId": "turn-1"},
            "turn/interrupt": {},
        }
        for method, result in responses.items():
            with self.subTest(method=method):
                self.assertEqual(self.validator.validate_response(method, result), result)
        self.assertEqual(
            self.validator.validate_response("account/login/cancel", {"status": "notFound"}),
            {"status": "notFound"},
        )

        with self.assertRaises(CodexSchemaValidationError) as context:
            self.validator.validate_response("account/read", {"turnId": "/secret/path"})
        self.assertEqual(
            str(context.exception),
            "Codex stable protocol response validation failed",
        )
        self.assertNotIn("secret", str(context.exception))

    def test_generated_unbounded_identifiers_and_auth_urls_hit_business_limits(self) -> None:
        oversized_responses = {
            "account/login/start": {
                "type": "chatgpt",
                "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake",
                "loginId": "l" * 257,
            },
            "thread/resume": _thread_response("t" * 513),
            "turn/start": {
                "turn": {"id": "u" * 513, "items": [], "status": "inProgress"},
            },
            "turn/steer": {"turnId": "u" * 513},
        }
        for method, result in oversized_responses.items():
            with self.subTest(method=method):
                self.assertTrue(
                    self.validator._schemas.response_results[method].is_valid(result)  # noqa: SLF001
                )
                with self.assertRaises(CodexSchemaValidationError) as context:
                    self.validator.validate_response(method, result)
                self.assertEqual(
                    str(context.exception),
                    "Codex stable protocol response validation failed",
                )

        for auth_url in (
            "https://evil.example/oauth/authorize",
            "https://auth.openai.com/not-oauth",
            "https://auth.openai.com/oauth/authorize?state=" + "x" * 2048,
        ):
            result = {
                "type": "chatgpt",
                "authUrl": auth_url,
                "loginId": "login-1",
            }
            self.assertTrue(
                self.validator._schemas.response_results["account/login/start"].is_valid(result)  # noqa: SLF001
            )
            with self.assertRaises(CodexSchemaValidationError):
                self.validator.validate_response("account/login/start", result)

    def test_thread_response_is_bound_to_pinned_execution_context(self) -> None:
        request = {
            "threadId": "thread-1",
            "cwd": "/tmp/fake-workspace",
            "sandbox": "read-only",
            "approvalPolicy": "never",
        }
        valid = _thread_response("thread-1")
        self.assertEqual(
            self.validator.validate_response("thread/resume", valid, request),
            valid,
        )

        variants = []
        wrong_top_cwd = _thread_response("thread-1")
        wrong_top_cwd["cwd"] = "/tmp/foreign"
        variants.append(wrong_top_cwd)
        wrong_thread_cwd = _thread_response("thread-1")
        wrong_thread_cwd["thread"]["cwd"] = "/tmp/foreign"  # type: ignore[index]
        variants.append(wrong_thread_cwd)
        ephemeral = _thread_response("thread-1")
        ephemeral["thread"]["ephemeral"] = True  # type: ignore[index]
        variants.append(ephemeral)
        wrong_cli = _thread_response("thread-1")
        wrong_cli["thread"]["cliVersion"] = "0.148.0-alpha.8"  # type: ignore[index]
        variants.append(wrong_cli)
        wrong_id = _thread_response("thread-foreign")
        variants.append(wrong_id)

        for result in variants:
            self.assertTrue(
                self.validator._schemas.response_results["thread/resume"].is_valid(result)  # noqa: SLF001
            )
            with self.assertRaises(CodexSchemaValidationError) as context:
                self.validator.validate_response("thread/resume", result, request)
            self.assertEqual(
                str(context.exception),
                "Codex stable protocol response validation failed",
            )

    def test_generated_unbounded_notification_ids_hit_business_limits(self) -> None:
        messages = (
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t" * 513,
                    "turn": {"id": "turn-1", "items": [], "status": "completed"},
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "u" * 513, "items": [], "status": "completed"},
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "completedAtMs": 1,
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "i" * 257, "type": "agentMessage", "text": "safe"},
                },
            },
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "l" * 257,
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            },
        )
        for message in messages:
            with self.subTest(method=message["method"]):
                self.assertTrue(
                    self.validator._schemas.server_notification.is_valid(message)  # noqa: SLF001
                )
                with self.assertRaises(CodexSchemaValidationError) as context:
                    self.validator.validate_server_notification(message)
                self.assertEqual(
                    str(context.exception),
                    "Codex stable protocol notification validation failed",
                )

    def test_initialized_is_the_only_outgoing_notification(self) -> None:
        self.assertEqual(
            self.validator.validate_client_notification("initialized", None),
            {"method": "initialized"},
        )
        for method, params in (("initialized", {"unexpected": True}), ("future/event", {})):
            with self.subTest(method=method):
                with self.assertRaises(CodexSchemaValidationError):
                    self.validator.validate_client_notification(method, params)

    def test_server_request_envelope_is_validated_before_typed_denial(self) -> None:
        known = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "itemId": "item-1",
                "startedAtMs": 1,
                "threadId": "thread-1",
                "turnId": "turn-1",
            },
        }
        self.assertTrue(self.validator.validate_server_request(known))

        # A future request with a well-typed correlation envelope is still
        # answerable only with the fixed denial; its params are never used.
        self.assertFalse(
            self.validator.validate_server_request(
                {
                    "jsonrpc": "2.0",
                    "id": "opaque-id",
                    "method": "future/request",
                    "params": {"raw": "/secret/path"},
                }
            )
        )

        malformed = dict(known)
        malformed["params"] = {"threadId": "thread-1"}
        with self.assertRaises(CodexSchemaValidationError) as context:
            self.validator.validate_server_request(malformed)
        self.assertEqual(
            str(context.exception),
            "Codex stable protocol server request validation failed",
        )

    def test_server_notification_requires_generated_envelope_and_business_allowlist(self) -> None:
        valid_notifications = (
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "items": [], "status": "completed"},
                },
            },
            {
                "method": "remoteControl/status/changed",
                "emittedAtMs": 1,
                "params": {
                    "status": "disabled",
                    "serverName": "fake-server",
                    "installationId": "fake-installation",
                    "environmentId": None,
                },
            },
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-1",
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            },
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-2",
                    "success": False,
                    "error": "provider failure",
                    "onboardingEntrypoint": None,
                },
            },
            {"method": "skills/changed", "params": {}},
            {"method": "thread/goal/cleared", "params": {"threadId": "thread-1"}},
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "thread-1",
                    "threadSettings": {
                        "approvalPolicy": "never",
                        "approvalsReviewer": "user",
                        "collaborationMode": {
                            "mode": "default",
                            "settings": {"model": "gpt-5.4-mini"},
                        },
                        "cwd": "/workspace",
                        "model": "gpt-5.4-mini",
                        "modelProvider": "openai",
                        "sandboxPolicy": {"type": "readOnly"},
                    },
                },
            },
        )
        for valid in valid_notifications:
            with self.subTest(method=valid["method"], success=valid["params"].get("success")):
                self.assertEqual(
                    self.validator.validate_server_notification(valid),
                    valid["method"],
                )

        malformed = {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"status": "completed"}},
        }
        unknown_but_generated = {"method": "app/list/updated", "params": {"data": []}}
        invented_login_failure = {
            "method": "account/login/failed",
            "params": {"loginId": "login-1", "success": False},
        }
        malformed_login_completion = {
            "method": "account/login/completed",
            "params": {"loginId": "login-1", "success": "false"},
        }
        malformed_inert_bootstrap = {
            "method": "remoteControl/status/changed",
            "params": {
                "status": "not-a-generated-status",
                "serverName": "fake-server",
                "installationId": "fake-installation",
                "environmentId": None,
            },
        }
        unrelated_process_spawn_terminal = {
            "method": "process/exited",
            "params": {
                "exitCode": 0,
                "processHandle": "spawn-1",
                "stderr": "",
                "stderrCapReached": False,
                "stdout": "",
                "stdoutCapReached": False,
            },
        }
        self.assertTrue(
            self.validator._schemas.server_notification.is_valid(  # noqa: SLF001
                unrelated_process_spawn_terminal
            )
        )
        for message in (
            malformed,
            unknown_but_generated,
            invented_login_failure,
            malformed_login_completion,
            malformed_inert_bootstrap,
            unrelated_process_spawn_terminal,
        ):
            with self.subTest(method=message["method"]):
                with self.assertRaises(CodexSchemaValidationError) as context:
                    self.validator.validate_server_notification(message)
                self.assertEqual(
                    str(context.exception),
                    "Codex stable protocol notification validation failed",
                )


class AppServerRuntimeSchemaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Keep the one-time generated-tree provenance check outside an asyncio task;
        # individual tests still prove validation happens before process spawn.
        verify_checked_in_stable_schema()

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.server_path = root / "schema_test_server.py"
        self.server_path.write_text(textwrap.dedent(SCHEMA_TEST_SERVER), encoding="utf-8")
        self.audit_path = root / "audit.jsonl"
        self.client: AppServerClient | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.close()
        self.temp_dir.cleanup()

    def _client(self, mode: str) -> AppServerClient:
        self.client = AppServerClient(
            AppServerConfig(
                command=(sys.executable, "-u", str(self.server_path), mode, str(self.audit_path)),
                startup_timeout=2.0,
                request_timeout=2.0,
                shutdown_timeout=0.5,
                expected_cli_version=None,
            )
        )
        return self.client

    async def test_invalid_outgoing_request_and_notification_do_not_spawn_or_send(self) -> None:
        spawns = 0

        async def forbidden_factory(*_command: str):
            nonlocal spawns
            spawns += 1
            raise AssertionError("invalid protocol payload reached process creation")

        self.client = AppServerClient(
            AppServerConfig(expected_cli_version=None),
            process_factory=forbidden_factory,
        )
        with self.assertRaises(CodexSchemaValidationError):
            await self.client.request(
                "turn/start",
                {
                    "threadId": "thread-1",
                    "input": [{"type": "text", "text": "hello"}],
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                },
            )
        with self.assertRaises(CodexSchemaValidationError):
            await self.client.notify("future/notification")
        for method, params in (
            (
                "thread/resume",
                {
                    "threadId": "t" * 513,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                },
            ),
            ("account/login/cancel", {"loginId": "l" * 257}),
        ):
            with self.subTest(method=method):
                with self.assertRaises(CodexSchemaValidationError):
                    await self.client.request(method, params)
        self.assertEqual(spawns, 0)
        self.assertFalse(self.client.started)

    async def test_oversized_auth_response_isolates_before_login_state_exists(self) -> None:
        client = self._client("oversized-auth-response")
        from agents.codex.auth import CodexAuthService

        auth = CodexAuthService(client)
        termination_entered = asyncio.Event()
        allow_termination = asyncio.Event()
        original_terminate = client._terminate_process  # noqa: SLF001

        async def gated_terminate(process, *, generation=None):
            termination_entered.set()
            await allow_termination.wait()
            return await original_terminate(process, generation=generation)

        client._terminate_process = gated_terminate  # type: ignore[method-assign]  # noqa: SLF001
        login_task = asyncio.create_task(auth.login_start())
        lock_waiter: asyncio.Task | None = None
        try:
            await asyncio.wait_for(termination_entered.wait(), timeout=2.0)
            # The response future has failed schema validation, but auth must
            # join the reader's exact-generation teardown before releasing its
            # operation lock and allowing a new request to spawn/write.
            await asyncio.sleep(0)
            self.assertFalse(login_task.done())
            lock_waiter = asyncio.create_task(auth._login_operation_lock.acquire())  # noqa: SLF001
            await asyncio.sleep(0)
            self.assertFalse(lock_waiter.done())

            allow_termination.set()
            with self.assertRaises(CodexAmbiguousRequestError) as context:
                await login_task
            self.assertIsInstance(context.exception.__cause__, CodexSchemaValidationError)
            await asyncio.wait_for(lock_waiter, timeout=1.0)
            auth._login_operation_lock.release()  # noqa: SLF001
            self.assertEqual(auth._states, {})  # noqa: SLF001
            self.assertEqual(client.last_error, "invalid protocol response")
            self.assertFalse(client.started)
        finally:
            allow_termination.set()
            if not login_task.done():
                login_task.cancel()
                await asyncio.gather(login_task, return_exceptions=True)
            if lock_waiter is not None and not lock_waiter.done():
                lock_waiter.cancel()
                await asyncio.gather(lock_waiter, return_exceptions=True)
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001
            await auth.close()

    async def test_oversized_generated_notification_isolates_before_broadcast(self) -> None:
        client = self._client("oversized-notification")
        queue = client.subscribe()
        try:
            await client.ensure_started()
            terminal = await asyncio.wait_for(queue.get(), timeout=2.0)
            self.assertEqual(terminal["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(client.last_error, "invalid protocol notification")
            self.assertFalse(client.started)
            self.assertTrue(queue.empty())
        finally:
            client.unsubscribe(queue)

    async def test_unclassifiable_duplicate_and_nonfinite_frames_isolate(self) -> None:
        modes = (
            "junk-envelope",
            "id-only-envelope",
            "result-only-envelope",
            "bool-response-id",
            "string-response-id",
            "unknown-response-id",
            "mixed-envelope",
            "duplicate-id",
            "duplicate-method",
            "duplicate-nested-id",
            "nonfinite-json",
        )
        for mode in modes:
            with self.subTest(mode=mode):
                client = self._client(mode)
                queue = client.subscribe()
                try:
                    await client.ensure_started()
                    terminal = await asyncio.wait_for(queue.get(), timeout=2.0)
                    self.assertEqual(terminal["method"], INTERNAL_APP_SERVER_EXITED)
                    self.assertFalse(client.started)
                    self.assertNotIn("turn-1", str(client.last_error))
                    self.assertNotIn("login-1", str(client.last_error))
                finally:
                    client.unsubscribe(queue)
                    await client.close()
                    self.client = None

    async def test_response_mismatch_uses_pending_method_and_isolates(self) -> None:
        client = self._client("response-mismatch")
        await client.ensure_started()
        with self.assertRaises(CodexAmbiguousRequestError) as context:
            await client.request("account/read", {"refreshToken": False})
        self.assertEqual(
            str(context.exception),
            "codex app-server request outcome is ambiguous",
        )
        self.assertIsInstance(context.exception.__cause__, CodexSchemaValidationError)
        self.assertNotIn("secret", str(context.exception))
        self.assertNotIn("path", str(context.exception).lower())
        for _ in range(100):
            if client.last_error is not None and not client.started:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(client.last_error, "invalid protocol response")
        self.assertFalse(client.started)

    async def test_auth_bootstrap_status_is_generated_valid_then_dropped(self) -> None:
        client = self._client("auth-bootstrap-notification")
        queue = client.subscribe()
        try:
            account = await client.request("account/read", {"refreshToken": False})
            login = await client.request(
                "account/login/start",
                {"type": "chatgpt", "appBrand": "chatgpt"},
            )
            login_id = login.get("loginId")
            self.assertIsInstance(login_id, str)
            await client.request("account/login/cancel", {"loginId": login_id})

            self.assertIsNone(account.get("account"))
            self.assertTrue(account.get("requiresOpenaiAuth"))
            self.assertTrue(client.started)
            self.assertIsNone(client.last_error)
            self.assertTrue(queue.empty())
        finally:
            client.unsubscribe(queue)

    async def test_unknown_generated_notification_fails_closed_and_isolates(self) -> None:
        client = self._client("unknown-notification")
        queue = client.subscribe()
        try:
            await client.ensure_started()
            terminal = await asyncio.wait_for(queue.get(), timeout=2.0)
            self.assertEqual(terminal["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(client.last_error, "invalid protocol notification")
            self.assertFalse(client.started)
        finally:
            client.unsubscribe(queue)

    async def test_malformed_notification_fails_closed_and_isolates(self) -> None:
        client = self._client("malformed-notification")
        queue = client.subscribe()
        try:
            await client.ensure_started()
            terminal = await asyncio.wait_for(queue.get(), timeout=2.0)
            self.assertEqual(terminal["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(client.last_error, "invalid protocol notification")
            self.assertFalse(client.started)
        finally:
            client.unsubscribe(queue)

    async def test_wire_process_exited_isolates_before_mapping_release(self) -> None:
        """A process/spawn terminal is never app-server lifecycle authority."""

        client = self._client("wire-process-exited")
        mapping_path = Path(self.temp_dir.name) / "thread-map.json"
        store = ThreadMappingStore(mapping_path)
        manager = ThreadManager(client, store)

        class EnabledFakeAgent(CodexAgentService):
            _TURN_EXECUTION_ENABLED = True

        agent = EnabledFakeAgent(client, manager, turn_timeout=2.0)
        session_id = "session-wire-process-exit"
        thread_id = "thread-wire-process-exit"
        await store.set(session_id, thread_id, durable=True)
        state = await agent.reserve_turn(session_id, "execution-wire-process-exit")
        await agent._bind_turn_ids(state, thread_id, "turn-wire-process-exit")  # noqa: SLF001
        state.mapping_committed = True

        termination_entered = asyncio.Event()
        allow_termination = asyncio.Event()
        original_terminate = client._terminate_process  # noqa: SLF001

        async def gated_terminate(process, *, generation=None):
            termination_entered.set()
            await allow_termination.wait()
            return await original_terminate(process, generation=generation)

        client._terminate_process = gated_terminate  # type: ignore[method-assign]  # noqa: SLF001
        try:
            await client.ensure_started()
            await asyncio.wait_for(termination_entered.wait(), timeout=2.0)

            # The generated-valid wire notification has failed the business
            # allowlist, but it cannot masquerade as app-server death.  Until
            # the owned process group is verified gone, the durable mapping and
            # active state remain authoritative and no terminal is published.
            self.assertIsNotNone(store.entry(session_id))
            self.assertIsNone(state.terminal)
            self.assertFalse(client._exit_seen)  # noqa: SLF001

            allow_termination.set()
            for _ in range(200):
                if state.terminal is not None and store.entry(session_id) is None:
                    break
                await asyncio.sleep(0.01)

            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "process_exit")
            self.assertEqual(state.terminal.data["source"], INTERNAL_APP_SERVER_EXITED)
            self.assertIsNone(store.entry(session_id))
            self.assertEqual(client.last_error, "invalid protocol notification")
            self.assertFalse(client.started)
        finally:
            allow_termination.set()
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001
            await agent.close()

    async def test_unknown_request_receives_typed_denial_without_raw_reflection(self) -> None:
        client = self._client("unknown-request")
        queue = client.subscribe()
        try:
            await client.ensure_started()
            denied = await asyncio.wait_for(queue.get(), timeout=2.0)
            self.assertEqual(denied["method"], "server/request/denied")
            self.assertEqual(denied["params"]["method"], "unknown")
            self.assertRegex(
                denied["params"]["requestId"],
                r"\Aserver-request-[0-9a-f]{16}\Z",
            )
            self.assertNotIn("secret", json.dumps(denied))
            self.assertTrue(client.started)

            for _ in range(100):
                if self.audit_path.exists() and '"code": -32001' in self.audit_path.read_text(
                    encoding="utf-8"
                ):
                    break
                await asyncio.sleep(0.01)
            audit = self.audit_path.read_text(encoding="utf-8")
            self.assertIn('"code": -32001', audit)
            self.assertIn('"id": "opaque-secret-request-id"', audit)
        finally:
            client.unsubscribe(queue)


class RealPinnedAuthHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_account_read_and_login_start_emit_only_required_methods(self) -> None:
        """Probe only method names; never retain or render account/auth payloads."""

        binary = shutil.which("codex")
        if binary is None:
            self.skipTest("pinned Codex CLI is not installed")
        version = await asyncio.to_thread(
            subprocess.run,
            [binary, "--version"],
            env=scrubbed_child_environment(),
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if (
            version.returncode != 0
            or version.stdout != f"codex-cli {EXPECTED_CLI_VERSION}\n"
            or version.stderr
        ):
            self.skipTest("installed Codex CLI is not the exact pinned build")

        observed_methods: list[str] = []

        class MethodOnlyValidator(StableProtocolValidator):
            def validate_server_notification(self, message):
                method = super().validate_server_notification(message)
                observed_methods.append(method)
                return method

        client = AppServerClient(
            AppServerConfig(
                command=(binary, "app-server", "--stdio"),
                startup_timeout=10.0,
                request_timeout=10.0,
                shutdown_timeout=2.0,
                expected_cli_version=EXPECTED_CLI_VERSION,
            )
        )
        client._schema_validator = MethodOnlyValidator()  # noqa: SLF001
        try:
            account = await client.request("account/read", {"refreshToken": False})
            if not isinstance(account, dict):
                self.fail("real account/read did not return an object")
            login = await client.request(
                "account/login/start",
                {"type": "chatgpt", "appBrand": "chatgpt"},
            )
            login_id = login.get("loginId")
            if not isinstance(login_id, str) or not login_id:
                self.fail("real account/login/start did not return a bounded login id")
            await client.request("account/login/cancel", {"loginId": login_id})
        finally:
            await client.close()

        required = set(
            STABLE_PROTOCOL_MANIFEST["requiredWire"]["serverNotifications"]
        )
        self.assertIn("remoteControl/status/changed", observed_methods)
        self.assertTrue(set(observed_methods).issubset(required))

    async def test_real_empty_thread_resume_rebuilds_only_exact_missing_rollout(self) -> None:
        """Exercise the pinned no-rollout grammar without retaining ids."""

        binary = shutil.which("codex")
        if binary is None:
            self.skipTest("pinned Codex CLI is not installed")
        version = await asyncio.to_thread(
            subprocess.run,
            [binary, "--version"],
            env=scrubbed_child_environment(),
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if (
            version.returncode != 0
            or version.stdout != f"codex-cli {EXPECTED_CLI_VERSION}\n"
            or version.stderr
        ):
            self.skipTest("installed Codex CLI is not the exact pinned build")

        class ThreadStartProbeValidator(StableProtocolValidator):
            def validate_server_notification(self, message):
                if message.get("method") != "thread/started":
                    return super().validate_server_notification(message)
                params = message.get("params")
                thread = params.get("thread") if isinstance(params, dict) else None
                thread_id = thread.get("id") if isinstance(thread, dict) else None
                if (
                    not self._schemas.server_notification.is_valid(message)  # noqa: SLF001
                    or not isinstance(thread_id, str)
                    or not 0 < len(thread_id) <= 512
                ):
                    raise CodexSchemaValidationError(
                        "Codex stable protocol notification validation failed",
                        code="codex_schema_notification_invalid",
                    )
                # Test-only method observation: production's business
                # notification allowlist remains unchanged and closed.
                return "thread/started"

        previous_codex_home = os.environ.get("CODEX_HOME")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            os.environ["CODEX_HOME"] = str(codex_home)
            if previous_codex_home is None:
                self.addCleanup(os.environ.pop, "CODEX_HOME", None)
            else:
                self.addCleanup(
                    os.environ.__setitem__,
                    "CODEX_HOME",
                    previous_codex_home,
                )
            store = ThreadMappingStore(root / "mapping.json")

            def new_client() -> AppServerClient:
                client = AppServerClient(
                    AppServerConfig(
                        command=(binary, "app-server", "--stdio"),
                        startup_timeout=10.0,
                        request_timeout=10.0,
                        shutdown_timeout=2.0,
                        expected_cli_version=EXPECTED_CLI_VERSION,
                    )
                )
                client._schema_validator = ThreadStartProbeValidator()  # noqa: SLF001
                return client

            first_client = new_client()
            try:
                first_manager = ThreadManager(first_client, store)
                first_thread = await first_manager.ensure_thread(
                    "real-empty-thread",
                    cwd=str(workspace),
                )
            finally:
                await first_client.close()

            await store.set(
                "real-empty-thread",
                first_thread,
                cwd=workspace,
            )
            observed_methods: list[str] = []
            second_client = new_client()
            original_request = second_client.request

            async def method_only_request(method, params=None, **kwargs):
                observed_methods.append(method)
                return await original_request(method, params, **kwargs)

            second_client.request = method_only_request  # type: ignore[method-assign]
            try:
                second_manager = ThreadManager(second_client, store)
                rebuilt_thread = await second_manager.ensure_thread(
                    "real-empty-thread",
                    cwd=str(workspace),
                )
            finally:
                await second_client.close()
                if previous_codex_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous_codex_home

            self.assertNotEqual(rebuilt_thread, first_thread)
            self.assertEqual(
                [
                    method
                    for method in observed_methods
                    if method in {"thread/resume", "thread/start"}
                ],
                ["thread/resume", "thread/start"],
            )
            self.assertNotIn("real-empty-thread", store.read())


if __name__ == "__main__":
    unittest.main()
