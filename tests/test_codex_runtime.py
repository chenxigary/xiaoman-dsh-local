"""Fake app-server contract tests for the direct Codex bridge boundary."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agents.codex import (
    AgentEvent,
    AgentEventType,
    AppServerClient,
    AppServerConfig,
    CodexAgentService,
    CodexAuthService,
    CodexCompatibilityError,
    CodexError,
    CodexProcessError,
    LoginStatus,
    ThreadManager,
    ThreadMappingStore,
)
from agents.codex.app_server_client import (
    INTERNAL_APP_SERVER_EXITED,
    INTERNAL_APP_SERVER_ISOLATION_FAILED,
    CodexAmbiguousRequestError,
    JsonRpcError,
    scrubbed_child_environment,
)
from agents.codex.schema_validator import CodexSchemaValidationError
from agents.codex.compatibility import ProtocolCompatibilityGate
from agents.codex.event_mapper import map_notification
from agents.codex.thread_manager import is_missing_rollout_error


FAKE_SERVER = r'''
import json
import os
import sys

audit_path = sys.argv[1] if len(sys.argv) > 1 else None
server_mode = sys.argv[2] if len(sys.argv) > 2 else ""
threads = 0
turns = 0
active = {}
ignore_interrupts = set()

def audit(value):
    if not audit_path:
        return
    with open(audit_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def result(message, value):
    send({"jsonrpc": "2.0", "id": message.get("id"), "result": value})

def error(message, code, text):
    send({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}})

def notification(method, params, emitted_at_ms=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if emitted_at_ms is not None:
        message["emittedAtMs"] = emitted_at_ms
    send(message)

def turn(turn_id, status):
    return {"id": turn_id, "items": [], "status": status}

def thread_response(thread_id):
    return {
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": "/tmp/fake-workspace",
        "model": "gpt-5",
        "modelProvider": "openai",
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "thread": {
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
        },
    }

for raw in sys.stdin:
    try:
        message = json.loads(raw)
    except Exception:
        continue
    audit(message)
    if "jsonrpc" in message:
        error(message, -32600, "jsonrpc envelope is not accepted")
        continue
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        result(message, {"codexHome": "/tmp/codex-home", "platformFamily": "unix", "platformOs": "macos", "userAgent": "fake"})
    elif method == "initialized":
        pass
    elif method == "account/read":
        if params != {"refreshToken": False}:
            error(message, -32602, "refreshToken must be explicitly false")
            continue
        # The pinned real app-server emits this generated-stable bootstrap
        # status before account/read's response. Runtime validates it fully
        # and deliberately drops it without exposing remote identity fields.
        notification("remoteControl/status/changed", {
            "status": "disabled",
            "serverName": "fake-server",
            "installationId": "fake-installation",
            "environmentId": None,
        }, emitted_at_ms=1)
        result(message, {"account": {"type": "chatgpt", "email": "fake@example.test", "planType": "pro", "accessToken": "MUST_NOT_LEAK"}, "requiresOpenaiAuth": True})
    elif method == "account/login/start":
        if params != {"type": "chatgpt", "appBrand": "chatgpt"}:
            error(message, -32602, "invalid ChatGPT login params")
            continue
        if server_mode == "lost-login-start":
            continue
        if server_mode == "eof-login-start":
            sys.exit(7)
        result(message, {"type": "chatgpt", "authUrl": "https://auth.openai.com/oauth/authorize?client_id=fake", "loginId": "login-1"})
    elif method == "account/login/cancel":
        if server_mode == "lost-login-cancel":
            continue
        if server_mode == "eof-login-cancel":
            sys.exit(7)
        result(message, {"status": "canceled"})
    elif method == "thread/start":
        if (
            params.get("ephemeral") is not False
            or params.get("sandbox") != "read-only"
            or params.get("approvalPolicy") != "never"
            or "excludeTurns" in params
        ):
            error(message, -32602, "invalid stable thread/start params")
            continue
        threads += 1
        result(message, thread_response("thread-%d" % threads))
    elif method == "thread/resume":
        if "excludeTurns" in params or params.get("sandbox") != "read-only" or params.get("approvalPolicy") != "never":
            error(message, -32602, "unknown or unsafe stable thread/resume params")
            continue
        result(message, thread_response(params.get("threadId")))
    elif method == "turn/start":
        turns += 1
        turn_id = "turn-%d" % turns
        thread_id = params.get("threadId")
        text = (((params.get("input") or [{}])[0]).get("text") or "")
        if text == "reject-start":
            # A valid JSON-RPC error is an explicit no-dispatch authority.
            error(message, -32602, "turn rejected")
            continue
        active[turn_id] = thread_id
        if text == "lost-start-response":
            # The turn exists and can emit events, but its request result is
            # lost. A timeout must therefore isolate the process before local
            # correlation/mapping state is released.
            notification("turn/started", {"threadId": thread_id, "turn": turn(turn_id, "inProgress")})
            continue
        if text == "eof-start-response":
            sys.exit(7)
        if text == "malformed-start-response":
            result(message, {})
            continue
        result(message, {"turn": turn(turn_id, "inProgress")})
        notification("turn/started", {"threadId": thread_id, "turn": turn(turn_id, "inProgress")})
        if text in ("wait", "ignore-interrupt"):
            if text == "ignore-interrupt":
                ignore_interrupts.add(turn_id)
            continue
        if text == "exit":
            sys.exit(7)
        if text == "malformed":
            sys.stdout.write("not-json stdout\n")
            sys.stdout.flush()
            continue
        if text == "queue-flood":
            # A deliberately unconsumed subscriber in the test fills its
            # bounded critical lane. The AppServerClient must isolate the
            # real process group before publishing a process terminal.
            for index in range(12):
                notification(
                    "item/completed",
                    {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": "flood-item-%d" % index,
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "status": "completed",
                            "text": "flood-%d" % index,
                        },
                        "completedAtMs": index + 1,
                    },
                )
            continue
        item = {"id": "item-%d" % turns, "type": "agentMessage", "status": "inProgress", "text": ""}
        if text == "hello":
            item["phase"] = "final_answer"
            item["text"] = "hello world"
        elif text == "commentary":
            item["phase"] = "commentary"
            item["text"] = "progress-only"
        notification("item/started", {"threadId": thread_id, "turnId": turn_id, "item": item, "startedAtMs": 1})
        if text == "approval":
            send({"jsonrpc": "2.0", "id": 900, "method": "some/new/request", "params": {"threadId": thread_id, "turnId": turn_id, "question": "do not echo"}})
            continue
        if text == "unknown":
            notification("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item["id"], "delta": "buffered-final"})
            item["phase"] = "final_answer"
            item["text"] = "buffered-final"
        elif text == "commentary":
            notification("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item["id"], "delta": "progress-only"})
        else:
            notification("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item["id"], "delta": "hello "})
            notification("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item["id"], "delta": "world"})
        item["status"] = "completed"
        notification("item/completed", {"threadId": thread_id, "turnId": turn_id, "item": item, "completedAtMs": 2})
        notification("turn/completed", {"threadId": thread_id, "turn": turn(turn_id, "completed")})
        active.pop(turn_id, None)
    elif method == "turn/interrupt":
        result(message, {})
        turn_id = params.get("turnId")
        thread_id = params.get("threadId")
        if turn_id in active:
            if turn_id in ignore_interrupts:
                continue
            notification("turn/completed", {"threadId": thread_id, "turn": turn(turn_id, "interrupted")})
            active.pop(turn_id, None)
    elif message.get("id") == 900 and "error" in message:
        # The bridge denied the unknown server request.  Only then complete the
        # turn, proving that the denial did not silently authorize the action.
        thread_id = active.get("turn-1") or "thread-1"
        notification("item/agentMessage/delta", {"threadId": thread_id, "turnId": "turn-1", "itemId": "msg-1", "delta": "after-deny"})
        notification("turn/completed", {"threadId": thread_id, "turn": turn("turn-1", "completed")})
        active.pop("turn-1", None)
'''


CHILD_SERVER = r'''
import json
import os
import signal
import subprocess
import sys
import time

pid_path = sys.argv[1]
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
])
with open(pid_path, "w", encoding="utf-8") as stream:
    stream.write(str(child.pid))
    stream.flush()

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": message.get("id"), "result": {"codexHome": "/tmp/codex-home", "platformFamily": "unix", "platformOs": "macos", "userAgent": "fake"}})
    elif message.get("method") == "initialized":
        pass
'''


class CodexRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.server_path = root / "fake_app_server.py"
        self.server_path.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")
        self.audit_path = root / "audit.jsonl"
        self.mapping_path = root / "thread-map.json"
        self._old_audit = os.environ.get("FAKE_CODEX_AUDIT")
        os.environ["FAKE_CODEX_AUDIT"] = str(self.audit_path)
        self.client: AppServerClient | None = None
        self.auth: CodexAuthService | None = None
        self.agent: CodexAgentService | None = None

    async def asyncTearDown(self) -> None:
        if self.auth is not None:
            await self.auth.close()
        if self.agent is not None:
            await self.agent.close()
        if self.client is not None:
            await self.client.close()
        if self._old_audit is None:
            os.environ.pop("FAKE_CODEX_AUDIT", None)
        else:
            os.environ["FAKE_CODEX_AUDIT"] = self._old_audit
        self.temp_dir.cleanup()

    def make_stack(
        self,
        *,
        queue_size: int = 16,
        request_timeout: float = 2.0,
        event_queue_size: int | None = None,
    ):
        config = AppServerConfig(
            command=(sys.executable, "-u", str(self.server_path), str(self.audit_path)),
            startup_timeout=2.0,
            request_timeout=request_timeout,
            shutdown_timeout=1.0,
            subscriber_queue_size=queue_size,
            expected_cli_version=None,
        )
        self.client = AppServerClient(config)
        manager = ThreadManager(self.client, ThreadMappingStore(self.mapping_path))
        class EnabledFakeServerAgent(CodexAgentService):
            _TURN_EXECUTION_ENABLED = True

        self.agent = EnabledFakeServerAgent(
            self.client,
            manager,
            turn_timeout=2.0,
            event_queue_size=queue_size if event_queue_size is None else event_queue_size,
        )
        return self.client, self.agent

    def make_auth_stack(self, server_mode: str) -> tuple[AppServerClient, CodexAuthService]:
        self.client = AppServerClient(
            AppServerConfig(
                command=(
                    sys.executable,
                    "-u",
                    str(self.server_path),
                    str(self.audit_path),
                    server_mode,
                ),
                startup_timeout=2.0,
                request_timeout=0.05,
                shutdown_timeout=1.0,
                subscriber_queue_size=16,
                expected_cli_version=None,
            )
        )
        self.auth = CodexAuthService(self.client)
        return self.client, self.auth

    async def test_child_environment_is_default_deny_and_keeps_auth_home(self) -> None:
        sentinel = {
            "HOME": "/tmp/codex-home-sentinel",
            "CODEX_HOME": "/tmp/codex-home-sentinel/.codex",
            "PATH": "/usr/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "OPENAI_API_KEY": "must-not-be-read",
            "AWS_PROFILE": "must-not-be-forwarded",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GIT_ASKPASS": "/tmp/askpass",
            "KUBECONFIG": "/tmp/kubeconfig",
            "DOCKER_CONFIG": "/tmp/docker",
            "HTTP_PROXY": "http://proxy.invalid",
        }
        previous = {name: os.environ.get(name) for name in sentinel}
        try:
            for name, value in sentinel.items():
                os.environ[name] = value
            environment = scrubbed_child_environment()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(environment.get("HOME"), sentinel["HOME"])
        self.assertEqual(environment.get("CODEX_HOME"), sentinel["CODEX_HOME"])
        self.assertEqual(environment.get("PATH"), sentinel["PATH"])
        self.assertEqual(environment.get("LC_ALL"), sentinel["LC_ALL"])
        for name in ("OPENAI_API_KEY", "AWS_PROFILE", "SSH_AUTH_SOCK", "GIT_ASKPASS", "KUBECONFIG", "DOCKER_CONFIG", "HTTP_PROXY"):
            self.assertNotIn(name, environment)

    async def test_provider_public_turn_reservation_is_disabled_in_local_only_build(self) -> None:
        client = AppServerClient(AppServerConfig(expected_cli_version=None))
        manager = ThreadManager(client, ThreadMappingStore(self.mapping_path))
        agent = CodexAgentService(client, manager)
        with self.assertRaisesRegex(CodexError, "unavailable"):
            await agent.reserve_turn("disabled-session", "disabled-execution")
        self.assertFalse(client.started)
        await agent.close()

    async def test_runtime_config_rejects_nonfinite_negative_and_huge_values_before_spawn(self) -> None:
        invalid_app_server = {
            "startup_timeout": (float("nan"), float("inf"), -1.0, 121.0),
            "request_timeout": (float("nan"), float("inf"), 0.0, 301.0),
            "shutdown_timeout": (float("nan"), float("inf"), -0.1, 31.0),
            "subscriber_queue_size": (True, 0, -1, 4097),
        }
        for field, values in invalid_app_server.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        AppServerConfig(**{field: value})

        spawns = 0

        async def forbidden_factory(*_command):
            nonlocal spawns
            spawns += 1
            raise AssertionError("invalid provider config reached spawn")

        client = AppServerClient(
            AppServerConfig(expected_cli_version=None),
            process_factory=forbidden_factory,
        )
        manager = ThreadManager(client, ThreadMappingStore(self.mapping_path))
        for field, values in {
            "turn_timeout": (float("nan"), float("inf"), 0.0, 7201.0),
            "event_queue_size": (True, 0, 4097),
            "finished_cache_size": (True, 0, 4097),
        }.items():
            for value in values:
                with self.subTest(provider_field=field, value=value):
                    with self.assertRaises(ValueError):
                        CodexAgentService(client, manager, **{field: value})
        self.assertEqual(spawns, 0)
        self.assertFalse(client.started)

    async def test_per_turn_queue_overflow_fails_closed_instead_of_dropping_delta(self) -> None:
        _client, agent = self.make_stack(queue_size=1)
        state = await agent.reserve_turn("session-queue-overflow", "execution-queue-overflow")
        self.assertEqual(
            await agent.wait_for_execution_release(
                "session-queue-overflow",
                "execution-queue-overflow",
            ),
            "pending",
        )
        self.assertEqual(
            await agent.wait_for_execution_release(
                "session-queue-overflow",
                "execution-unknown",
            ),
            "unknown",
        )
        state.push(AgentEvent(AgentEventType.STARTED, session_id=state.session_id, correlation_id=state.correlation_id))
        with self.assertRaises(CodexError) as context:
            state.push(
                AgentEvent(
                    AgentEventType.TEXT_DELTA,
                    session_id=state.session_id,
                    correlation_id=state.correlation_id,
                    text="middle delta",
                    speakable=True,
                    phase="final_answer",
                )
            )
        self.assertEqual(context.exception.code, "invalid_response")
        self.assertIsNone(state.terminal)

    async def test_handshake_normal_turn_and_secret_free_mapping(self) -> None:
        client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-1", "hello", correlation_id="corr-1")]
        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.STARTED,
                AgentEventType.TOOL_ACTIVITY,
                AgentEventType.TEXT_DELTA,
                AgentEventType.TEXT_DELTA,
                AgentEventType.TOOL_ACTIVITY,
                AgentEventType.FINISHED,
            ],
        )
        self.assertEqual(events[-1].text, "hello world")
        self.assertEqual(events[-1].correlation_id, "corr-1")
        self.assertNotIn("jsonrpc", events[-1].to_dict())
        self.assertNotIn("accessToken", self.mapping_path.read_text(encoding="utf-8"))
        messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all("jsonrpc" not in item for item in messages))
        initialize = next(item for item in messages if item.get("method") == "initialize")
        self.assertFalse(initialize["params"]["capabilities"]["experimentalApi"])
        self.assertFalse(initialize["params"]["capabilities"]["requestAttestation"])
        turn_start = next(item for item in messages if item.get("method") == "turn/start")
        self.assertEqual(turn_start["params"]["model"], "gpt-5.4-mini")
        self.assertEqual(turn_start["params"]["effort"], "low")
        self.assertIsNone(turn_start["params"]["serviceTier"])
        self.assertTrue(any(item.get("method") == "initialized" for item in messages))
        self.assertEqual((await client.health())["pending_requests"], 0)
        self.assertEqual(
            await agent.wait_for_execution_release("session-1", "corr-1"),
            "released",
        )

    async def test_protocol_gate_rejects_unpinned_server_and_cleans_process(self) -> None:
        config = AppServerConfig(
            command=(sys.executable, "-u", str(self.server_path), str(self.audit_path)),
            startup_timeout=2.0,
            request_timeout=2.0,
            shutdown_timeout=1.0,
            expected_cli_version="0.149.0-alpha.4.1",
        )
        client = AppServerClient(config)
        with self.assertRaises(CodexCompatibilityError):
            await client.ensure_started()
        self.assertFalse(client.started)
        await client.close()

    async def test_protocol_gate_requires_exact_initialized_user_agent_grammar(self) -> None:
        gate = ProtocolCompatibilityGate("0.149.0-alpha.4.1")
        valid = (
            "xiaoman-dsh/0.149.0-alpha.4.1 "
            "(Mac OS 15.6.1; aarch64) unknown "
            "(xiaoman-dsh; 0.1.0)"
        )
        self.assertEqual(gate.validate_initialize({"userAgent": valid}).cli_version, "0.149.0-alpha.4.1")

        invalid = (
            "codex-cli 0.149.0-alpha.4.1",
            "prefix " + valid,
            valid + " suffix",
            valid.replace("/0.149.0-alpha.4.1 ", "/0.148.0-alpha.8 ", 1),
            valid.replace("xiaoman-dsh/", "codex_cli_rs/", 1),
            valid.replace("(xiaoman-dsh; 0.1.0)", "(other-client; 0.1.0)"),
            valid.replace(" unknown ", " unknown\n"),
            valid.replace(" unknown ", " 终端 "),
            valid + ("x" * 512),
        )
        for user_agent in invalid:
            with self.subTest(user_agent=user_agent):
                with self.assertRaises(CodexCompatibilityError) as context:
                    gate.validate_initialize({"userAgent": user_agent})
                self.assertEqual(context.exception.code, "codex_version_unsupported")

    async def test_close_kills_descendant_in_process_group_after_parent_exit(self) -> None:
        root = Path(self.temp_dir.name)
        child_server = root / "child_app_server.py"
        child_server.write_text(textwrap.dedent(CHILD_SERVER), encoding="utf-8")
        child_pid_path = root / "child.pid"
        client = AppServerClient(
            AppServerConfig(
                command=(sys.executable, "-u", str(child_server), str(child_pid_path)),
                startup_timeout=2.0,
                request_timeout=1.0,
                shutdown_timeout=0.1,
                expected_cli_version=None,
            )
        )
        try:
            await client.ensure_started()
            for _ in range(100):
                if child_pid_path.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            await client.close()
            gone = False
            for _ in range(150):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    gone = True
                    break
                except OSError:
                    gone = True
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(gone)
        finally:
            await client.close()

    async def test_stderr_overlong_unterminated_record_is_continuously_drained(self) -> None:
        root = Path(self.temp_dir.name)
        stderr_server = root / "stderr_flood_app_server.py"
        stderr_server.write_text(
            textwrap.dedent(
                r'''
                import json
                import os
                import sys

                remaining = memoryview(b"x" * (256 * 1024))
                while remaining:
                    remaining = remaining[os.write(2, remaining):]

                for raw in sys.stdin:
                    message = json.loads(raw)
                    if message.get("method") == "initialize":
                        response = {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "result": {
                                "codexHome": "/tmp/codex-home",
                                "platformFamily": "unix",
                                "platformOs": "macos",
                                "userAgent": "fake",
                            },
                        }
                        sys.stdout.write(json.dumps(response) + "\n")
                        sys.stdout.flush()
                '''
            ),
            encoding="utf-8",
        )
        client = AppServerClient(
            AppServerConfig(
                command=(sys.executable, "-u", str(stderr_server)),
                startup_timeout=2.0,
                request_timeout=2.0,
                shutdown_timeout=1.0,
                expected_cli_version=None,
            )
        )
        try:
            await asyncio.wait_for(client.ensure_started(), timeout=3.0)
            self.assertTrue(client.started)
            self.assertIsNone(client.last_error)
        finally:
            await client.close()

    async def test_process_group_survival_is_reported_as_isolation_failure(self) -> None:
        client, _agent = self.make_stack()
        client.config = replace(client.config, shutdown_timeout=0.05)

        class ExitedParent:
            pid = 424242
            returncode = 0

            async def wait(self):
                return self.returncode

        signals: list[int] = []
        original_exists = client._process_group_exists  # noqa: SLF001
        original_signal = client._signal_process_group  # noqa: SLF001
        client._process_group_exists = lambda _pid: True  # type: ignore[method-assign]  # noqa: SLF001
        client._signal_process_group = lambda _process, sig: signals.append(sig)  # type: ignore[method-assign]  # noqa: SLF001
        try:
            terminated = await client._terminate_process(ExitedParent())  # type: ignore[arg-type]  # noqa: SLF001
            self.assertFalse(terminated)
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
            self.assertIsNone(client.last_error)
        finally:
            client._process_group_exists = original_exists  # type: ignore[method-assign]  # noqa: SLF001
            client._signal_process_group = original_signal  # type: ignore[method-assign]  # noqa: SLF001

    async def test_sigkill_wait_hang_is_bounded_and_never_publishes_exit(self) -> None:
        client, _agent = self.make_stack()
        client.config = replace(client.config, shutdown_timeout=0.08)
        queue = client.subscribe(maxsize=1)

        class NeverReapedParent:
            pid = 424245
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()

        process = NeverReapedParent()
        client._process = process  # type: ignore[assignment]  # noqa: SLF001
        client._process_generation = 1  # noqa: SLF001

        signals: list[int] = []
        original_exists = client._process_group_exists  # noqa: SLF001
        original_signal = client._signal_process_group  # noqa: SLF001
        client._process_group_exists = lambda _pid: True  # type: ignore[method-assign]  # noqa: SLF001
        client._signal_process_group = lambda _process, sig: signals.append(sig)  # type: ignore[method-assign]  # noqa: SLF001
        try:
            terminated = await asyncio.wait_for(
                client._terminate_and_mark_exit(  # noqa: SLF001
                    process,  # type: ignore[arg-type]
                    1,
                    "process exited",
                ),
                timeout=0.5,
            )
            self.assertFalse(terminated)
            self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
            message = queue.get_nowait()
            self.assertEqual(message["method"], INTERNAL_APP_SERVER_ISOLATION_FAILED)
            self.assertNotEqual(message["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(message["params"], {"reason": "isolation_failed"})
            self.assertEqual(client.last_error, "isolation_failed")
        finally:
            client._process_group_exists = original_exists  # type: ignore[method-assign]  # noqa: SLF001
            client._signal_process_group = original_signal  # type: ignore[method-assign]  # noqa: SLF001
            client._process = None  # noqa: SLF001
            client.unsubscribe(queue)

    async def test_process_group_permission_error_is_not_treated_as_gone(self) -> None:
        # Lack of authority is not evidence that a tool descendant exited.
        with patch("agents.codex.app_server_client.os.killpg", side_effect=PermissionError(1, "operation not permitted")):
            self.assertTrue(AppServerClient._process_group_exists(424244))  # noqa: SLF001

    async def test_failed_group_verification_never_emits_authoritative_process_exit(self) -> None:
        client, _agent = self.make_stack()
        queue = client.subscribe(maxsize=1)

        class ExitedParent:
            pid = 424243
            returncode = 0

        process = ExitedParent()
        client._process = process  # type: ignore[assignment]  # noqa: SLF001
        client._process_generation = 1  # noqa: SLF001

        original_terminate = client._terminate_process  # noqa: SLF001

        async def failed_terminate(_process, *, generation=None):
            _ = generation
            return False

        client._terminate_process = failed_terminate  # type: ignore[method-assign]  # noqa: SLF001
        try:
            terminated = await client._terminate_and_mark_exit(  # type: ignore[arg-type]  # noqa: SLF001
                process,
                1,
                "malformed stdout",
            )
            self.assertFalse(terminated)
            message = queue.get_nowait()
            self.assertEqual(message["method"], INTERNAL_APP_SERVER_ISOLATION_FAILED)
            self.assertNotEqual(message["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(message["params"], {"reason": "isolation_failed"})
        finally:
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001
            client._process = None  # noqa: SLF001
            client.unsubscribe(queue)

    async def test_eof_and_wait_join_one_generation_termination_outcome(self) -> None:
        client, _agent = self.make_stack()
        queue = client.subscribe(maxsize=2)

        class EOFStream:
            async def readline(self):
                return b""

        class ExitedProcess:
            pid = 424248
            returncode = 0
            stdout = EOFStream()

            async def wait(self):
                return self.returncode

        process = ExitedProcess()
        client._process = process  # type: ignore[assignment]  # noqa: SLF001
        client._process_generation = 7  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        original_terminate = client._terminate_process  # noqa: SLF001

        async def one_failed_attempt(_process, *, generation=None):
            nonlocal calls
            self.assertIs(_process, process)
            self.assertEqual(generation, 7)
            calls += 1
            entered.set()
            await release.wait()
            return False

        client._terminate_process = one_failed_attempt  # type: ignore[method-assign]  # noqa: SLF001
        reader = asyncio.create_task(
            client._reader_loop(process, 7)  # type: ignore[arg-type]  # noqa: SLF001
        )
        waiter = asyncio.create_task(
            client._wait_loop(process, 7)  # type: ignore[arg-type]  # noqa: SLF001
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            # Give both lifecycle callbacks a chance to join the shared task
            # before making the forced verification attempt complete.
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(reader, waiter), timeout=1.0)
            self.assertEqual(calls, 1)
            terminal = queue.get_nowait()
            self.assertEqual(terminal["method"], INTERNAL_APP_SERVER_ISOLATION_FAILED)
            self.assertTrue(queue.empty())
            self.assertEqual(client.last_error, "isolation_failed")
            # A late callback from the same lifecycle sees the sticky result;
            # it cannot start a contradictory second termination attempt.
            self.assertFalse(
                await client._terminate_and_mark_exit(  # type: ignore[arg-type]  # noqa: SLF001
                    process,
                    7,
                    "late waiter",
                )
            )
            self.assertEqual(calls, 1)
            self.assertTrue(queue.empty())
        finally:
            release.set()
            for task in (reader, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001
            client._process = None  # noqa: SLF001
            client.unsubscribe(queue)

    async def test_late_old_generation_callback_cannot_exit_new_process(self) -> None:
        client, _agent = self.make_stack()
        queue = client.subscribe(maxsize=2)

        class FakeProcess:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.returncode = 0

        old_process = FakeProcess(424246)
        new_process = FakeProcess(424247)
        client._process = old_process  # type: ignore[assignment]  # noqa: SLF001
        client._process_generation = 11  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        original_terminate = client._terminate_process  # noqa: SLF001

        async def delayed_old_terminate(process, *, generation=None):
            self.assertEqual(generation, 11)
            self.assertIs(process, old_process)
            entered.set()
            await release.wait()
            return True

        client._terminate_process = delayed_old_terminate  # type: ignore[method-assign]  # noqa: SLF001
        callback = asyncio.create_task(
            client._terminate_and_mark_exit(  # type: ignore[arg-type]  # noqa: SLF001
                old_process,
                11,
                "old process exited",
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            client._process = new_process  # type: ignore[assignment]  # noqa: SLF001
            client._process_generation = 12  # noqa: SLF001
            client._exit_seen = False  # noqa: SLF001
            client._last_error = None  # noqa: SLF001
            release.set()
            self.assertTrue(await asyncio.wait_for(callback, timeout=1.0))

            self.assertIs(client._process, new_process)  # noqa: SLF001
            self.assertFalse(client._exit_seen)  # noqa: SLF001
            self.assertIsNone(client.last_error)
            self.assertTrue(queue.empty())
        finally:
            release.set()
            if not callback.done():
                callback.cancel()
                await asyncio.gather(callback, return_exceptions=True)
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001
            client._process = None  # noqa: SLF001
            client.unsubscribe(queue)

    async def test_failed_isolation_poison_blocks_restart_until_reconciliation(self) -> None:
        client, _agent = self.make_stack()
        factory_calls = 0
        original_factory = client._process_factory

        async def counted_factory(*command):
            nonlocal factory_calls
            factory_calls += 1
            return await original_factory(*command)

        client._process_factory = counted_factory  # type: ignore[method-assign]
        client._isolation_failed = True  # noqa: SLF001 - forced process-group failure probe
        with self.assertRaises(CodexError) as context:
            await client.ensure_started()
        self.assertIn("reconciliation", str(context.exception))
        self.assertEqual(factory_calls, 0)

    async def test_resume_uses_persisted_thread_id(self) -> None:
        _client, agent = self.make_stack()
        first = [event async for event in agent.stream_turn("session-resume", "hello")]
        first_thread = first[-1].thread_id
        self.assertEqual(first_thread, "thread-1")
        await agent.close()
        await self.client.close()  # type: ignore[union-attr]
        self.agent = None
        self.client = None

        _client, agent = self.make_stack()
        second = [event async for event in agent.stream_turn("session-resume", "hello")]
        self.assertEqual(second[-1].thread_id, first_thread)
        messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(item.get("method") == "thread/resume" for item in messages))

    async def test_missing_rollout_classifier_binds_exact_requested_thread_id(self) -> None:
        expected = "thread-expected-no-rollout"
        self.assertTrue(
            is_missing_rollout_error(
                JsonRpcError(-32600, f"no rollout found for thread id {expected}"),
                expected,
            )
        )
        self.assertTrue(
            is_missing_rollout_error(
                JsonRpcError(-32600, f"no rollout found for thread id {expected}."),
                expected,
            )
        )
        for message in (
            "no rollout found",
            "rollout not found",
            f"No rollout found for thread id {expected}",
            "no rollout found for thread id thread-foreign",
            f"prefix no rollout found for thread id {expected}",
            f"no rollout found for thread id {expected} suffix",
            f"no rollout found for thread id {expected}!",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    is_missing_rollout_error(JsonRpcError(-32600, message), expected)
                )
        for code in (404, "404", "not_found", "thread_not_found", "no_rollout"):
            with self.subTest(generic_code=code):
                self.assertFalse(
                    is_missing_rollout_error(
                        JsonRpcError(code, "thread not found"),
                        expected,
                    )
                )

        store = ThreadMappingStore(self.mapping_path)
        await store.set("session-empty-rollout", expected)
        methods: list[str] = []

        class EmptyRolloutClient:
            async def request(self, method, params):
                methods.append(method)
                if method == "thread/resume":
                    raise JsonRpcError(
                        -32600,
                        f"no rollout found for thread id {params['threadId']}",
                    )
                if method == "thread/start":
                    return {"thread": {"id": "thread-rebuilt"}}
                raise AssertionError("unexpected method")

        manager = ThreadManager(EmptyRolloutClient(), store)  # type: ignore[arg-type]
        self.assertEqual(
            await manager.ensure_thread("session-empty-rollout"),
            "thread-rebuilt",
        )
        self.assertEqual(methods, ["thread/resume", "thread/start"])
        self.assertNotIn("session-empty-rollout", store.read())

    async def test_auth_url_status_cancel_and_allowlist(self) -> None:
        client, _agent = self.make_stack()
        self.auth = CodexAuthService(client)
        account = await self.auth.account_read()
        self.assertEqual(account["account"], {"type": "chatgpt", "planType": "pro"})
        self.assertNotIn("email", json.dumps(account))
        self.assertNotIn("accessToken", json.dumps(account))
        state = await self.auth.login_start()
        self.assertEqual(
            state.auth_url,
            "https://auth.openai.com/oauth/authorize?client_id=fake",
        )
        self.assertEqual((await self.auth.login_status("login-1")).status.value, "pending")
        canceled = await self.auth.login_cancel("login-1")
        self.assertEqual(canceled.status.value, "canceled")

    async def test_login_operation_retry_survives_abort_and_joins_one_request(self) -> None:
        class ControlledLoginClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.request_started = asyncio.Event()
                self.release_response = asyncio.Event()
                self.start_calls = 0

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, method, params):
                self.assert_request = (method, params)
                self.start_calls += 1
                self.request_started.set()
                await self.release_response.wait()
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=operation",
                    "loginId": "login-operation",
                }

        fake = ControlledLoginClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        operation_id = "11111111-1111-4111-8111-111111111111"
        foreign_id = "22222222-2222-4222-8222-222222222222"

        disconnected = asyncio.create_task(self.auth.login_start(operation_id))
        await fake.request_started.wait()
        disconnected.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await disconnected

        retry_one = asyncio.create_task(self.auth.login_start(operation_id))
        retry_two = asyncio.create_task(self.auth.login_start(operation_id))
        await asyncio.sleep(0)
        self.assertEqual(fake.start_calls, 1)
        with self.assertRaises(CodexError) as context:
            await self.auth.login_start(foreign_id)
        self.assertEqual(context.exception.code, "login_in_progress")

        fake.release_response.set()
        first, second = await asyncio.gather(retry_one, retry_two)
        self.assertEqual(first.login_id, "login-operation")
        self.assertEqual(second.login_id, first.login_id)
        self.assertEqual((await self.auth.login_start(operation_id)).login_id, first.login_id)
        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(
            fake.assert_request,
            ("account/login/start", {"type": "chatgpt", "appBrand": "chatgpt"}),
        )

        # Terminal operation ownership survives ordinary LoginState LRU/TTL
        # churn, so the same operation can never turn into a second flow.
        await fake.queue.put(
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-operation",
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            }
        )
        for _ in range(100):
            reconciled = await self.auth.login_start(operation_id)
            if reconciled.status is LoginStatus.COMPLETED:
                break
            await asyncio.sleep(0)
        self.assertEqual(reconciled.status, LoginStatus.COMPLETED)
        async with self.auth._state_lock:  # noqa: SLF001
            for index in range(80):
                self.auth._remember_locked(  # noqa: SLF001
                    type(reconciled)(
                        login_id=f"operation-lru-{index}",
                        status=LoginStatus.COMPLETED,
                        success=True,
                    )
                )
        retained = await self.auth.login_start(operation_id)
        self.assertEqual(retained.login_id, "login-operation")
        self.assertEqual(retained.status, LoginStatus.COMPLETED)
        self.assertEqual(fake.start_calls, 1)

    async def test_reused_remote_login_id_isolates_and_operation_retry_fails_closed(self) -> None:
        class FakeConfig:
            shutdown_timeout = 0.05

        class ReusedLoginClient:
            config = FakeConfig()

            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.start_calls = 0
                self.isolate_calls = 0

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, method, _params):
                if method == "account/login/cancel":
                    return {"status": "canceled"}
                self.start_calls += 1
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=reused",
                    "loginId": "login-reused",
                }

            async def isolate(self, _reason):
                self.isolate_calls += 1

        fake = ReusedLoginClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        first_id = "33333333-3333-4333-8333-333333333333"
        conflicting_id = "44444444-4444-4444-8444-444444444444"
        first = await self.auth.login_start(first_id)
        await self.auth.login_cancel(first.login_id)

        for _ in range(2):
            with self.assertRaises(CodexError) as context:
                await self.auth.login_start(conflicting_id)
            self.assertEqual(context.exception.code, "login_operation_conflict")
        self.assertEqual(fake.start_calls, 2)
        self.assertEqual(fake.isolate_calls, 1)

    async def test_auth_close_cancels_registry_owned_start_boundedly(self) -> None:
        class HangingLoginClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.request_started = asyncio.Event()

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, _method, _params):
                self.request_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        fake = HangingLoginClient()
        auth = CodexAuthService(fake)  # type: ignore[arg-type]
        operation_id = "55555555-5555-4555-8555-555555555555"
        waiter = asyncio.create_task(auth.login_start(operation_id))
        await fake.request_started.wait()
        await asyncio.wait_for(auth.close(), timeout=0.2)
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        with self.assertRaises(CodexError) as context:
            await auth.login_start(operation_id)
        self.assertEqual(context.exception.code, "shutting_down")

    async def test_login_operation_registry_capacity_fails_closed_without_eviction(self) -> None:
        class CapacityLoginClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.start_calls = 0

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, method, _params):
                if method == "account/login/cancel":
                    return {"status": "canceled"}
                self.start_calls += 1
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=capacity",
                    "loginId": f"login-capacity-{self.start_calls}",
                }

        fake = CapacityLoginClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        first_operation = "aaaaaaaa-aaaa-4aaa-8aaa-000000000000"
        first_login_id = ""
        for index in range(64):
            operation_id = f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012x}"
            state = await self.auth.login_start(operation_id)
            if index == 0:
                first_login_id = state.login_id
            await self.auth.login_cancel(state.login_id)

        with self.assertRaises(CodexError) as context:
            await self.auth.login_start("bbbbbbbb-bbbb-4bbb-8bbb-000000000000")
        self.assertEqual(context.exception.code, "login_operation_capacity")
        self.assertEqual(fake.start_calls, 64)
        retained = await self.auth.login_start(first_operation)
        self.assertEqual(retained.login_id, first_login_id)
        self.assertEqual(retained.status, LoginStatus.CANCELED)
        self.assertEqual(fake.start_calls, 64)

    async def test_ambiguous_login_start_isolates_without_retry_or_url_state(self) -> None:
        for index, mode in enumerate(("lost-login-start", "eof-login-start"), 1):
            with self.subTest(mode=mode):
                client, auth = self.make_auth_stack(mode)
                try:
                    operation_id = f"00000000-0000-4000-8000-{index:012d}"
                    for _ in range(2):
                        with self.assertRaises(CodexAmbiguousRequestError):
                            await auth.login_start(operation_id)

                    self.assertFalse(client.started)
                    self.assertEqual((await client.health())["pending_requests"], 0)
                    self.assertFalse(
                        any(
                            state.status is LoginStatus.PENDING
                            for state in auth._states.values()  # noqa: SLF001
                        )
                    )
                    self.assertNotIn(
                        "auth_url",
                        json.dumps(
                            [state.to_dict() for state in auth._states.values()]  # noqa: SLF001
                        ),
                    )
                finally:
                    await auth.close()
                    await client.close()
                    self.auth = None
                    self.client = None
        messages = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(message.get("method") == "account/login/start" for message in messages),
            2,
        )

    async def test_ambiguous_login_cancel_isolates_and_fails_pending_without_url(self) -> None:
        for mode in ("lost-login-cancel", "eof-login-cancel"):
            with self.subTest(mode=mode):
                client, auth = self.make_auth_stack(mode)
                try:
                    pending = await auth.login_start()
                    self.assertEqual(pending.status, LoginStatus.PENDING)
                    with self.assertRaises(CodexAmbiguousRequestError):
                        await auth.login_cancel(pending.login_id)

                    self.assertFalse(client.started)
                    self.assertEqual((await client.health())["pending_requests"], 0)
                    failed = await auth.login_status(pending.login_id)
                    self.assertEqual(failed.status, LoginStatus.FAILED)
                    self.assertIn(failed.error, {"process_exit", "isolation_failed"})
                    self.assertNotIn("auth_url", failed.to_dict())
                    self.assertFalse(
                        any(
                            state.status is LoginStatus.PENDING
                            for state in auth._states.values()  # noqa: SLF001
                        )
                    )
                finally:
                    await auth.close()
                    await client.close()
                    self.auth = None
                    self.client = None
        messages = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(message.get("method") == "account/login/cancel" for message in messages),
            2,
        )

    async def test_pending_login_is_not_ttl_or_lru_evicted_and_new_start_is_rejected(self) -> None:
        client, _agent = self.make_stack()
        self.auth = CodexAuthService(client)
        pending = await self.auth.login_start()
        self.auth._state_expiry[pending.login_id] = 0.0  # noqa: SLF001
        self.assertEqual(
            (await self.auth.login_status(pending.login_id)).status,
            LoginStatus.PENDING,
        )
        async with self.auth._state_lock:  # noqa: SLF001
            for index in range(80):
                self.auth._remember_locked(  # noqa: SLF001
                    type(pending)(
                        login_id=f"terminal-login-{index}",
                        status=LoginStatus.COMPLETED,
                        success=True,
                    )
                )
        self.assertEqual(
            (await self.auth.login_status(pending.login_id)).status,
            LoginStatus.PENDING,
        )
        with self.assertRaises(CodexError) as context:
            await self.auth.login_start()
        self.assertEqual(context.exception.code, "login_in_progress")
        messages = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            sum(message.get("method") == "account/login/start" for message in messages),
            1,
        )
        await self.auth.login_cancel(pending.login_id)

    async def test_login_cancel_preserves_generated_not_found_status(self) -> None:
        class NotFoundCancelClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, method, params):
                if method == "account/login/start":
                    return {
                        "type": "chatgpt",
                        "authUrl": "https://auth.openai.com/oauth/authorize?client_id=not-found",
                        "loginId": "login-not-found",
                    }
                self.assert_cancel = (method, params)
                return {"status": "notFound"}

        fake_client = NotFoundCancelClient()
        self.auth = CodexAuthService(fake_client)  # type: ignore[arg-type]
        await self.auth.login_start()
        state = await self.auth.login_cancel("login-not-found")
        self.assertEqual(state.status.value, "not_found")
        self.assertFalse(state.success)
        self.assertEqual(
            fake_client.assert_cancel,
            ("account/login/cancel", {"loginId": "login-not-found"}),
        )

    async def test_login_completion_before_response_is_claimed_atomically(self) -> None:
        class EarlyCompletionClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.auth: CodexAuthService | None = None

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, method, _params):
                self.assert_method = method
                await self.queue.put(
                    {
                        "method": "account/login/completed",
                        "params": {
                            "loginId": "login-early",
                            "success": True,
                            "error": None,
                            "onboardingEntrypoint": None,
                        },
                    }
                )
                assert self.auth is not None
                for _ in range(100):
                    if "login-early" in self.auth._early_completions:  # noqa: SLF001
                        break
                    await asyncio.sleep(0)
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=early",
                    "loginId": "login-early",
                }

        fake = EarlyCompletionClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        fake.auth = self.auth
        state = await self.auth.login_start()
        self.assertEqual(state.status.value, "completed")
        self.assertTrue(state.success)
        self.assertEqual((await self.auth.login_status("login-early")).status.value, "completed")

    async def test_login_response_future_then_completion_before_task_resume_is_claimed(self) -> None:
        class ResponseFutureRaceClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.response_delivered = asyncio.Event()
                self.auth: CodexAuthService | None = None

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, _method, _params):
                # Models the reader setting the response Future and then
                # broadcasting a following notification before login_start's
                # task is rescheduled to register its pending state.
                self.response_delivered.set()
                await self.queue.put(
                    {
                        "method": "account/login/completed",
                        "params": {
                            "loginId": "login-resume-race",
                            "success": False,
                            "error": "provider detail must be reduced",
                            "onboardingEntrypoint": None,
                        },
                    }
                )
                assert self.auth is not None
                for _ in range(100):
                    if "login-resume-race" in self.auth._early_completions:  # noqa: SLF001
                        break
                    await asyncio.sleep(0)
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=race",
                    "loginId": "login-resume-race",
                }

        fake = ResponseFutureRaceClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        fake.auth = self.auth
        state = await self.auth.login_start()
        self.assertTrue(fake.response_delivered.is_set())
        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.error, "login_failed")

    async def test_app_server_terminal_fails_all_pending_logins_promptly(self) -> None:
        class PendingLoginClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def request(self, _method, _params):
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=pending",
                    "loginId": "login-pending",
                }

        fake = PendingLoginClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        self.assertEqual((await self.auth.login_start()).status.value, "pending")
        await fake.queue.put(
            {"method": INTERNAL_APP_SERVER_EXITED, "params": {"reason": "fixed"}}
        )
        for _ in range(100):
            state = await self.auth.login_status("login-pending")
            if state.status.value != "pending":
                break
            await asyncio.sleep(0)
        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.error, "process_exit")

    async def test_conflicting_early_login_terminals_fail_closed_and_isolate(self) -> None:
        class ConflictingLoginClient:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
                self.auth: CodexAuthService | None = None
                self.isolate_calls = 0

            def subscribe(self):
                return self.queue

            def unsubscribe(self, _queue):
                return None

            async def isolate(self, _reason):
                self.isolate_calls += 1

            async def request(self, _method, _params):
                for success in (True, False):
                    await self.queue.put(
                        {
                            "method": "account/login/completed",
                            "params": {
                                "loginId": "login-conflict",
                                "success": success,
                                "error": None if success else "provider detail",
                                "onboardingEntrypoint": None,
                            },
                        }
                    )
                assert self.auth is not None
                for _ in range(100):
                    early = self.auth._early_completions.get("login-conflict")  # noqa: SLF001
                    if early is not None and early.error == "protocol_conflict":
                        break
                    await asyncio.sleep(0)
                return {
                    "type": "chatgpt",
                    "authUrl": "https://auth.openai.com/oauth/authorize?client_id=conflict",
                    "loginId": "login-conflict",
                }

        fake = ConflictingLoginClient()
        self.auth = CodexAuthService(fake)  # type: ignore[arg-type]
        fake.auth = self.auth
        state = await self.auth.login_start()
        self.assertEqual(state.status.value, "failed")
        self.assertEqual(state.error, "protocol_conflict")
        self.assertEqual(fake.isolate_calls, 1)

    async def test_login_notifications_require_pending_owned_id_and_ignore_foreign_terminal(self) -> None:
        client, _agent = self.make_stack()
        self.auth = CodexAuthService(client)
        await self.auth.login_start()
        await client._broadcast(  # noqa: SLF001
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "foreign-login",
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            }
        )
        await asyncio.sleep(0.02)
        self.assertEqual((await self.auth.login_status("foreign-login")).status.value, "not_found")
        await client._broadcast(  # noqa: SLF001
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-1",
                    "success": True,
                    "error": None,
                    "onboardingEntrypoint": None,
                },
            }
        )
        await asyncio.sleep(0.02)
        self.assertEqual((await self.auth.login_status("login-1")).status.value, "completed")
        # A contradictory second authority is a protocol conflict. Preserve
        # neither optimistic outcome: quarantine the shared app-server.
        await client._broadcast(  # noqa: SLF001
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-1",
                    "success": False,
                    "error": "secret provider detail /private/path",
                    "onboardingEntrypoint": None,
                },
            }
        )
        await asyncio.sleep(0.02)
        conflicted = await self.auth.login_status("login-1")
        self.assertEqual(conflicted.status.value, "failed")
        self.assertEqual(conflicted.error, "protocol_conflict")

        # A fresh pending flow consumes the generated success=false shape as a
        # secret-free failure. There are no stable failed/canceled notification
        # method variants; cancellation comes from account/login/cancel.
        await self.auth.close()
        self.auth = CodexAuthService(client)
        await self.auth.login_start()
        await client._broadcast(  # noqa: SLF001
            {
                "method": "account/login/completed",
                "params": {
                    "loginId": "login-1",
                    "success": False,
                    "error": "secret provider detail /private/path",
                    "onboardingEntrypoint": None,
                },
            }
        )
        await asyncio.sleep(0.02)
        failed = await self.auth.login_status("login-1")
        self.assertEqual(failed.status.value, "failed")
        self.assertFalse(failed.success)
        self.assertEqual(failed.error, "login_failed")
        self.assertNotIn("secret", json.dumps(failed.to_dict()))

    async def test_account_union_validation_is_fail_closed_and_secret_free(self) -> None:
        class AccountClient:
            def __init__(self, account):
                self.account = account

            async def request(self, method, params):
                self.assert_method = method
                return {"account": self.account, "requiresOpenaiAuth": False}

        variants = [
            {"type": "apiKey", "apiKey": "secret"},
            {"type": "amazonBedrock", "accessToken": "secret"},
            {"type": "chatgpt", "email": "x@example.test"},
            {"type": "chatgpt", "email": "x@example.test", "planType": "not-a-plan"},
            {"type": "chatgpt", "email": "x@example.test", "planType": []},
            {"type": "mystery", "email": "x@example.test", "planType": "pro"},
        ]
        for variant in variants:
            account = await CodexAuthService(AccountClient(variant)).account_read()
            self.assertEqual(account["state"], "signed_out")
            self.assertFalse(account["logged_in"])
            self.assertIsNone(account["account"])
            self.assertNotIn("secret", json.dumps(account))
            self.assertNotIn("email", json.dumps(account))

    async def test_event_error_mapping_is_secret_free(self) -> None:
        event = map_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-safe",
                    "turn": {
                        "id": "turn-safe",
                        "status": "failed",
                        "error": "secret prompt /Users/private/cwd token=abc",
                    },
                },
            },
            session_id="session-safe",
            correlation_id="corr-safe",
        )
        assert event is not None
        self.assertEqual(event.error, "codex_turn_failed")
        self.assertNotIn("secret", json.dumps(event.to_dict()))
        self.assertNotIn("/Users", json.dumps(event.to_dict()))

    async def test_interrupt_is_idempotent_and_waits_for_terminal(self) -> None:
        _client, agent = self.make_stack()
        stream_task = asyncio.create_task(
            self._collect(agent.stream_turn("session-interrupt", "wait", correlation_id="execution-1"))
        )
        for _ in range(100):
            if agent._active:  # noqa: SLF001 - correlation probe for the race test
                break
            await asyncio.sleep(0.01)
        self.assertTrue(agent._active)
        thread_id, turn_id = next(iter(agent._active))  # noqa: SLF001
        state = agent._active[(thread_id, turn_id)]  # noqa: SLF001
        with self.assertRaises(CodexError) as mixed_context:
            await agent.interrupt_by_reference(
                "session-interrupt",
                execution_id="execution-other",
                thread_id=thread_id,
                turn_id=turn_id,
            )
        self.assertEqual(mixed_context.exception.code, "turn_not_found")
        self.assertFalse(state.cancel_requested)
        with self.assertRaises(CodexError) as incomplete_context:
            await agent.interrupt_by_reference(
                "session-interrupt",
                execution_id="execution-1",
                thread_id=thread_id,
            )
        self.assertEqual(incomplete_context.exception.code, "turn_not_found")
        self.assertFalse(state.cancel_requested)
        with self.assertRaises(CodexError) as isolate_context:
            await agent.isolate_turn(
                "session-interrupt",
                "execution-1",
                thread_id="thread-other",
                turn_id=turn_id,
            )
        self.assertEqual(isolate_context.exception.code, "turn_not_found")
        self.assertFalse(state.cancel_requested)
        self.assertTrue(_client.started)
        first, second = await asyncio.gather(
            agent.interrupt_by_reference(
                "session-interrupt",
                execution_id="execution-1",
                thread_id=thread_id,
                turn_id=turn_id,
            ),
            agent.interrupt_by_reference(
                "session-interrupt",
                execution_id="execution-1",
                thread_id=thread_id,
                turn_id=turn_id,
            ),
        )
        events = await stream_task
        self.assertIs(first, second)
        self.assertEqual(first.type, AgentEventType.INTERRUPTED)
        self.assertEqual(events[-1].type, AgentEventType.INTERRUPTED)
        cached = await agent.interrupt_by_reference(
            "session-interrupt",
            execution_id="execution-1",
            thread_id=thread_id,
            turn_id=turn_id,
        )
        self.assertIs(cached, first)
        messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(item.get("method") == "turn/interrupt" for item in messages), 1)

    async def test_early_interrupt_records_intent_before_exact_ids_exist(self) -> None:
        _client, agent = self.make_stack()
        await agent.reserve_turn("session-early", "execution-early")
        stream_task = asyncio.create_task(
            self._collect(
                agent.stream_turn(
                    "session-early",
                    "wait",
                    correlation_id="execution-early",
                )
            )
        )
        terminal = await agent.interrupt_by_reference(
            "session-early",
            execution_id="execution-early",
        )
        events = await stream_task
        self.assertEqual(terminal.type, AgentEventType.INTERRUPTED)
        self.assertEqual(events[-1].type, AgentEventType.INTERRUPTED)
        messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(item.get("method") == "turn/interrupt" for item in messages), 1)
        # No successful terminal was observed, so a provisional thread is not
        # durable and the runtime mapping file remains absent.
        self.assertFalse(self.mapping_path.exists())

    async def test_isolate_after_release_loss_is_idempotent_for_exact_execution(self) -> None:
        client, agent = self.make_stack()
        events = [
            event async for event in agent.stream_turn(
                "session-release-loss",
                "hello",
                correlation_id="execution-release-loss",
            )
        ]
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        # Simulate the HTTP isolate fallback arriving after provider cleanup
        # but after the WS `turn/released` frame was lost.  The exact execution
        # ledger must acknowledge it without killing a healthy new process.
        self.assertEqual(
            await agent.isolate_turn("session-release-loss", "execution-release-loss"),
            "released",
        )
        self.assertTrue(client.started)
        with self.assertRaises(CodexError) as context:
            await agent.isolate_turn("session-release-loss", "unknown-execution")
        self.assertEqual(context.exception.code, "turn_not_found")

    async def test_late_verified_app_server_exit_preserves_completed_durable_mapping(self) -> None:
        client, agent = self.make_stack()
        session_id = "session-late-exit-resume"
        execution_id = "execution-late-exit-resume"
        stream = agent.stream_turn(
            session_id,
            "hello",
            correlation_id=execution_id,
        )
        terminal = None
        while terminal is None:
            event = await asyncio.wait_for(anext(stream), timeout=2.0)
            if event.terminal:
                terminal = event
        self.assertEqual(terminal.type, AgentEventType.FINISHED)
        self.assertEqual(terminal.status, "completed")
        thread_id = terminal.thread_id
        turn_id = terminal.turn_id
        self.assertIsInstance(thread_id, str)
        state = agent._active[(thread_id, turn_id)]  # noqa: SLF001
        self.assertTrue(state.mapping_committed)
        self.assertEqual(agent.thread_manager.store.read()[session_id], thread_id)

        # Hold an observable release fence while the exact client verifies and
        # publishes its internal process-exit sentinel. The terminal-aware
        # handler must settle that fence without deleting durable history.
        state.begin_release_fence()
        self.assertTrue(await client.isolate("late verified process exit"))
        for _ in range(200):
            if not state.release_fence_active:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(state.release_fence_active)
        self.assertEqual(agent.thread_manager.store.read()[session_id], thread_id)

        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertTrue(state.released_event.is_set())
        self.assertTrue(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )
        self.assertEqual(
            await agent.wait_for_execution_release(session_id, execution_id),
            "released",
        )

        await agent.close()
        await client.close()
        self.agent = None
        self.client = None

        _new_client, resumed_agent = self.make_stack()
        resumed = [
            event
            async for event in resumed_agent.stream_turn(
                session_id,
                "hello",
                correlation_id="execution-after-late-exit",
            )
        ]
        self.assertEqual(resumed[-1].thread_id, thread_id)
        methods = [
            json.loads(line).get("method")
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("thread/resume", methods)

    async def test_conflicting_provider_turn_terminal_isolates_and_poisons_release(self) -> None:
        client, agent = self.make_stack()
        session_id = "session-provider-terminal-conflict"
        execution_id = "execution-provider-terminal-conflict"
        stream = agent.stream_turn(
            session_id,
            "wait",
            correlation_id=execution_id,
        )
        started = await asyncio.wait_for(anext(stream), timeout=2.0)
        self.assertEqual(started.type, AgentEventType.STARTED)
        self.assertIsInstance(started.thread_id, str)
        self.assertIsInstance(started.turn_id, str)
        state = agent._active[(started.thread_id, started.turn_id)]  # noqa: SLF001

        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": started.thread_id,
                "turn": {
                    "id": started.turn_id,
                    "items": [],
                    "status": "completed",
                },
            },
        }
        client._schema_validator.validate_server_notification(completed)  # noqa: SLF001
        await client._broadcast(completed)  # noqa: SLF001
        terminal = await asyncio.wait_for(anext(stream), timeout=2.0)
        self.assertEqual(terminal.type, AgentEventType.FINISHED)
        self.assertTrue(state.mapping_committed)

        duplicate = {**completed, "emittedAtMs": 999}
        client._schema_validator.validate_server_notification(duplicate)  # noqa: SLF001
        await client._broadcast(duplicate)  # noqa: SLF001
        await asyncio.sleep(0.01)
        self.assertTrue(client.started)
        self.assertFalse(state.release_poisoned)
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertTrue(state.released_event.is_set())
        self.assertTrue(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )

        conflict = {
            "method": "turn/completed",
            "params": {
                "threadId": started.thread_id,
                "turn": {
                    "id": started.turn_id,
                    "items": [],
                    "status": "failed",
                },
            },
        }
        client._schema_validator.validate_server_notification(conflict)  # noqa: SLF001
        await client._broadcast(conflict)  # noqa: SLF001
        for _ in range(200):
            finished = agent._finished_by_execution.get(  # noqa: SLF001
                (session_id, execution_id)
            )
            if (
                finished is not None
                and not finished[1]
                and not client.started
                and session_id not in agent.thread_manager.store.read()
            ):
                break
            await asyncio.sleep(0.01)
        self.assertFalse(client.started)
        self.assertNotIn(session_id, agent.thread_manager.store.read())
        self.assertFalse(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )
        self.assertEqual(
            await agent.wait_for_execution_release(session_id, execution_id),
            "poisoned",
        )

    async def test_pristine_reservation_isolate_is_atomic_release_without_spawn(self) -> None:
        client, agent = self.make_stack()
        state = await agent.reserve_turn(
            "session-pristine-reservation",
            "execution-pristine-reservation",
        )
        isolate_calls = 0

        async def forbidden_isolate(_reason=""):
            nonlocal isolate_calls
            isolate_calls += 1
            raise AssertionError("pristine reservation reached process isolation")

        with patch.object(client, "isolate", forbidden_isolate):
            self.assertEqual(
                await agent.isolate_turn(
                    "session-pristine-reservation",
                    "execution-pristine-reservation",
                ),
                "released",
            )
            # A lost WS release frame is acknowledged from the exact ledger.
            self.assertEqual(
                await agent.isolate_turn(
                    "session-pristine-reservation",
                    "execution-pristine-reservation",
                ),
                "released",
            )
        self.assertEqual(isolate_calls, 0)
        self.assertFalse(client.started)
        self.assertTrue(state.released_event.is_set())
        self.assertEqual(state.terminal.status, "start_canceled")
        self.assertEqual(
            await agent.wait_for_execution_release(
                "session-pristine-reservation",
                "execution-pristine-reservation",
            ),
            "released",
        )

    async def test_cancel_reservation_retains_terminal_and_poisoned_ownership(self) -> None:
        _client, agent = self.make_stack()
        state = await agent.reserve_turn(
            "session-owned-reservation",
            "execution-owned-reservation",
        )
        state.push(
            AgentEvent(
                AgentEventType.ERROR,
                session_id=state.session_id,
                correlation_id=state.correlation_id,
                status="isolation_failed",
                error="codex app-server process isolation failed",
            )
        )
        state.poison_release_authority()
        self.assertFalse(
            await agent.cancel_reservation(
                "session-owned-reservation",
                "execution-owned-reservation",
            )
        )
        self.assertIs(
            agent._pending_by_execution[  # noqa: SLF001 - ownership invariant
                ("session-owned-reservation", "execution-owned-reservation")
            ],
            state,
        )
        self.assertEqual(
            await agent.wait_for_execution_release(
                "session-owned-reservation",
                "execution-owned-reservation",
            ),
            "poisoned",
        )

    async def test_explicit_turn_start_rejection_is_safe_without_process_isolation(self) -> None:
        client, agent = self.make_stack(request_timeout=0.1)
        events = [
            event
            async for event in agent.stream_turn(
                "session-start-rejected",
                "reject-start",
                correlation_id="execution-start-rejected",
            )
        ]
        terminals = [event for event in events if event.terminal]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "start_failed")
        self.assertTrue(client.started)
        self.assertTrue(
            agent._finished_by_execution[  # noqa: SLF001 - exact release-authority probe
                ("session-start-rejected", "execution-start-rejected")
            ][1]
        )
        # A lost Host release ACK can use the explicit rejection ledger; it
        # must not kill the still-healthy shared App Server.
        self.assertEqual(
            await agent.isolate_turn("session-start-rejected", "execution-start-rejected"),
            "released",
        )
        self.assertTrue(client.started)

    async def test_lost_turn_start_response_holds_state_until_process_group_is_gone(self) -> None:
        client, agent = self.make_stack(request_timeout=0.05)
        entered_termination = asyncio.Event()
        allow_termination = asyncio.Event()
        original_terminate = client._terminate_process  # noqa: SLF001

        async def gated_terminate(process, *, generation=None):
            entered_termination.set()
            await allow_termination.wait()
            return await original_terminate(process, generation=generation)

        client._terminate_process = gated_terminate  # type: ignore[method-assign]  # noqa: SLF001
        stream_task = asyncio.create_task(
            self._collect(
                agent.stream_turn(
                    "session-lost-start",
                    "lost-start-response",
                    correlation_id="execution-lost-start",
                )
            )
        )
        state = None
        events: list[AgentEvent] = []
        try:
            await asyncio.wait_for(entered_termination.wait(), timeout=1.0)
            state = agent._pending_by_execution[  # noqa: SLF001 - isolation-fence probe
                ("session-lost-start", "execution-lost-start")
            ]
            self.assertIs(agent._pending_by_thread.get(state.thread_id), state)  # noqa: SLF001
            self.assertEqual(
                await agent.thread_manager.mapping("session-lost-start"),
                state.thread_id,
            )
            self.assertFalse(state.released_event.is_set())
            self.assertNotIn(
                ("session-lost-start", "execution-lost-start"),
                agent._finished_by_execution,  # noqa: SLF001
            )
            self.assertTrue(client.started)
            allow_termination.set()
            events = await asyncio.wait_for(stream_task, timeout=2.0)
        finally:
            allow_termination.set()
            if not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001

        assert state is not None
        terminals = [event for event in events if event.terminal]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "process_exit")
        self.assertFalse(client.started)
        self.assertTrue(state.released_event.is_set())
        self.assertNotIn(state.thread_id, agent._pending_by_thread)  # noqa: SLF001
        self.assertNotIn(
            ("session-lost-start", "execution-lost-start"),
            agent._pending_by_execution,  # noqa: SLF001
        )
        self.assertIsNone(await agent.thread_manager.mapping("session-lost-start"))
        self.assertTrue(
            agent._finished_by_execution[  # noqa: SLF001
                ("session-lost-start", "execution-lost-start")
            ][1]
        )

    async def test_lost_turn_start_response_kill_failure_poisoned_ledger(self) -> None:
        client, agent = self.make_stack(request_timeout=0.05)
        original_isolate = client.isolate

        async def fail_isolate(_reason: str = "") -> None:
            raise CodexError("process group survived SIGKILL", code="isolation_failed")

        client.isolate = fail_isolate  # type: ignore[method-assign]
        try:
            events = [
                event
                async for event in agent.stream_turn(
                    "session-lost-start-kill-failure",
                    "lost-start-response",
                    correlation_id="execution-lost-start-kill-failure",
                )
            ]
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]

        terminals = [event for event in events if event.terminal]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "isolation_failed")
        self.assertTrue(client.started)
        execution_key = (
            "session-lost-start-kill-failure",
            "execution-lost-start-kill-failure",
        )
        state = agent._pending_by_execution[execution_key]  # noqa: SLF001
        self.assertFalse(state.released_event.is_set())
        self.assertNotIn(execution_key, agent._finished_by_execution)  # noqa: SLF001
        self.assertEqual(
            await agent.wait_for_execution_release(*execution_key),
            "poisoned",
        )
        with self.assertRaises(CodexError) as retry_context:
            await agent.isolate_turn(
                "session-lost-start-kill-failure",
                "execution-lost-start-kill-failure",
            )
        self.assertEqual(retry_context.exception.code, "isolation_failed")
        self.assertTrue(state.released_event.is_set())
        self.assertFalse(agent._finished_by_execution[execution_key][1])  # noqa: SLF001
        self.assertEqual(
            await agent.wait_for_execution_release(*execution_key),
            "poisoned",
        )

    async def test_cancel_during_ambiguous_start_waits_for_isolation_fence(self) -> None:
        client, agent = self.make_stack(request_timeout=0.05)
        entered_termination = asyncio.Event()
        allow_termination = asyncio.Event()
        original_terminate = client._terminate_process  # noqa: SLF001

        async def gated_terminate(process, *, generation=None):
            entered_termination.set()
            await allow_termination.wait()
            return await original_terminate(process, generation=generation)

        client._terminate_process = gated_terminate  # type: ignore[method-assign]  # noqa: SLF001
        stream_task = asyncio.create_task(
            self._collect(
                agent.stream_turn(
                    "session-cancel-ambiguous-start",
                    "lost-start-response",
                    correlation_id="execution-cancel-ambiguous-start",
                )
            )
        )
        state = None
        try:
            await asyncio.wait_for(entered_termination.wait(), timeout=1.0)
            stream_task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stream_task.done())
            state = agent._pending_by_execution[  # noqa: SLF001
                (
                    "session-cancel-ambiguous-start",
                    "execution-cancel-ambiguous-start",
                )
            ]
            self.assertFalse(state.released_event.is_set())
            self.assertIsNotNone(await agent.thread_manager.mapping(state.session_id))
            allow_termination.set()
            with self.assertRaises(asyncio.CancelledError):
                await stream_task
        finally:
            allow_termination.set()
            if not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            client._terminate_process = original_terminate  # type: ignore[method-assign]  # noqa: SLF001

        assert state is not None
        self.assertTrue(state.released_event.is_set())
        self.assertFalse(client.started)
        self.assertTrue(
            agent._finished_by_execution[  # noqa: SLF001
                (
                    "session-cancel-ambiguous-start",
                    "execution-cancel-ambiguous-start",
                )
            ][1]
        )

    async def test_isolation_mapping_cleanup_failure_never_safe_releases_stale_mapping(self) -> None:
        client, agent = self.make_stack(request_timeout=0.05)
        session_id = "session-mapping-cleanup-failure"
        execution_id = "execution-mapping-cleanup-failure"
        thread_id = "thread-mapping-cleanup-failure"
        await agent.thread_manager.store.set(session_id, thread_id)
        original_invalidate = agent.thread_manager.invalidate

        async def fail_invalidate(_session_id, _thread_id=None):
            raise OSError("simulated mapping cleanup failure")

        agent.thread_manager.invalidate = fail_invalidate  # type: ignore[method-assign]
        try:
            events = [
                event
                async for event in agent.stream_turn(
                    session_id,
                    "lost-start-response",
                    correlation_id=execution_id,
                )
            ]
        finally:
            agent.thread_manager.invalidate = original_invalidate  # type: ignore[method-assign]

        terminals = [event for event in events if event.terminal]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "isolation_failed")
        self.assertFalse(client.started)
        self.assertEqual(agent.thread_manager.store.read().get(session_id), thread_id)
        state = agent._pending_by_execution[(session_id, execution_id)]  # noqa: SLF001
        self.assertFalse(state.released_event.is_set())
        self.assertNotIn((session_id, execution_id), agent._finished_by_execution)  # noqa: SLF001

        # A later exact retry may retire the retained ownership only after its
        # mapping cleanup succeeds. The original failure remains sticky in the
        # bounded release ledger.
        with self.assertRaises(CodexError) as retry_context:
            await agent.isolate_turn(session_id, execution_id)
        self.assertEqual(retry_context.exception.code, "isolation_failed")
        self.assertTrue(state.released_event.is_set())
        self.assertIsNone(agent.thread_manager.store.read().get(session_id))
        self.assertFalse(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )

    async def test_eof_or_malformed_turn_start_response_never_becomes_start_failed(self) -> None:
        for index, text in enumerate(("eof-start-response", "malformed-start-response")):
            with self.subTest(text=text):
                client, agent = self.make_stack(request_timeout=0.2)
                try:
                    session_id = f"session-post-write-{index}"
                    execution_id = f"execution-post-write-{index}"
                    events = [
                        event
                        async for event in agent.stream_turn(
                            session_id,
                            text,
                            correlation_id=execution_id,
                        )
                    ]
                    terminals = [event for event in events if event.terminal]
                    self.assertEqual(len(terminals), 1)
                    self.assertEqual(terminals[0].status, "process_exit")
                    self.assertNotEqual(terminals[0].status, "start_failed")
                    self.assertFalse(client.started)
                    self.assertTrue(
                        agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
                    )
                finally:
                    await agent.close()
                    await client.close()
                    self.agent = None
                    self.client = None

    async def test_cached_terminal_yield_does_not_hold_global_state_lock(self) -> None:
        _client, agent = self.make_stack()
        events = [
            event
            async for event in agent.stream_turn(
                "session-cached-lock",
                "hello",
                correlation_id="execution-cached-lock",
            )
        ]
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        cached = agent.stream_turn(
            "session-cached-lock",
            "ignored after terminal",
            correlation_id="execution-cached-lock",
        )
        self.assertEqual((await cached.__anext__()).type, AgentEventType.FINISHED)
        # The cached branch is suspended at its yield. A different session
        # must still acquire the state lock before the cached consumer resumes.
        reservation = await asyncio.wait_for(
            agent.reserve_turn("session-other-lock", "execution-other-lock"),
            timeout=0.25,
        )
        self.assertEqual(reservation.session_id, "session-other-lock")
        await cached.aclose()

    async def test_release_ledger_waits_for_mapping_cleanup_and_is_single_flight(self) -> None:
        client, agent = self.make_stack()
        original_isolate = client.isolate

        async def verified_isolate(_reason=""):
            return True

        client.isolate = verified_isolate  # type: ignore[method-assign]
        state = await agent.reserve_turn("session-release-fence", "execution-release-fence")
        state.thread_id = "thread-release-fence"
        state.turn_id = "turn-release-fence"
        state.terminal = AgentEvent(
            AgentEventType.FINISHED,
            session_id=state.session_id,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            correlation_id=state.correlation_id,
            status="completed",
            text="answer",
        )
        state.confirm_release_authority()
        agent._pending_by_thread[state.thread_id] = state  # noqa: SLF001
        agent._active[(state.thread_id, state.turn_id)] = state  # noqa: SLF001
        entered = asyncio.Event()
        release = asyncio.Event()
        discard_calls = 0
        finish_calls = 0
        finish_inflight = 0
        max_finish_inflight = 0
        manager = agent.thread_manager
        original_discard = manager.discard_provisional
        original_finish_once = agent._finish_state_once  # noqa: SLF001

        async def blocked_discard(session_id, thread_id=None):
            nonlocal discard_calls
            discard_calls += 1
            entered.set()
            await release.wait()
            await original_discard(session_id, thread_id)

        manager.discard_provisional = blocked_discard  # type: ignore[method-assign]
        async def counted_finish_once(value):
            nonlocal finish_calls, finish_inflight, max_finish_inflight
            finish_calls += 1
            finish_inflight += 1
            max_finish_inflight = max(max_finish_inflight, finish_inflight)
            try:
                return await original_finish_once(value)
            finally:
                finish_inflight -= 1

        agent._finish_state_once = counted_finish_once  # type: ignore[method-assign]  # noqa: SLF001
        try:
            first = asyncio.create_task(agent._finish_state(state))  # noqa: SLF001
            second = asyncio.create_task(agent._finish_state(state))  # noqa: SLF001
            await asyncio.wait_for(entered.wait(), timeout=0.25)
            self.assertFalse(state.released_event.is_set())
            retry = asyncio.create_task(
                agent.isolate_turn("session-release-fence", "execution-release-fence")
            )
            await asyncio.sleep(0)
            self.assertFalse(retry.done())
            release.set()
            await asyncio.gather(first, second, retry)
            self.assertTrue(state.released_event.is_set())
            # The two original callers share one release attempt. isolate_turn
            # separately invalidates the mapping and revokes that attempt while
            # process quiescence is unknown, so exactly one sequential retry is
            # required after the verified fence; neither attempt overlaps.
            self.assertEqual(discard_calls, 3)
            self.assertEqual(finish_calls, 2)
            self.assertEqual(max_finish_inflight, 1)
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]
            manager.discard_provisional = original_discard  # type: ignore[method-assign]
            agent._finish_state_once = original_finish_once  # type: ignore[method-assign]  # noqa: SLF001

    async def test_isolate_waits_for_blocked_commit_then_removes_final_mapping(self) -> None:
        client, agent = self.make_stack(request_timeout=1.0)
        await client.ensure_started()
        session_id = "session-commit-isolate-race"
        execution_id = "execution-commit-isolate-race"
        thread_id = "thread-commit-isolate-race"
        turn_id = "turn-commit-isolate-race"
        state = await agent.reserve_turn(session_id, execution_id)
        await agent._bind_turn_ids(state, thread_id, turn_id)  # noqa: SLF001

        entered_commit = asyncio.Event()
        allow_commit = asyncio.Event()
        manager = agent.thread_manager
        original_commit = manager.commit_thread

        async def blocked_commit(*args, **kwargs):
            entered_commit.set()
            await allow_commit.wait()
            await original_commit(*args, **kwargs)

        async def deliver_completed() -> None:
            async with state.notification_lock:
                await agent._dispatch_notification(  # noqa: SLF001
                    state,
                    "turn/completed",
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "items": [],
                                "status": "completed",
                            },
                        },
                    },
                )

        manager.commit_thread = blocked_commit  # type: ignore[method-assign]
        commit_task = asyncio.create_task(deliver_completed())
        isolate_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered_commit.wait(), timeout=1.0)
            isolate_task = asyncio.create_task(
                agent.isolate("commit/isolate serialization", target=state)
            )
            for _ in range(100):
                if state.release_fence_active:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(state.release_fence_active)
            self.assertFalse(isolate_task.done())
            self.assertFalse(state.released_event.is_set())
            self.assertNotIn(
                (session_id, execution_id),
                agent._finished_by_execution,  # noqa: SLF001
            )

            allow_commit.set()
            await asyncio.gather(commit_task, isolate_task)
        finally:
            allow_commit.set()
            manager.commit_thread = original_commit  # type: ignore[method-assign]
            if not commit_task.done():
                commit_task.cancel()
                await asyncio.gather(commit_task, return_exceptions=True)
            if isolate_task is not None and not isolate_task.done():
                isolate_task.cancel()
                await asyncio.gather(isolate_task, return_exceptions=True)

        self.assertTrue(state.mapping_committed)
        self.assertTrue(state.released_event.is_set())
        self.assertEqual(agent.thread_manager.store.read(), {})
        self.assertTrue(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )
        self.assertEqual(
            await agent.wait_for_execution_release(session_id, execution_id),
            "released",
        )

    async def test_oversized_or_missing_agent_snapshot_never_falls_back_to_buffered_delta(self) -> None:
        _client, agent = self.make_stack()
        state = await agent.reserve_turn("session-snapshot-bound", "execution-snapshot-bound")
        state.thread_id = "thread-snapshot-bound"
        state.turn_id = "turn-snapshot-bound"
        state.unknown_item_text["item-1"] = ["untrusted buffered text"]
        for item in (
            {"id": "item-1", "type": "agentMessage"},
            {"id": "item-1", "type": "agentMessage", "text": "x" * 16_001},
        ):
            with self.assertRaises(CodexError) as context:
                await agent._dispatch_notification(  # noqa: SLF001
                    state,
                    "item/completed",
                    {"method": "item/completed", "params": {"threadId": state.thread_id, "turnId": state.turn_id, "item": item}},
                )
            self.assertEqual(context.exception.code, "invalid_response")

    async def test_failed_process_group_isolation_never_claims_interrupt_isolated(self) -> None:
        client, agent = self.make_stack()
        state = await agent.reserve_turn("session-isolation-failure", "execution-isolation-failure")
        original_isolate = client.isolate

        async def fail_isolate(_reason: str = "") -> None:
            raise CodexError("process group survived SIGKILL", code="isolation_failed")

        client.isolate = fail_isolate  # type: ignore[method-assign]
        try:
            with self.assertRaises(CodexError):
                await agent.isolate("interrupt timeout", target=state)
            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "isolation_failed")
            self.assertNotEqual(state.terminal.status, "interrupt_isolated")
            self.assertFalse(state.released_event.is_set())
            self.assertIs(
                agent._pending_by_execution[  # noqa: SLF001
                    ("session-isolation-failure", "execution-isolation-failure")
                ],
                state,
            )
            self.assertNotIn(
                ("session-isolation-failure", "execution-isolation-failure"),
                agent._finished_by_execution,  # noqa: SLF001
            )
            with self.assertRaises(CodexError) as retry:
                await agent.isolate_turn("session-isolation-failure", "execution-isolation-failure")
            self.assertEqual(retry.exception.code, "isolation_failed")
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]

    async def test_kill_failure_retains_committed_mapping_until_close_reconciles(self) -> None:
        client, agent = self.make_stack(request_timeout=1.0)
        await client.ensure_started()
        session_id = "session-isolate-reconcile"
        execution_id = "execution-isolate-reconcile"
        thread_id = "thread-isolate-reconcile"
        state = await agent.reserve_turn(session_id, execution_id)
        state.thread_id = thread_id
        state.turn_id = "turn-isolate-reconcile"
        state.mapping_committed = True
        agent._pending_by_thread[thread_id] = state  # noqa: SLF001
        await agent.thread_manager.store.set(session_id, thread_id)

        isolate_calls = 0
        close_calls = 0
        original_isolate = client.isolate
        original_close = client.close

        async def fail_isolate(_reason: str = "") -> None:
            nonlocal isolate_calls
            isolate_calls += 1
            raise CodexProcessError("simulated unverified process group")

        async def counted_close() -> None:
            nonlocal close_calls
            close_calls += 1
            await original_close()

        client.isolate = fail_isolate  # type: ignore[method-assign]
        client.close = counted_close  # type: ignore[method-assign]
        try:
            with self.assertRaises(CodexProcessError):
                await agent.isolate("individual isolate failure", target=state)
            self.assertEqual(state.terminal.status, "isolation_failed")
            self.assertFalse(state.released_event.is_set())
            self.assertEqual(agent.thread_manager.store.read().get(session_id), thread_id)
            self.assertIs(
                agent._pending_by_execution[(session_id, execution_id)],  # noqa: SLF001
                state,
            )
            self.assertNotIn((session_id, execution_id), agent._finished_by_execution)  # noqa: SLF001

            # Provider close first retries isolate, then falls back to the
            # AppServerClient's retained exact process handle. Only that
            # verified outcome permits mapping invalidation and retirement.
            await agent.close()
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]
            client.close = original_close  # type: ignore[method-assign]

        self.assertEqual(isolate_calls, 2)
        self.assertEqual(close_calls, 1)
        self.assertTrue(state.released_event.is_set())
        self.assertEqual(agent.thread_manager.store.read(), {})
        self.assertNotIn((session_id, execution_id), agent._pending_by_execution)  # noqa: SLF001
        self.assertNotIn(thread_id, agent._pending_by_thread)  # noqa: SLF001
        self.assertFalse(
            agent._finished_by_execution[(session_id, execution_id)][1]  # noqa: SLF001
        )
        restarted = ThreadManager(client, ThreadMappingStore(self.mapping_path))
        self.assertIsNone(await restarted.mapping(session_id))

    async def test_concurrent_isolates_share_one_complete_process_group_outcome(self) -> None:
        client, _agent = self.make_stack()
        await client.ensure_started()
        calls = 0
        original_isolate_once = client._isolate_once  # noqa: SLF001

        async def counted_isolate_once() -> None:
            nonlocal calls
            calls += 1
            await original_isolate_once()

        client._isolate_once = counted_isolate_once  # type: ignore[method-assign]  # noqa: SLF001
        await asyncio.gather(client.isolate("first"), client.isolate("second"))
        self.assertEqual(calls, 1)
        self.assertFalse(client.started)

    async def test_provider_close_uses_one_bounded_process_fence_for_multiple_states(self) -> None:
        client, agent = self.make_stack(request_timeout=1.0)
        states = [
            await agent.reserve_turn("session-close-a", "execution-close-a"),
            await agent.reserve_turn("session-close-b", "execution-close-b"),
        ]
        isolate_calls = 0
        original_isolate = client.isolate
        original_interrupt = agent._interrupt_state  # noqa: SLF001

        async def counted_isolate(reason: str = "") -> None:
            nonlocal isolate_calls
            isolate_calls += 1
            await original_isolate(reason)

        async def forbidden_per_state_interrupt(_state):
            raise AssertionError("provider close used a per-state interrupt timeout")

        client.isolate = counted_isolate  # type: ignore[method-assign]
        agent._interrupt_state = forbidden_per_state_interrupt  # type: ignore[method-assign]  # noqa: SLF001
        try:
            await asyncio.wait_for(agent.close(), timeout=0.5)
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]
            agent._interrupt_state = original_interrupt  # type: ignore[method-assign]  # noqa: SLF001

        self.assertEqual(isolate_calls, 1)
        for state in states:
            self.assertTrue(state.released_event.is_set())
            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "process_exit")
            self.assertTrue(
                agent._finished_by_execution[  # noqa: SLF001
                    (state.session_id, state.correlation_id)
                ][1]
            )

    async def test_provider_close_timeout_falls_back_to_exact_close_before_mapping_release(self) -> None:
        client, agent = self.make_stack(request_timeout=1.0)
        client.config = replace(client.config, shutdown_timeout=0.05)
        states = [
            await agent.reserve_turn("session-close-timeout-a", "execution-close-timeout-a"),
            await agent.reserve_turn("session-close-timeout-b", "execution-close-timeout-b"),
        ]
        for index, state in enumerate(states):
            state.thread_id = f"thread-close-timeout-{index}"
            state.mapping_committed = True
            agent._pending_by_thread[state.thread_id] = state  # noqa: SLF001
            await agent.thread_manager.store.set(state.session_id, state.thread_id)
        isolate_calls = 0
        close_calls = 0
        original_isolate = client.isolate
        original_close = client.close

        async def hanging_isolate(_reason: str = "") -> None:
            nonlocal isolate_calls
            isolate_calls += 1
            await asyncio.Event().wait()

        async def counted_close() -> None:
            nonlocal close_calls
            close_calls += 1
            await original_close()

        client.isolate = hanging_isolate  # type: ignore[method-assign]
        client.close = counted_close  # type: ignore[method-assign]
        try:
            await asyncio.wait_for(agent.close(), timeout=0.8)
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]
            client.close = original_close  # type: ignore[method-assign]

        self.assertEqual(isolate_calls, 1)
        self.assertEqual(close_calls, 1)
        self.assertEqual(agent.thread_manager.store.read(), {})
        for state in states:
            self.assertTrue(state.released_event.is_set())
            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "process_exit")
            self.assertTrue(
                agent._finished_by_execution[  # noqa: SLF001
                    (state.session_id, state.correlation_id)
                ][1]
            )
        restarted = ThreadManager(client, ThreadMappingStore(self.mapping_path))
        for state in states:
            self.assertIsNone(await restarted.mapping(state.session_id))

    async def test_provider_close_retains_committed_mapping_when_both_quiescence_checks_fail(self) -> None:
        client, agent = self.make_stack(request_timeout=1.0)
        state = await agent.reserve_turn(
            "session-close-unverified",
            "execution-close-unverified",
        )
        state.thread_id = "thread-close-unverified"
        state.mapping_committed = True
        agent._pending_by_thread[state.thread_id] = state  # noqa: SLF001
        await agent.thread_manager.store.set(state.session_id, state.thread_id)
        original_isolate = client.isolate
        original_close = client.close

        async def fail_isolate(_reason: str = "") -> None:
            raise CodexProcessError("simulated unverified isolate")

        async def fail_close() -> None:
            raise CodexProcessError("simulated unverified close")

        client.isolate = fail_isolate  # type: ignore[method-assign]
        client.close = fail_close  # type: ignore[method-assign]
        try:
            with self.assertRaises(CodexProcessError):
                await agent.close()
            self.assertEqual(
                agent.thread_manager.store.read().get(state.session_id),
                state.thread_id,
            )
            self.assertIs(
                agent._pending_by_execution[  # noqa: SLF001
                    (state.session_id, state.correlation_id)
                ],
                state,
            )
            self.assertIs(agent._pending_by_thread[state.thread_id], state)  # noqa: SLF001
            self.assertFalse(state.released_event.is_set())
            self.assertIsNone(state.terminal)
            self.assertNotIn(
                (state.session_id, state.correlation_id),
                agent._finished_by_execution,  # noqa: SLF001
            )
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]
            client.close = original_close  # type: ignore[method-assign]

        # A later exact retry owns the retained state and removes the durable
        # mapping only after the real client verifies quiescence.
        await agent.close()
        self.assertTrue(state.released_event.is_set())
        self.assertEqual(agent.thread_manager.store.read(), {})

    async def test_reader_queue_overflow_isolates_real_process_before_terminal(self) -> None:
        # Keep the provider's own event queue large enough to observe the
        # process-facing subscriber overflow rather than tripping its local
        # per-turn backpressure first.
        client, agent = self.make_stack(queue_size=4, request_timeout=1.0, event_queue_size=32)
        # This subscriber never consumes. It forces the reserved critical lane
        # to overflow while the provider's own subscriber is still active.
        stalled = client.subscribe(maxsize=1)
        try:
            events = [
                event
                async for event in agent.stream_turn(
                    "session-queue-flood",
                    "queue-flood",
                    correlation_id="execution-queue-flood",
                )
            ]
            self.assertTrue(events[-1].terminal)
            self.assertIn(events[-1].status, {"process_exit", "isolation_failed"})
            self.assertFalse(client.started)
            self.assertLessEqual(stalled.qsize(), stalled.maxsize + max(4, stalled.maxsize))
        finally:
            client.unsubscribe(stalled)

    async def test_interrupt_without_terminal_isolates_process_and_all_turns(self) -> None:
        client, agent = self.make_stack(request_timeout=0.25)
        manager = agent.thread_manager
        await manager.store.set("session-isolate-a", "thread-seeded-a")
        await manager.store.set("session-isolate-b", "thread-seeded-b")
        first_task = asyncio.create_task(
            self._collect(agent.stream_turn("session-isolate-a", "ignore-interrupt", correlation_id="iso-a"))
        )
        second_task = asyncio.create_task(
            self._collect(agent.stream_turn("session-isolate-b", "ignore-interrupt", correlation_id="iso-b"))
        )
        for _ in range(100):
            if len(agent._active) >= 2:  # noqa: SLF001 - intentional race probe
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(agent._active), 2)
        target_thread, target_turn = next(iter(agent._active))  # noqa: SLF001
        terminal = await agent.interrupt(
            "session-isolate-a",
            thread_id=target_thread,
            turn_id=target_turn,
        )
        first_events, second_events = await asyncio.gather(first_task, second_task)
        self.assertIn(terminal.type, {AgentEventType.ERROR, AgentEventType.INTERRUPTED})
        self.assertTrue(first_events[-1].terminal)
        self.assertTrue(second_events[-1].terminal)
        self.assertIn(first_events[-1].status, {"process_exit", "interrupt_isolated"})
        self.assertEqual(second_events[-1].status, "process_exit")
        self.assertFalse(client.started)
        self.assertEqual(ThreadMappingStore(self.mapping_path).read(), {})

    async def test_early_interrupt_timeout_isolates_without_ghost_turn_start(self) -> None:
        client, agent = self.make_stack(request_timeout=0.1)
        await agent.reserve_turn("session-early-timeout", "early-timeout")
        terminal = await agent.interrupt_by_reference(
            "session-early-timeout",
            execution_id="early-timeout",
        )
        self.assertEqual(terminal.type, AgentEventType.ERROR)
        self.assertEqual(terminal.status, "isolation_failed")
        events = [
            event
            async for event in agent.stream_turn(
                "session-early-timeout",
                "hello",
                correlation_id="early-timeout",
            )
        ]
        self.assertEqual(events[-1].status, "isolation_failed")
        self.assertFalse(client.started)
        if self.audit_path.exists():
            messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(item.get("method") == "turn/start" for item in messages))

    async def test_mapping_is_committed_only_after_successful_terminal(self) -> None:
        _client, agent = self.make_stack()
        await agent.reserve_turn("session-interrupted", "execution-interrupted")
        stream_task = asyncio.create_task(
            self._collect(
                agent.stream_turn(
                    "session-interrupted",
                    "wait",
                    correlation_id="execution-interrupted",
                )
            )
        )
        await agent.interrupt_by_reference("session-interrupted", execution_id="execution-interrupted")
        await stream_task
        self.assertFalse(self.mapping_path.exists())
        events = [event async for event in agent.stream_turn("session-success", "hello")]
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        self.assertTrue(self.mapping_path.exists())
        payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["sessions"]["session-success"]["thread_id"], "thread-2")
        self.assertNotIn(self.temp_dir.name, json.dumps(payload))

    async def test_mapping_commit_failure_is_terminal_error_not_success(self) -> None:
        _client, agent = self.make_stack()

        async def fail_commit(*_args, **_kwargs):
            raise OSError("simulated mapping write failure")

        agent.thread_manager.commit_thread = fail_commit  # type: ignore[method-assign]
        events = [event async for event in agent.stream_turn("session-map-failure", "hello")]
        self.assertEqual(events[-1].type, AgentEventType.ERROR)
        self.assertEqual(events[-1].status, "mapping_commit_failed")
        self.assertEqual(events[-1].error, "Codex thread mapping could not be persisted")
        self.assertFalse(ThreadMappingStore(self.mapping_path).read())

    async def test_v1_mapping_is_read_and_cwd_fingerprint_is_one_way(self) -> None:
        self.mapping_path.write_text(
            json.dumps({"version": 1, "sessions": {"legacy-session": "legacy-thread"}}),
            encoding="utf-8",
        )
        store = ThreadMappingStore(self.mapping_path)
        self.assertEqual(store.read(), {"legacy-session": "legacy-thread"})
        await store.set("new-session", "new-thread", cwd=self.temp_dir.name)
        payload = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 2)
        self.assertNotIn(self.temp_dir.name, json.dumps(payload))
        self.assertTrue(payload["sessions"]["new-session"]["cwd_fingerprint"].startswith("sha256:"))
        self.assertEqual(payload["sessions"]["legacy-session"]["thread_id"], "legacy-thread")
        self.assertIs(payload["sessions"]["legacy-session"]["durable"], True)
        self.assertEqual(
            store.read(),
            {"legacy-session": "legacy-thread", "new-session": "new-thread"},
        )
        with self.assertRaises(ValueError):
            await store.set("bad-session", "bad-thread", cwd=str(self.mapping_path))

    async def test_mapping_corrupt_or_future_version_fails_closed_without_starting_server(self) -> None:
        store = ThreadMappingStore(self.mapping_path)
        self.mapping_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(CodexError) as corrupt:
            store.read()
        self.assertEqual(corrupt.exception.code, "mapping_corrupt")

        self.mapping_path.write_text(
            json.dumps({"version": 999, "sessions": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(CodexError) as future:
            store.read()
        self.assertEqual(future.exception.code, "mapping_version_unsupported")

        self.mapping_path.write_text("{not-json", encoding="utf-8")
        _client, agent = self.make_stack()
        events = [
            event
            async for event in agent.stream_turn(
                "session-corrupt-mapping",
                "hello",
                correlation_id="execution-corrupt-mapping",
            )
        ]
        self.assertEqual(events[-1].status, "start_failed")
        self.assertEqual(events[-1].error, "Codex thread mapping could not be read")
        self.assertFalse(self.client.started)  # type: ignore[union-attr]

    async def test_mapping_entries_are_closed_and_fingerprint_durable_bool_is_strict(self) -> None:
        cases = [
            {"version": True, "sessions": {}},
            {"version": 2.0, "sessions": {}},
            {"sessions": {}},
            {"version": 2},
            {"version": 2, "sessions": []},
            {"version": 1, "sessions": {}, "updated_at": 0},
            {"version": 1, "sessions": {}, "extra": None},
            {"version": 2, "sessions": {}, "extra": None},
            {"version": 2, "sessions": {"session": {"thread_id": "thread", "durable": "yes"}}},
            {"version": 2, "sessions": {"session": {"thread_id": "thread", "durable": True, "extra": 1}}},
            {"version": 2, "sessions": {"session": {"thread_id": "thread", "durable": True, "cwd_fingerprint": "sha256:not-hex"}}},
            {"version": 2, "sessions": {"session": {"thread_id": "thread"}}},
            {"version": 1, "sessions": {"session": {"thread_id": "thread", "durable": True}}},
        ]
        store = ThreadMappingStore(self.mapping_path)
        for payload in cases:
            self.mapping_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(CodexError) as context:
                store.read_entries()
            self.assertEqual(context.exception.code, "mapping_corrupt")
        with self.assertRaises(ValueError):
            await store.set("session", "thread", durable="yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await store.set("session", "thread", cwd_fingerprint="sha256:not-hex")

    async def test_mapping_json_rejects_duplicate_keys_at_every_object_level(self) -> None:
        payloads = {
            "root": '{"version":2,"version":2,"sessions":{}}',
            "sessions": (
                '{"version":2,"sessions":{'
                '"session":{"thread_id":"thread-a","durable":true},'
                '"session":{"thread_id":"thread-b","durable":true}}}'
            ),
            "entry": (
                '{"version":2,"sessions":{"session":{'
                '"thread_id":"thread-a","thread_id":"thread-b","durable":true}}}'
            ),
        }
        store = ThreadMappingStore(self.mapping_path)
        for level, payload in payloads.items():
            with self.subTest(level=level):
                self.mapping_path.write_text(payload, encoding="utf-8")
                with self.assertRaises(CodexError) as context:
                    store.read_entries()
                self.assertEqual(context.exception.code, "mapping_corrupt")

    async def test_mapping_updated_at_is_optional_nonnegative_strict_integer(self) -> None:
        store = ThreadMappingStore(self.mapping_path)
        for value in (True, -1, 1.0, "1", None):
            with self.subTest(value=value):
                self.mapping_path.write_text(
                    json.dumps({"version": 2, "sessions": {}, "updated_at": value}),
                    encoding="utf-8",
                )
                with self.assertRaises(CodexError) as context:
                    store.read_entries()
                self.assertEqual(context.exception.code, "mapping_corrupt")

        for payload in (
            {"version": 2, "sessions": {}},
            {"version": 2, "sessions": {}, "updated_at": 0},
        ):
            with self.subTest(valid=payload):
                self.mapping_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(store.read_entries(), {})

    async def test_concurrent_thread_claim_has_one_owner_under_forced_contention(self) -> None:
        _client, agent = self.make_stack()
        manager = agent.thread_manager
        ready = asyncio.Event()
        calls = 0

        async def same_thread_request(method, _params):
            nonlocal calls
            self.assertEqual(method, "thread/start")
            calls += 1
            if calls == 2:
                ready.set()
            await ready.wait()
            return {"thread": {"id": "thread-contention"}}

        manager.client.request = same_thread_request  # type: ignore[method-assign]
        results = await asyncio.gather(
            manager.ensure_thread("contention-a"),
            manager.ensure_thread("contention-b"),
            return_exceptions=True,
        )
        successes = [result for result in results if isinstance(result, str)]
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(successes, ["thread-contention"])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], CodexError)
        self.assertEqual(failures[0].code, "thread_ownership_conflict")  # type: ignore[union-attr]

    async def test_resume_mismatch_cannot_rebind_to_concurrent_owner(self) -> None:
        store = ThreadMappingStore(self.mapping_path)
        await store.set("resume-session", "thread-original")
        await store.set("other-session", "thread-foreign")
        calls: list[tuple[str, dict[str, object]]] = []

        class MismatchingResumeClient:
            async def request(self, method, params):
                calls.append((method, dict(params)))
                return {"thread": {"id": "thread-foreign"}}

        manager = ThreadManager(MismatchingResumeClient(), store)  # type: ignore[arg-type]
        with self.assertRaises(CodexError) as context:
            await manager.ensure_thread("resume-session")
        self.assertEqual(context.exception.code, "thread_ownership_conflict")
        self.assertEqual([method for method, _params in calls], ["thread/resume"])
        self.assertNotIn("turn/start", [method for method, _params in calls])
        self.assertEqual(
            store.read(),
            {
                "resume-session": "thread-original",
                "other-session": "thread-foreign",
            },
        )

    async def test_resume_wrong_response_cwd_preserves_mapping_and_never_starts_turn(self) -> None:
        client, agent = self.make_stack()
        workspace = Path(self.temp_dir.name).resolve()
        store = agent.thread_manager.store
        await store.set("resume-cwd", "thread-original", cwd=workspace)

        with self.assertRaises(CodexAmbiguousRequestError) as context:
            await agent.thread_manager.ensure_thread("resume-cwd", cwd=str(workspace))
        self.assertIsInstance(context.exception.__cause__, CodexSchemaValidationError)
        self.assertEqual(store.read()["resume-cwd"], "thread-original")
        messages = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        methods = [message.get("method") for message in messages]
        self.assertIn("thread/resume", methods)
        self.assertNotIn("thread/start", methods)
        self.assertNotIn("turn/start", methods)
        for _ in range(100):
            if not client.started:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(client.started)

    async def test_coding_sandboxes_are_rejected_until_approval_gateway(self) -> None:
        client, _agent = self.make_stack()
        with self.assertRaises(ValueError):
            ThreadManager(client, ThreadMappingStore(self.mapping_path), sandbox="workspace-write")
        with self.assertRaises(ValueError):
            ThreadManager(client, ThreadMappingStore(self.mapping_path), sandbox="danger-full-access")

    async def test_duplicate_thread_ownership_fails_closed_without_rewriting_history(self) -> None:
        store = ThreadMappingStore(self.mapping_path)
        await store.set("a-session", "thread-shared")
        await store.set("b-session", "thread-shared")
        client, agent = self.make_stack()
        manager = agent.thread_manager
        with self.assertRaises(CodexError) as context:
            await manager.ensure_thread("b-session")
        self.assertEqual(context.exception.code, "thread_ownership_conflict")
        self.assertFalse(client.started)
        self.assertEqual(store.read()["a-session"], "thread-shared")
        self.assertEqual(store.read()["b-session"], "thread-shared")

    async def test_concurrent_reservations_drop_foreign_exact_notifications(self) -> None:
        _client, agent = self.make_stack()
        first = await agent.reserve_turn("session-a", "execution-a")
        second = await agent.reserve_turn("session-b", "execution-b")
        self.assertIsNot(first, second)
        states = await agent._states_for_notification(  # noqa: SLF001
            "turn/started",
            {"threadId": "foreign-thread", "turnId": "foreign-turn"},
        )
        self.assertEqual(states, [])

    async def test_pre_response_exact_pair_replay_preserves_order_and_drops_stale_terminal(self) -> None:
        _client, agent = self.make_stack()
        state = await agent.reserve_turn("session-pair", "execution-pair")
        # The bridge has provisionally associated the thread, but the
        # turn/start response is still authoritative for the turn id.
        state.thread_id = "thread-pair"
        agent._pending_by_thread["thread-pair"] = state  # noqa: SLF001
        self.assertEqual(
            await agent._states_for_notification(  # noqa: SLF001
                "turn/started", {"threadId": "thread-pair", "turnId": "turn-old"}
            ),
            [],
        )
        self.assertEqual(
            await agent._states_for_notification(  # noqa: SLF001
                "turn/started", {"threadId": "thread-pair", "turnId": "turn-new"}
            ),
            [],
        )
        self.assertEqual(
            await agent._states_for_notification(  # noqa: SLF001
                "item/started",
                {"threadId": "thread-pair", "turnId": "turn-new", "item": {"id": "item-1", "type": "agentMessage", "phase": "final_answer"}},
            ),
            [],
        )
        self.assertEqual(
            await agent._states_for_notification(  # noqa: SLF001
                "item/agentMessage/delta",
                {"threadId": "thread-pair", "turnId": "turn-new", "itemId": "item-1", "delta": "hello"},
            ),
            [],
        )
        self.assertEqual(
            await agent._states_for_notification(  # noqa: SLF001
                "turn/completed",
                {"threadId": "thread-pair", "turnId": "turn-old", "turn": {"id": "turn-old", "status": "completed"}},
            ),
            [],
        )
        await agent._bind_turn_ids(state, "thread-pair", "turn-new")  # noqa: SLF001
        self.assertEqual(
            [event.type for event in list(state.queue._queue)],
            [AgentEventType.STARTED, AgentEventType.TOOL_ACTIVITY, AgentEventType.TEXT_DELTA],
        )  # noqa: SLF001
        self.assertIsNone(state.terminal)
        self.assertNotIn(("thread-pair", "turn-old"), agent._active)  # noqa: SLF001
        self.assertIn(("thread-pair", "turn-new"), agent._active)  # noqa: SLF001
        await agent._dispatch_notification(  # noqa: SLF001
            state,
            "turn/completed",
            {"method": "turn/completed", "params": {"threadId": "thread-pair", "turn": {"id": "turn-new", "status": "completed"}}},
        )
        self.assertIsNotNone(state.terminal)
        self.assertEqual(state.terminal.type, AgentEventType.FINISHED)
        await agent._finish_state(state)  # noqa: SLF001
        states = await agent._states_for_notification(  # noqa: SLF001
            "item/agentMessage/delta",
            {"delta": "ambiguous"},
        )
        self.assertEqual(states, [])

    async def test_pre_response_buffer_overflow_isolates_instead_of_dropping_current_terminal(self) -> None:
        _client, agent = self.make_stack()
        state = await agent.reserve_turn("session-buffer", "execution-buffer")
        state.thread_id = "thread-buffer"
        agent._pending_by_thread["thread-buffer"] = state  # noqa: SLF001
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        dispatcher = asyncio.create_task(agent._dispatch_loop(queue))  # noqa: SLF001
        try:
            for index in range(256):
                queue.put_nowait(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-buffer",
                            "turnId": f"stale-{index}",
                            "itemId": f"item-{index}",
                            "delta": "stale",
                        },
                    }
                )
            queue.put_nowait(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-buffer",
                        "turnId": "current-turn",
                        "turn": {"id": "current-turn", "status": "completed"},
                    },
                }
            )
            for _ in range(100):
                if state.terminal is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "isolation_failed")
            self.assertFalse(dispatcher.done())
        finally:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)

    async def test_process_exit_waits_for_pre_response_replay_lock(self) -> None:
        _client, agent = self.make_stack()
        state = await agent.reserve_turn("session-order", "execution-order")
        state.thread_id = "thread-order"
        agent._pending_by_thread["thread-order"] = state  # noqa: SLF001
        state.buffered_notifications.extend(
            [
                (
                    "item/started",
                    {
                        "threadId": "thread-order",
                        "turnId": "turn-order",
                        "item": {"id": "item-order", "type": "agentMessage", "phase": "final_answer"},
                    },
                ),
                (
                    "item/agentMessage/delta",
                    {
                        "threadId": "thread-order",
                        "turnId": "turn-order",
                        "itemId": "item-order",
                        "delta": "answer-before-exit",
                    },
                ),
            ]
        )
        await state.notification_lock.acquire()
        bind = asyncio.create_task(agent._bind_turn_ids(state, "thread-order", "turn-order"))  # noqa: SLF001
        await asyncio.sleep(0)
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        dispatcher = asyncio.create_task(agent._dispatch_loop(queue))  # noqa: SLF001
        queue.put_nowait({"method": INTERNAL_APP_SERVER_EXITED, "params": {}})
        state.notification_lock.release()
        try:
            await bind
            for _ in range(100):
                if state.terminal is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(state.terminal)
            self.assertEqual(state.terminal.status, "process_exit")
            queued = list(state.queue._queue)  # noqa: SLF001
            delta_index = next(index for index, event in enumerate(queued) if event.type is AgentEventType.TEXT_DELTA)
            terminal_index = next(index for index, event in enumerate(queued) if event.terminal)
            self.assertLess(delta_index, terminal_index)
        finally:
            if state.notification_lock.locked():
                state.notification_lock.release()
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)

    async def test_unknown_server_request_is_fail_closed_and_observable(self) -> None:
        _client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-approval", "approval")]
        denied = [event for event in events if event.status == "denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0].status, "denied")
        self.assertEqual(denied[0].type, AgentEventType.TOOL_ACTIVITY)
        self.assertEqual(events[-1].type, AgentEventType.FINISHED)
        messages = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        denial = next(item for item in messages if item.get("id") == 900 and "error" in item)
        self.assertEqual(denial["error"]["code"], -32001)
        self.assertNotIn("do not echo", json.dumps(denial))

    async def test_unknown_delta_is_not_spoken_until_item_completion_confirms_final(self) -> None:
        _client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-unknown", "unknown")]
        deltas = [event for event in events if event.type is AgentEventType.TEXT_DELTA]
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[0].speakable)
        self.assertEqual(deltas[0].phase, "final_answer")
        self.assertTrue(deltas[0].data.get("reconciled"))
        self.assertEqual(events[-1].final_text, "buffered-final")

    async def test_commentary_item_is_never_marked_speakable(self) -> None:
        _client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-commentary", "commentary")]
        deltas = [event for event in events if event.type is AgentEventType.TEXT_DELTA]
        self.assertEqual(len(deltas), 1)
        self.assertFalse(deltas[0].speakable)
        self.assertEqual(deltas[0].phase, "commentary")
        self.assertIsNone(events[-1].final_text)

    async def test_malformed_stdout_fails_closed(self) -> None:
        client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-malformed", "malformed")]
        self.assertEqual(events[-1].type, AgentEventType.ERROR)
        self.assertEqual(events[-1].status, "process_exit")
        self.assertEqual(client.last_error, "malformed stdout")

    async def test_process_exit_is_mapped_without_hanging_turn(self) -> None:
        client, agent = self.make_stack()
        events = [event async for event in agent.stream_turn("session-exit", "exit")]
        self.assertEqual(events[-1].type, AgentEventType.ERROR)
        self.assertEqual(events[-1].status, "process_exit")
        self.assertFalse(client.started)

    async def test_subscriber_queue_is_bounded_and_retains_delta_prefix(self) -> None:
        config = AppServerConfig(subscriber_queue_size=2)
        client = AppServerClient(config)
        queue = client.subscribe()
        await client._broadcast({"method": "one"})  # noqa: SLF001
        await client._broadcast({"method": "two"})  # noqa: SLF001
        await client._broadcast({"method": "three"})  # noqa: SLF001
        self.assertEqual(queue.maxsize, 2)
        self.assertEqual((await queue.get())["method"], "one")
        self.assertEqual((await queue.get())["method"], "two")
        client.unsubscribe(queue)

    async def test_subscriber_queue_waiter_drains_normal_and_priority_entries(self) -> None:
        config = AppServerConfig(subscriber_queue_size=1)
        client = AppServerClient(config)
        queue = client.subscribe()
        waiter = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        await client._broadcast({"method": "item/agentMessage/delta", "params": {"delta": "first"}})  # noqa: SLF001
        self.assertEqual((await waiter)["method"], "item/agentMessage/delta")
        await client._broadcast({"method": "item/agentMessage/delta", "params": {"delta": "prefix"}})  # noqa: SLF001
        await client._broadcast(  # noqa: SLF001
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread",
                    "turn": {"id": "turn", "items": [], "status": "completed"},
                },
            }
        )
        self.assertEqual((await queue.get())["method"], "item/agentMessage/delta")
        self.assertEqual((await queue.get())["method"], "turn/completed")
        client.unsubscribe(queue)

    async def test_two_server_request_denials_keep_distinct_safe_correlations_in_tiny_queue(self) -> None:
        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        queue = client.subscribe()
        process = object()
        client._process = process  # type: ignore[assignment]  # noqa: SLF001
        client._process_generation = 1  # noqa: SLF001
        original_send = client._send  # noqa: SLF001

        async def discard_wire_denial(_message, **_kwargs):
            return None

        client._send = discard_wire_denial  # type: ignore[method-assign]  # noqa: SLF001
        try:
            await client._deny_server_request(  # noqa: SLF001 - denial queue contract
                {
                    "id": "raw-secret-request-one",
                    "method": "future/approval",
                    "params": {"threadId": "thread-1", "turnId": "turn-1"},
                },
                business_known=False,
                process=process,  # type: ignore[arg-type]
                generation=1,
            )
            await client._deny_server_request(  # noqa: SLF001 - denial queue contract
                {
                    "id": "raw-secret-request-two",
                    "method": "future/approval",
                    "params": {"threadId": "thread-1", "turnId": "turn-1"},
                },
                business_known=False,
                process=process,  # type: ignore[arg-type]
                generation=1,
            )
            denied = [queue.get_nowait(), queue.get_nowait()]
            correlations = [message["params"]["requestId"] for message in denied]
            self.assertEqual(len(set(correlations)), 2)
            self.assertTrue(
                all(
                    len(value) == len("server-request-") + 16
                    and value.startswith("server-request-")
                    for value in correlations
                )
            )
            serialized = json.dumps(denied)
            self.assertNotIn("raw-secret-request-one", serialized)
            self.assertNotIn("raw-secret-request-two", serialized)
            self.assertFalse(queue.overflowed)
        finally:
            client._send = original_send  # type: ignore[method-assign]  # noqa: SLF001
            client._process = None  # noqa: SLF001
            client.unsubscribe(queue)

    async def test_flood_drops_progress_but_preserves_terminal_and_denial(self) -> None:
        config = AppServerConfig(subscriber_queue_size=2)
        client = AppServerClient(config)
        queue = client.subscribe()
        for index in range(50):
            await client._broadcast(  # noqa: SLF001
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": str(index), "delta": "progress"},
                }
            )
        await client._broadcast(
            {
                "method": "server/request/denied",
                "params": {"requestId": "approval-1", "method": "approval"},
            }
        )
        for index in range(50, 100):
            await client._broadcast(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": str(index), "delta": "progress"},
                }
            )
        await client._broadcast(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            }
        )
        drained = [queue.get_nowait() for _ in range(queue.qsize())]
        methods = [message["method"] for message in drained]
        self.assertIn("server/request/denied", methods)
        self.assertIn("turn/completed", methods)
        self.assertLessEqual(queue.qsize(), queue.maxsize + max(4, queue.maxsize))
        client.unsubscribe(queue)

    async def test_critical_item_snapshots_coalesce_only_by_item_identity(self) -> None:
        config = AppServerConfig(subscriber_queue_size=1)
        client = AppServerClient(config)
        queue = client.subscribe()
        for item_id in ("item-1", "item-2", "item-3", "item-4", "item-5"):
            await client._broadcast(  # noqa: SLF001
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"id": item_id, "type": "agentMessage", "text": item_id},
                    },
                }
            )
        drained = [queue.get_nowait() for _ in range(queue.qsize())]
        completed_ids = {
            message["params"]["item"]["id"]
            for message in drained
            if message.get("method") == "item/completed"
        }
        self.assertTrue({"item-1", "item-2"}.issubset(completed_ids))
        self.assertLessEqual(queue.qsize(), queue.maxsize + max(4, queue.maxsize))
        client.unsubscribe(queue)

    async def test_critical_notifications_preserve_wire_order_across_reserved_lane(self) -> None:
        """A later turn terminal must not overtake an earlier item snapshot."""

        config = AppServerConfig(subscriber_queue_size=1)
        client = AppServerClient(config)
        queue = client.subscribe()
        await client._broadcast(  # noqa: SLF001
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "item-1", "type": "agentMessage", "text": "answer"},
                },
            }
        )
        await client._broadcast(  # noqa: SLF001
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )
        first = queue.get_nowait()
        second = queue.get_nowait()
        self.assertEqual(first["method"], "item/completed")
        self.assertEqual(second["method"], "turn/completed")
        client.unsubscribe(queue)

    async def test_conflicting_same_turn_terminal_preserves_first_and_isolates(self) -> None:
        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        queue = client.subscribe()

        def completed(
            turn_id: str,
            status: str = "completed",
            emitted_at_ms: int | None = None,
        ) -> dict[str, object]:
            message: dict[str, object] = {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": turn_id, "items": [], "status": status},
                },
            }
            if emitted_at_ms is not None:
                message["emittedAtMs"] = emitted_at_ms
            return message

        queue.put_notification(
            {"method": "item/agentMessage/delta", "params": {"delta": "prefix"}}
        )
        originals = [
            completed(f"turn-{index}", emitted_at_ms=index + 1)
            for index in range(queue._priority_limit)  # noqa: SLF001
        ]
        for message in originals:
            client._schema_validator.validate_server_notification(message)  # noqa: SLF001
            queue.put_notification(message)
        self.assertFalse(queue.overflowed)

        # A separately decoded but semantic-identical duplicate may coalesce;
        # both real nested turn identities remain distinct in the lane.
        duplicate = completed("turn-0", emitted_at_ms=999)
        queue.put_notification(duplicate)
        self.assertFalse(queue.overflowed)
        self.assertEqual(
            {
                entry[1]["params"]["turn"]["id"]
                for entry in queue._priority  # noqa: SLF001
            },
            {f"turn-{index}" for index in range(queue._priority_limit)},  # noqa: SLF001
        )

        isolate_calls = 0
        original_isolate = client.isolate

        async def counted_isolate(_reason: str = "") -> None:
            nonlocal isolate_calls
            isolate_calls += 1

        conflict = completed("turn-0", "failed")
        client._schema_validator.validate_server_notification(conflict)  # noqa: SLF001
        client.isolate = counted_isolate  # type: ignore[method-assign]
        try:
            await client._broadcast(conflict)  # noqa: SLF001
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]

        self.assertEqual(isolate_calls, 1)
        self.assertTrue(queue.overflowed)
        first = next(
            entry[1]
            for entry in queue._priority  # noqa: SLF001
            if entry[1]["params"]["turn"]["id"] == "turn-0"
        )
        self.assertEqual(first["params"]["turn"]["status"], "completed")
        client.unsubscribe(queue)

    async def test_conflicting_same_login_terminal_preserves_first_and_isolates(self) -> None:
        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        queue = client.subscribe()
        queue.put_notification(
            {"method": "item/agentMessage/delta", "params": {"delta": "prefix"}}
        )
        first = {
            "method": "account/login/completed",
            "params": {
                "loginId": "login-1",
                "success": True,
                "error": None,
                "onboardingEntrypoint": None,
            },
        }
        client._schema_validator.validate_server_notification(first)  # noqa: SLF001
        queue.put_notification(first)
        for index in range(queue._priority_limit - 1):  # noqa: SLF001
            queue.put_notification(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": f"item-login-fill-{index}",
                            "type": "agentMessage",
                            "text": "x",
                        },
                    },
                }
            )
        queue.put_notification(dict(first, params=dict(first["params"])))
        self.assertFalse(queue.overflowed)

        conflict = {
            "method": "account/login/completed",
            "params": {
                "loginId": "login-1",
                "success": False,
                "error": "provider failure",
                "onboardingEntrypoint": None,
            },
        }
        client._schema_validator.validate_server_notification(conflict)  # noqa: SLF001
        isolate_calls = 0
        original_isolate = client.isolate

        async def counted_isolate(_reason: str = "") -> None:
            nonlocal isolate_calls
            isolate_calls += 1

        client.isolate = counted_isolate  # type: ignore[method-assign]
        try:
            await client._broadcast(conflict)  # noqa: SLF001
        finally:
            client.isolate = original_isolate  # type: ignore[method-assign]

        self.assertEqual(isolate_calls, 1)
        self.assertTrue(queue.overflowed)
        retained = next(
            entry[1]
            for entry in queue._priority  # noqa: SLF001
            if entry[1].get("method") == "account/login/completed"
        )
        self.assertTrue(retained["params"]["success"])
        client.unsubscribe(queue)

    async def test_critical_snapshot_follows_queued_delta_prefix_without_evicting_it(self) -> None:
        config = AppServerConfig(subscriber_queue_size=2)
        client = AppServerClient(config)
        queue = client.subscribe()
        for delta in ("A", "B", "C"):
            await client._broadcast(  # noqa: SLF001
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "item-1", "delta": delta},
                }
            )
        await client._broadcast(  # noqa: SLF001
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "item-1", "type": "agentMessage", "text": "ABC"},
                },
            }
        )
        drained = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual(
            [message.get("params", {}).get("delta") for message in drained[:2]],
            ["A", "B"],
        )
        self.assertEqual(drained[-1]["method"], "item/completed")
        client.unsubscribe(queue)

    async def test_process_exit_force_delivers_when_terminal_put_fills_both_lanes(self) -> None:
        """The terminal write itself may discover the queue is already full."""

        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        queue = client.subscribe()
        try:
            queue.put_notification({"method": "item/agentMessage/delta", "params": {"delta": "prefix"}})
            for index in range(queue._priority_limit):  # noqa: SLF001 - exact lane-boundary probe
                queue.put_notification(
                    {
                        "method": "item/completed",
                        "params": {"item": {"id": f"item-{index}", "type": "agentMessage", "text": "x"}},
                    }
                )
            self.assertFalse(queue.overflowed)
            await client._mark_exit("exact-full")  # noqa: SLF001
            self.assertFalse(queue.overflowed)
            self.assertEqual(queue.qsize(), 1)
            self.assertEqual(queue.get_nowait()["method"], INTERNAL_APP_SERVER_EXITED)
        finally:
            client.unsubscribe(queue)

    async def test_isolation_failed_force_delivers_when_terminal_put_fills_both_lanes(self) -> None:
        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        queue = client.subscribe()
        try:
            queue.put_notification({"method": "item/agentMessage/delta", "params": {"delta": "prefix"}})
            for index in range(queue._priority_limit):  # noqa: SLF001 - exact lane-boundary probe
                queue.put_notification(
                    {
                        "method": "item/completed",
                        "params": {"item": {"id": f"item-{index}", "type": "agentMessage", "text": "x"}},
                    }
                )
            self.assertFalse(queue.overflowed)
            await client._mark_isolation_failed()  # noqa: SLF001
            self.assertFalse(queue.overflowed)
            self.assertEqual(queue.qsize(), 1)
            self.assertEqual(
                queue.get_nowait()["method"],
                INTERNAL_APP_SERVER_ISOLATION_FAILED,
            )
        finally:
            client.unsubscribe(queue)

    async def test_process_terminal_reaches_fast_and_slow_subscribers(self) -> None:
        """A slow full subscriber cannot make a faster subscriber lose the terminal."""

        client = AppServerClient(AppServerConfig(subscriber_queue_size=1))
        slow = client.subscribe(maxsize=1)
        fast = client.subscribe(maxsize=4)
        fast_events: list[dict[str, object]] = []

        async def consume_fast() -> None:
            while True:
                message = await fast.get()
                fast_events.append(message)
                if message.get("method") == INTERNAL_APP_SERVER_EXITED:
                    return

        consumer = asyncio.create_task(consume_fast())
        try:
            await asyncio.sleep(0)
            await client._broadcast({"method": "item/agentMessage/delta", "params": {"delta": "prefix"}})  # noqa: SLF001
            for index in range(slow._priority_limit):  # noqa: SLF001 - exact slow-lane boundary
                await client._broadcast(  # noqa: SLF001
                    {
                        "method": "item/completed",
                        "params": {"item": {"id": f"item-{index}", "type": "agentMessage", "text": "x"}},
                    }
                )
            await client._mark_exit("different-rates")  # noqa: SLF001
            await asyncio.wait_for(consumer, timeout=1)
            self.assertEqual(fast_events[-1]["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertEqual(slow.get_nowait()["method"], INTERNAL_APP_SERVER_EXITED)
            self.assertFalse(slow.overflowed)
        finally:
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            client.unsubscribe(slow)
            client.unsubscribe(fast)

    @staticmethod
    async def _collect(iterator):
        return [event async for event in iterator]


if __name__ == "__main__":
    unittest.main()
