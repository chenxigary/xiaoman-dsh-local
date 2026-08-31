"""Bridge import/HTTP smoke tests that never start a Codex process."""

from __future__ import annotations

import unittest
import asyncio
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    import bridge.voice_bridge as voice_bridge
    from agents.codex import AgentEvent, AgentEventType, CodexCompatibilityError, JsonRpcError
except Exception:  # pragma: no cover - optional bridge dependencies
    TestClient = None  # type: ignore[assignment]
    voice_bridge = None  # type: ignore[assignment]
    JsonRpcError = None  # type: ignore[assignment]
    AgentEvent = AgentEventType = None  # type: ignore[assignment]
    CodexCompatibilityError = None  # type: ignore[assignment]


@unittest.skipUnless(TestClient is not None and voice_bridge is not None, "FastAPI bridge dependencies unavailable")
class CodexBridgeSmokeTests(unittest.TestCase):
    def test_import_and_health_do_not_spawn_app_server(self) -> None:
        assert voice_bridge is not None
        # The health route must remain non-starting even if a real codex binary
        # is on PATH.  Reset only test-owned globals because the module can be
        # imported by another test process in the same interpreter.
        voice_bridge._CODEX_CLIENT = None
        voice_bridge._CODEX_AUTH = None
        voice_bridge._CODEX_AGENT = None
        voice_bridge._CODEX_SHUTTING_DOWN = False
        with TestClient(voice_bridge.app) as client:
            response = client.get("/api/codex/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "disabled")
        self.assertFalse(body["codex"]["enabled"])
        self.assertFalse(body["codex"]["started"])
        self.assertNotIn("pid", body["codex"])
        self.assertNotIn("command", body["codex"])
        self.assertNotIn("runtime_state", body["codex"])
        self.assertIsNone(voice_bridge._CODEX_CLIENT)
        self.assertIsNone(voice_bridge._CODEX_AGENT)

    def test_browser_errors_never_echo_app_server_details(self) -> None:
        assert voice_bridge is not None and JsonRpcError is not None
        status, payload = voice_bridge._codex_error_payload(  # noqa: SLF001
            JsonRpcError(-32602, "secret prompt /Users/private/workspace/token=abc")
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"code": "codex_error", "message": "Codex operation failed"})
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("/Users", str(payload))

    def test_login_start_requires_exact_operation_id_and_returns_safe_contract(self) -> None:
        assert voice_bridge is not None
        operation_id = "11111111-1111-4111-8111-111111111111"

        class SafeState:
            def to_dict(self):
                return {
                    "login_id": "login-safe",
                    "status": "pending",
                    "auth_url": "https://auth.openai.com/oauth/authorize?client_id=safe",
                }

        class FakeAuth:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def login_start(self, supplied_operation_id):
                self.calls.append(supplied_operation_id)
                return SafeState()

        fake_auth = FakeAuth()

        async def fake_ensure():
            return None, fake_auth, None

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                for _ in range(2):
                    response = client.post(
                        "/api/codex/auth/login/start",
                        json={"operation_id": operation_id},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.json(),
                        {
                            "operation_id": operation_id,
                            "login_id": "login-safe",
                            "status": "pending",
                            "auth_url": "https://auth.openai.com/oauth/authorize?client_id=safe",
                        },
                    )
        self.assertEqual(fake_auth.calls, [operation_id, operation_id])

    def test_login_start_rejects_noncanonical_or_open_body_before_backend(self) -> None:
        assert voice_bridge is not None
        ensure_calls = 0
        valid = "11111111-1111-4111-8111-111111111111"

        async def forbidden_ensure():
            nonlocal ensure_calls
            ensure_calls += 1
            raise AssertionError("invalid login operation reached backend")

        invalid_json_bodies = (
            b"{}",
            b"null",
            b'{"operation_id":true}',
            b'{"operation_id":NaN}',
            b'{"operation_id":"11111111-1111-1111-8111-111111111111"}',
            b'{"operation_id":"11111111-1111-4111-8111-11111111111A"}',
            b'{"operation_id":"11111111-1111-4111-8111-111111111111","extra":1}',
            (
                b'{"operation_id":"'
                + valid.encode("ascii")
                + b'","operation_id":"'
                + valid.encode("ascii")
                + b'"}'
            ),
        )

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with patch.object(voice_bridge, "_ensure_codex", forbidden_ensure):
            with TestClient(voice_bridge.app) as client:
                for body in invalid_json_bodies:
                    with self.subTest(body=body):
                        response = client.post(
                            "/api/codex/auth/login/start",
                            content=body,
                            headers={"content-type": "application/json"},
                        )
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(
                            response.json()["detail"],
                            {"code": "invalid_request", "message": "invalid request"},
                        )
                oversized = client.post(
                    "/api/codex/auth/login/start",
                    content=b'{' + (b'"x":0,' * 80) + b'"operation_id":"' + valid.encode("ascii") + b'"}',
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(oversized.status_code, 413)
        self.assertEqual(ensure_calls, 0)

    def test_bridge_forces_read_only_preview_sandbox(self) -> None:
        assert voice_bridge is not None
        cfg = voice_bridge.load_config()
        self.assertEqual(cfg["codex"]["sandbox"], "read-only")
        self.assertEqual(cfg["codex"]["approval_policy"], "never")
        self.assertFalse(voice_bridge.CODEX_TURN_EXECUTION_ENABLED)
        self.assertEqual(cfg["codex"]["execution_home"], "runtime/codex-execution-home")

    def test_bridge_config_rejects_nonfinite_json_numbers(self) -> None:
        assert voice_bridge is not None
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "bridge-config.json"
                config_path.write_text(
                    '{"codex":{"startup_timeout_sec":' + constant + "}}",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError) as context:
                    voice_bridge.load_config(config_path)
                self.assertEqual(
                    str(context.exception),
                    "bridge config contains a non-finite JSON number",
                )

    def test_production_cli_version_override_is_rejected_before_client_construction(self) -> None:
        assert voice_bridge is not None and CodexCompatibilityError is not None
        cfg = voice_bridge.CONFIG["codex"]
        original = cfg.get("expected_cli_version")
        original_enabled = cfg.get("enabled")
        cfg["enabled"] = True

        async def exercise() -> None:
            for value in (None, "", "0.148.0-alpha.8", "0.149.0-alpha.4.1 ", 148):
                with self.subTest(value=value):
                    cfg["expected_cli_version"] = value
                    voice_bridge._CODEX_CLIENT = None
                    voice_bridge._CODEX_AUTH = None
                    voice_bridge._CODEX_AGENT = None
                    voice_bridge._CODEX_SHUTTING_DOWN = False
                    with patch.object(voice_bridge, "AppServerClient") as constructor:
                        with self.assertRaises(CodexCompatibilityError) as context:
                            await voice_bridge._ensure_codex()
                        self.assertEqual(context.exception.code, "codex_version_unsupported")
                        constructor.assert_not_called()

            # A previously constructed bundle cannot turn a later invalid
            # production configuration into an implicit bypass.
            cfg["expected_cli_version"] = None
            voice_bridge._CODEX_CLIENT = object()
            voice_bridge._CODEX_AUTH = object()
            voice_bridge._CODEX_AGENT = object()
            with patch.object(voice_bridge, "AppServerClient") as constructor:
                with self.assertRaises(CodexCompatibilityError):
                    await voice_bridge._ensure_codex()
                constructor.assert_not_called()

        try:
            asyncio.run(exercise())
        finally:
            cfg["expected_cli_version"] = original
            cfg["enabled"] = original_enabled
            voice_bridge._CODEX_CLIENT = None
            voice_bridge._CODEX_AUTH = None
            voice_bridge._CODEX_AGENT = None

    def test_invalid_raw_numeric_config_never_reaches_client_construction(self) -> None:
        assert voice_bridge is not None
        cfg = voice_bridge.CONFIG["codex"]
        original = dict(cfg)
        cfg["enabled"] = True
        invalid = {
            "startup_timeout_sec": (True, None, "15", float("nan"), 121, 10**100),
            "request_timeout_sec": (False, None, "30", float("nan"), 301, 10**100),
            "shutdown_timeout_sec": (True, None, "3", float("nan"), 31, 10**100),
            "turn_timeout_sec": (False, None, "1800", float("nan"), 7201, 10**100),
            "subscriber_queue_size": (True, None, "256", float("nan"), 256.0, 4097, 10**100),
        }

        async def exercise() -> None:
            for field, values in invalid.items():
                valid_value = original.get(field)
                for value in values:
                    with self.subTest(field=field, value=value):
                        cfg[field] = value
                        voice_bridge._CODEX_CLIENT = None
                        voice_bridge._CODEX_AUTH = None
                        voice_bridge._CODEX_AGENT = None
                        voice_bridge._CODEX_SHUTTING_DOWN = False
                        with patch.object(voice_bridge, "AppServerClient") as constructor:
                            with self.assertRaises(ValueError):
                                await voice_bridge._ensure_codex()
                            constructor.assert_not_called()
                        if valid_value is None:
                            cfg.pop(field, None)
                        else:
                            cfg[field] = valid_value

        try:
            asyncio.run(exercise())
        finally:
            cfg.clear()
            cfg.update(original)
            voice_bridge._CODEX_CLIENT = None
            voice_bridge._CODEX_AUTH = None
            voice_bridge._CODEX_AGENT = None
            voice_bridge._CODEX_SHUTTING_DOWN = False

    def test_authenticated_turn_start_is_accepted_after_reservation(self) -> None:
        assert voice_bridge is not None

        class FakeAgent:
            reserve_calls = 0
            reservations: set[tuple[str, str]] = set()

            async def reserve_turn(self, session_id, correlation_id, **kwargs):
                key = (session_id, correlation_id)
                if key not in self.reservations:
                    self.reservations.add(key)
                    self.reserve_calls += 1

            async def cancel_reservation(self, *args, **kwargs):
                return True

        fake = FakeAgent()

        async def fake_ensure():
            return None, None, fake

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with (
            patch.object(voice_bridge, "_ensure_codex", fake_ensure),
            patch.object(voice_bridge, "CODEX_TURN_EXECUTION_ENABLED", True),
        ):
            with TestClient(voice_bridge.app) as client:
                with client.websocket_connect("/api/codex/ws") as ws:
                    ws.send_json({
                        "type": "turn/start",
                        "session_id": "blocked-session",
                        "execution_id": "blocked-exec",
                        "text": "test -f README.md",
                    })
                    response = ws.receive_json()
                    self.assertEqual(response["type"], "accepted")
                    self.assertNotIn("README", str(response))
        self.assertEqual(fake.reserve_calls, 1)

    def test_http_and_ws_isolate_ack_distinguish_release_from_real_kill(self) -> None:
        assert voice_bridge is not None

        class FakeAgent:
            def __init__(self, outcome: str) -> None:
                self.outcome = outcome
                self.calls: list[tuple[str, str, str | None, str | None]] = []

            async def isolate_turn(
                self,
                session_id,
                execution_id,
                *,
                thread_id=None,
                turn_id=None,
                reason=None,
            ):
                self.calls.append((session_id, execution_id, thread_id, turn_id))
                return self.outcome

        for outcome in ("released", "isolated"):
            with self.subTest(outcome=outcome):
                fake = FakeAgent(outcome)

                async def fake_ensure():
                    return None, None, fake

                voice_bridge._CODEX_SHUTTING_DOWN = False
                with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
                    with TestClient(voice_bridge.app) as client:
                        response = client.post(
                            "/api/codex/turn/isolate",
                            json={
                                "session_id": "http-session",
                                "execution_id": "http-exec",
                                "thread_id": "http-thread",
                                "turn_id": "http-turn",
                            },
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["status"], outcome)

                        with client.websocket_connect("/api/codex/ws") as ws:
                            ws.send_json(
                                {
                                    "type": "turn/isolate",
                                    "session_id": "ws-session",
                                    "execution_id": "ws-exec",
                                    "thread_id": "ws-thread",
                                    "turn_id": "ws-turn",
                                }
                            )
                            payload = ws.receive_json()
                            self.assertEqual(payload["type"], "isolate_result")
                            self.assertEqual(payload["status"], outcome)
                self.assertEqual(
                    fake.calls,
                    [
                        ("http-session", "http-exec", "http-thread", "http-turn"),
                        ("ws-session", "ws-exec", "ws-thread", "ws-turn"),
                    ],
                )

    def test_control_models_reject_extra_and_incomplete_identity_before_provider(self) -> None:
        assert voice_bridge is not None
        ensure_calls = 0

        async def forbidden_ensure():
            nonlocal ensure_calls
            ensure_calls += 1
            raise AssertionError("invalid control request reached provider")

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with patch.object(voice_bridge, "_ensure_codex", forbidden_ensure):
            with TestClient(voice_bridge.app) as client:
                extra = client.post(
                    "/api/codex/turn/isolate",
                    json={
                        "session_id": "session",
                        "execution_id": "execution",
                        "unexpected": "ignored-before",
                    },
                )
                incomplete = client.post(
                    "/api/codex/turn/isolate",
                    json={
                        "session_id": "session",
                        "execution_id": "execution",
                        "thread_id": "thread",
                    },
                )
                oversized = client.post(
                    "/api/codex/turn/isolate",
                    json={
                        "session_id": "s" * 513,
                        "execution_id": "execution",
                    },
                )
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(incomplete.status_code, 400)
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(ensure_calls, 0)

    def test_model_catalog_is_host_only_and_returns_the_bounded_provider_projection(self) -> None:
        assert voice_bridge is not None
        catalog = {
            "models": [{
                "id": "gpt-5.4-mini",
                "displayName": "GPT-5.4 Mini",
                "description": "Small, fast, and cost-efficient.",
                "defaultReasoningEffort": "low",
                "supportedReasoningEfforts": [{"id": "low", "description": "Fast responses"}],
                "serviceTiers": [],
            }],
        }

        class FakeAgent:
            async def list_models(self):
                return catalog

        async def fake_ensure():
            return None, None, FakeAgent()

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with (
            patch.object(voice_bridge, "_ensure_codex", fake_ensure),
            patch.object(voice_bridge, "CODEX_TURN_EXECUTION_ENABLED", True),
        ):
            with TestClient(voice_bridge.app) as client:
                response = client.get("/api/codex/models")
                browser = client.get(
                    "/api/codex/models",
                    headers={"origin": "http://127.0.0.1:3080"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), catalog)
        self.assertEqual(browser.status_code, 403)

    def test_shutdown_reaches_auth_and_exact_client_close_after_agent_failure(self) -> None:
        assert voice_bridge is not None
        calls: list[str] = []

        class FailingAgent:
            async def close(self):
                calls.append("agent")
                raise RuntimeError("must not be reflected")

        class FakeAuth:
            async def close(self):
                calls.append("auth")

        class FakeConfig:
            shutdown_timeout = 0.1

        class FakeClient:
            config = FakeConfig()

            async def close(self):
                calls.append("client")

        async def exercise() -> None:
            voice_bridge._CODEX_CLIENT = FakeClient()
            voice_bridge._CODEX_AUTH = FakeAuth()
            voice_bridge._CODEX_AGENT = FailingAgent()
            voice_bridge._CODEX_SHUTTING_DOWN = False
            with self.assertRaisesRegex(RuntimeError, r"\ACodex shutdown cleanup failed\Z"):
                await voice_bridge._shutdown_codex()

        try:
            asyncio.run(exercise())
        finally:
            voice_bridge._CODEX_CLIENT = None
            voice_bridge._CODEX_AUTH = None
            voice_bridge._CODEX_AGENT = None
            voice_bridge._CODEX_SHUTTING_DOWN = False
        self.assertEqual(calls, ["agent", "auth", "client"])

    def test_ws_release_frame_requires_exact_provider_release_authority(self) -> None:
        assert voice_bridge is not None and AgentEvent is not None and AgentEventType is not None

        class FakeConfig:
            shutdown_timeout = 0.1

        class FakeClient:
            config = FakeConfig()

        class FakeAgent:
            client = FakeClient()

            def __init__(self, release_status: str) -> None:
                self.release_status = release_status
                self.release_checked = threading.Event()
                self.isolate_calls = 0

            async def reserve_turn(self, *_args, **_kwargs):
                return None

            async def stream_turn(self, session_id, _text, *, correlation_id=None, cwd=None, **_selection):
                _ = cwd
                event_type = (
                    AgentEventType.FINISHED
                    if self.release_status == "released"
                    else AgentEventType.ERROR
                )
                yield AgentEvent(
                    event_type,
                    session_id=session_id,
                    thread_id="thread-release-proof",
                    turn_id="turn-release-proof",
                    correlation_id=correlation_id,
                    status=(
                        "completed"
                        if self.release_status == "released"
                        else "isolation_failed"
                    ),
                    error=(
                        None
                        if self.release_status == "released"
                        else "codex app-server process isolation failed"
                    ),
                )

            async def wait_for_execution_release(
                self,
                session_id,
                execution_id,
                *,
                timeout=0.0,
            ):
                self.asserted_identity = (session_id, execution_id)
                self.asserted_timeout = timeout
                self.release_checked.set()
                return self.release_status

            async def isolate_turn(self, *_args, **_kwargs):
                self.isolate_calls += 1
                return "isolated"

            async def cancel_reservation(self, *_args, **_kwargs):
                return False

        for release_status in ("released", "poisoned"):
            with self.subTest(release_status=release_status):
                fake = FakeAgent(release_status)

                async def fake_ensure():
                    return fake.client, None, fake

                voice_bridge._CODEX_SHUTTING_DOWN = False
                with (
                    patch.object(voice_bridge, "_ensure_codex", fake_ensure),
                    patch.object(voice_bridge, "CODEX_TURN_EXECUTION_ENABLED", True),
                    TestClient(voice_bridge.app) as client,
                    client.websocket_connect("/api/codex/ws") as ws,
                ):
                    ws.send_json(
                        {
                            "type": "turn/start",
                            "session_id": "session-release-proof",
                            "execution_id": "execution-release-proof",
                            "text": "bounded fake turn",
                        }
                    )
                    self.assertEqual(ws.receive_json()["type"], "accepted")
                    terminal = ws.receive_json()
                    self.assertEqual(
                        terminal["status"],
                        "completed" if release_status == "released" else "isolation_failed",
                    )
                    self.assertTrue(fake.release_checked.wait(timeout=1.0))
                    self.assertEqual(
                        fake.asserted_identity,
                        ("session-release-proof", "execution-release-proof"),
                    )
                    if release_status == "released":
                        released = ws.receive_json()
                        self.assertEqual(released["type"], "turn/released")
                        self.assertEqual(
                            released["execution_id"],
                            "execution-release-proof",
                        )
                    else:
                        # A control round-trip after the stream task has
                        # completed proves no release frame was queued behind
                        # the isolation_failed terminal.
                        ws.send_json(
                            {"type": "initialize", "session_id": "session-release-proof"}
                        )
                        self.assertEqual(ws.receive_json()["type"], "ready")
                self.assertEqual(
                    fake.isolate_calls,
                    0 if release_status == "released" else 1,
                )

    def test_accepted_immediate_close_writes_ledger_for_http_release_ack(self) -> None:
        assert voice_bridge is not None

        class FakeConfig:
            shutdown_timeout = 0.1

        class FakeClient:
            config = FakeConfig()

        class FakeAgent:
            client = FakeClient()

            def __init__(self) -> None:
                self.reservations: set[tuple[str, str]] = set()
                self.released: set[tuple[str, str]] = set()
                self.stream_entered = False
                self.isolate_calls = 0

            async def reserve_turn(self, session_id, correlation_id, *, cwd=None):
                _ = cwd
                self.reservations.add((session_id, correlation_id))

            async def stream_turn(self, *_args, **_kwargs):
                self.stream_entered = True
                if False:  # pragma: no cover - retain async-generator shape
                    yield None

            async def isolate_turn(
                self,
                session_id,
                execution_id,
                *,
                thread_id=None,
                turn_id=None,
                reason=None,
            ):
                _ = (thread_id, turn_id, reason)
                self.isolate_calls += 1
                key = (session_id, execution_id)
                if key in self.released:
                    return "released"
                if key not in self.reservations:
                    raise RuntimeError("unknown reservation")
                self.reservations.remove(key)
                self.released.add(key)
                return "released"

            async def cancel_reservation(self, session_id, correlation_id):
                # The exact isolate path has already written the ledger; the
                # legacy finalizer must not turn it back into unknown.
                return self.reservations.discard((session_id, correlation_id)) is not None

        fake = FakeAgent()

        async def fake_ensure():
            return fake.client, None, fake

        real_create_task = asyncio.create_task
        deferred_tasks: list[asyncio.Task] = []

        def defer_nested_run_turn(coro, *args, **kwargs):
            code = getattr(coro, "cr_code", None)
            if code is None or code.co_name != "run_turn":
                return real_create_task(coro, *args, **kwargs)

            async def hold_until_cancelled():
                try:
                    await asyncio.Event().wait()
                finally:
                    coro.close()

            task = real_create_task(hold_until_cancelled(), *args, **kwargs)
            deferred_tasks.append(task)
            return task

        voice_bridge._CODEX_SHUTTING_DOWN = False
        with (
            patch.object(voice_bridge, "_ensure_codex", fake_ensure),
            patch.object(voice_bridge, "CODEX_TURN_EXECUTION_ENABLED", True),
            patch.object(voice_bridge.asyncio, "create_task", side_effect=defer_nested_run_turn),
            TestClient(voice_bridge.app) as client,
        ):
            ws = client.websocket_connect("/api/codex/ws")
            ws.__enter__()
            ws.send_json(
                {
                    "type": "turn/start",
                    "session_id": "session-pristine-close",
                    "execution_id": "execution-pristine-close",
                    "text": "bounded fake turn",
                }
            )
            self.assertEqual(ws.receive_json()["type"], "accepted")
            ws.close()
            ws.__exit__(None, None, None)

            # Simulate a lost WS release ACK: Host retries the exact identity
            # over its host-only HTTP fallback and receives the ledger result.
            response = client.post(
                "/api/codex/turn/isolate",
                json={
                    "session_id": "session-pristine-close",
                    "execution_id": "execution-pristine-close",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "released")
        self.assertFalse(fake.stream_entered)
        self.assertEqual(fake.isolate_calls, 2)
        self.assertTrue(all(task.done() for task in deferred_tasks))

    def test_ws_reserves_before_ack_and_handles_pre_start_duplicate_interrupts(self) -> None:
        assert voice_bridge is not None and AgentEvent is not None and AgentEventType is not None
        original_execution_enabled = voice_bridge.CODEX_TURN_EXECUTION_ENABLED
        voice_bridge.CODEX_TURN_EXECUTION_ENABLED = True
        self.addCleanup(
            setattr,
            voice_bridge,
            "CODEX_TURN_EXECUTION_ENABLED",
            original_execution_enabled,
        )

        class FakeAgent:
            def __init__(self, *, block_before_start: bool = False) -> None:
                self.reservations: set[tuple[str, str]] = set()
                self.reserve_calls = 0
                self.interrupt_calls = 0
                self.cancel_reservation_calls = 0
                self.isolate_calls = 0
                self.block_before_start = block_before_start
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def reserve_turn(self, session_id, correlation_id, *, cwd=None):
                key = (session_id, correlation_id)
                if key not in self.reservations:
                    self.reserve_calls += 1
                    self.reservations.add(key)

            async def stream_turn(self, session_id, text, *, correlation_id=None, cwd=None, **_selection):
                if self.block_before_start:
                    await asyncio.sleep(60)
                self.started.set()
                yield AgentEvent(
                    AgentEventType.STARTED,
                    session_id=session_id,
                    thread_id="thread-test",
                    turn_id="turn-test",
                    correlation_id=correlation_id,
                    status="started",
                )
                await self.cancelled.wait()
                yield AgentEvent(
                    AgentEventType.INTERRUPTED,
                    session_id=session_id,
                    thread_id="thread-test",
                    turn_id="turn-test",
                    correlation_id=correlation_id,
                    status="interrupted",
                )

            async def interrupt_by_reference(self, session_id, *, execution_id=None, thread_id=None, turn_id=None):
                if not self.started.is_set():
                    await self.started.wait()
                self.interrupt_calls += 1
                self.cancelled.set()
                return AgentEvent(
                    AgentEventType.INTERRUPTED,
                    session_id=session_id,
                    thread_id="thread-test",
                    turn_id="turn-test",
                    correlation_id=execution_id,
                    status="interrupted",
                )

            async def cancel_reservation(self, session_id, correlation_id):
                self.cancel_reservation_calls += 1

            async def isolate_turn(
                self,
                session_id,
                execution_id,
                *,
                thread_id=None,
                turn_id=None,
                reason=None,
            ):
                self.isolate_calls += 1
                self.cancelled.set()

        async def fake_ensure():
            return None, None, fake

        fake = FakeAgent()
        voice_bridge._CODEX_SHUTTING_DOWN = False
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                with client.websocket_connect("/api/codex/ws") as ws:
                    ws.send_json(
                        {
                            "type": "turn/start",
                            "session_id": "ws-session",
                            "execution_id": "ws-exec",
                            "text": "hello",
                        }
                    )
                    accepted = ws.receive_json()
                    self.assertEqual(accepted["type"], "accepted")
                    # The acknowledgement is sent only after reserve_turn.
                    self.assertEqual(fake.reserve_calls, 1)
                    ws.send_json(
                        {
                            "type": "turn/interrupt",
                            "session_id": "ws-session",
                            "execution_id": "ws-exec",
                        }
                    )
                    requested_seen = False
                    first_payload = None
                    for _ in range(3):
                        first_payload = ws.receive_json()
                        if first_payload["type"] == "interrupt_requested":
                            requested_seen = True
                            break
                    self.assertTrue(requested_seen)
                    seen = []
                    if first_payload is not None and first_payload["type"] != "interrupt_requested":
                        seen.append(first_payload["type"])
                    for _ in range(3):
                        payload = ws.receive_json()
                        seen.append(payload["type"])
                        if payload["type"] == AgentEventType.INTERRUPTED.value:
                            break
                    self.assertIn(AgentEventType.INTERRUPTED.value, seen)
                    self.assertEqual(fake.interrupt_calls, 1)
        self.assertEqual(fake.cancel_reservation_calls, 0)

        # Interrupt-before-start reserves the same correlation and is not
        # silently discarded; the later start consumes the pending intent.
        fake = FakeAgent()
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                with client.websocket_connect("/api/codex/ws") as ws:
                    ws.send_json(
                        {
                            "type": "turn/interrupt",
                            "session_id": "early-session",
                            "execution_id": "early-exec",
                        }
                    )
                    self.assertEqual(ws.receive_json()["type"], "interrupt_requested")
                    ws.send_json(
                        {
                            "type": "turn/start",
                            "session_id": "early-session",
                            "execution_id": "early-exec",
                            "text": "hello",
                        }
                    )
                    self.assertEqual(ws.receive_json()["type"], "accepted")
                    terminal_seen = False
                    for _ in range(4):
                        payload = ws.receive_json()
                        if payload["type"] == AgentEventType.INTERRUPTED.value:
                            terminal_seen = True
                            break
                    self.assertTrue(terminal_seen)
                    self.assertEqual(fake.interrupt_calls, 1)

        # A disconnect before start cleans the reservation instead of leaving
        # a ghost pending turn in the long-lived agent.
        fake = FakeAgent()
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                ws = client.websocket_connect("/api/codex/ws")
                ws.__enter__()
                ws.send_json(
                    {
                        "type": "turn/interrupt",
                        "session_id": "abort-session",
                        "execution_id": "abort-exec",
                    }
                )
                self.assertEqual(ws.receive_json()["type"], "interrupt_requested")
                ws.close()
                ws.__exit__(None, None, None)
        self.assertEqual(fake.cancel_reservation_calls, 1)

        fake = FakeAgent()
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                with client.websocket_connect("/api/codex/ws") as ws:
                    ws.send_json(
                        {
                            "type": "turn/start",
                            "session_id": "disconnect-session",
                            "execution_id": "disconnect-exec",
                            "text": "hello",
                        }
                    )
                    self.assertEqual(ws.receive_json()["type"], "accepted")
                    # Consume the started event, then disconnect while the
                    # stream is still live. The bridge must isolate first.
                    self.assertEqual(ws.receive_json()["type"], AgentEventType.STARTED.value)
                self.assertEqual(fake.isolate_calls, 1)

        # The browser can close immediately after the synchronous accepted
        # acknowledgement, before the stream task gets a scheduling turn.
        # The reserve gate must still have installed the agent and pending
        # correlation so disconnect isolation cannot be skipped.
        fake = FakeAgent(block_before_start=True)
        with patch.object(voice_bridge, "_ensure_codex", fake_ensure):
            with TestClient(voice_bridge.app) as client:
                ws = client.websocket_connect("/api/codex/ws")
                ws.__enter__()
                ws.send_json(
                    {
                        "type": "turn/start",
                        "session_id": "immediate-session",
                        "execution_id": "immediate-exec",
                        "text": "hello",
                    }
                )
                self.assertEqual(ws.receive_json()["type"], "accepted")
                # Do not receive AgentStarted; close in the accepted-to-task
                # scheduling window and require typed isolation first.
                ws.close()
                ws.__exit__(None, None, None)
        self.assertEqual(fake.isolate_calls, 1)
        self.assertEqual(fake.cancel_reservation_calls, 1)


if __name__ == "__main__":
    unittest.main()
