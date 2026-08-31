"""Contract tests for the checked-in stable Codex schema artifacts."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "codex-schema-gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("codex_schema_gate", GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load schema gate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _write_fake_codex(
    root: Path,
    *,
    version_stdout: str = gate.EXPECTED_CLI_BANNER,
    version_stderr: str = "",
    version_returncode: int = 0,
) -> tuple[Path, Path]:
    log = root / "argv.jsonl"
    fake = root / "fake-codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        f"    sys.stdout.write({version_stdout!r})\n"
        f"    sys.stderr.write({version_stderr!r})\n"
        f"    raise SystemExit({version_returncode})\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
        "if sys.argv[2] == 'generate-json-schema':\n"
        "    (out / 'codex_app_server_protocol.v2.schemas.json').write_text('{}')\n"
        "else:\n"
        "    (out / 'index.ts').write_text('export type Stable = string;\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake, log


def _read_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


class CodexSchemaGateTests(unittest.TestCase):
    def test_checked_in_stable_artifacts_pass_gate(self) -> None:
        gate.check(ROOT)

    def test_manifest_hash_and_tree_metadata_match_artifacts(self) -> None:
        manifest = json.loads((ROOT / "agents/codex/protocol-manifest.json").read_text(encoding="utf-8"))
        schema_path = ROOT / manifest["generatedSchema"]
        self.assertEqual(gate._sha256_file(schema_path), manifest["schemaSha256"])
        schema_digest, schema_count, schema_bytes = gate.tree_digest(
            ROOT / manifest["generatedSchemaTree"]["root"], suffix=".json"
        )
        self.assertEqual(schema_count, manifest["generatedSchemaTree"]["fileCount"])
        self.assertEqual(schema_bytes, manifest["generatedSchemaTree"]["byteCount"])
        self.assertEqual(schema_digest, manifest["generatedSchemaTree"]["treeSha256"])
        digest, count, byte_count = gate.tree_digest(ROOT / manifest["generatedTypes"]["root"])
        self.assertEqual(count, manifest["generatedTypes"]["fileCount"])
        self.assertEqual(byte_count, manifest["generatedTypes"]["byteCount"])
        self.assertEqual(digest, manifest["generatedTypes"]["treeSha256"])

    def test_stable_surface_manifest_disables_experimental_flags(self) -> None:
        generated = ROOT / "agents/codex/generated"
        # The stable generator legitimately emits stable definitions whose
        # names contain `ExperimentalFeature`; the forbidden surface is the
        # generator's `--experimental` flag, not that vocabulary.
        self.assertTrue(any(path.name == "ExperimentalFeatureListParams.json" for path in generated.rglob("*")))
        manifest = json.loads((ROOT / "agents/codex/protocol-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["experimentalApi"])
        self.assertFalse(manifest["requestAttestation"])

    def test_required_wire_includes_login_initialized_and_excludes_spawn_terminal(self) -> None:
        manifest = json.loads((ROOT / "agents/codex/protocol-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("account/login/start", manifest["requiredWire"]["clientRequests"])
        self.assertIn("account/login/cancel", manifest["requiredWire"]["clientRequests"])
        self.assertEqual(manifest["requiredWire"]["clientNotifications"], ["initialized"])
        self.assertIn(
            "remoteControl/status/changed",
            manifest["requiredWire"]["serverNotifications"],
        )
        self.assertIn("account/updated", manifest["requiredWire"]["serverNotifications"])
        self.assertIn("account/login/completed", manifest["requiredWire"]["serverNotifications"])
        self.assertIn("skills/changed", manifest["requiredWire"]["serverNotifications"])
        self.assertIn("thread/goal/cleared", manifest["requiredWire"]["serverNotifications"])
        self.assertIn("thread/settings/updated", manifest["requiredWire"]["serverNotifications"])
        self.assertNotIn("process/exited", manifest["requiredWire"]["serverNotifications"])
        client_notification = (ROOT / "agents/codex/generated/stable-ts/ClientNotification.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn('"method": "initialized"', client_notification)
        server_notification = (ROOT / "agents/codex/generated/stable-ts/ServerNotification.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn('"method": "remoteControl/status/changed"', server_notification)
        self.assertIn('"method": "process/exited"', server_notification)
        self.assertNotIn('"method": "dsh/app-server/exited"', server_notification)
        self.assertNotIn('"method": "dsh/app-server/isolation-failed"', server_notification)
        process_exited = (
            ROOT / "agents/codex/generated/stable/v2/ProcessExitedNotification.json"
        ).read_text(encoding="utf-8")
        self.assertIn("Final process exit notification for `process/spawn`", process_exited)

    def test_generator_command_is_stable_only(self) -> None:
        with self.assertRaises(gate.SchemaGateError):
            gate._assert_no_experimental_flag(["codex", "app-server", "generate-ts", "--experimental"])
        gate._assert_no_experimental_flag(["codex", "app-server", "generate-ts", "--out", "/tmp/out"])

    def test_generate_checks_version_then_runs_both_stable_generators(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            fake, log = _write_fake_codex(root)
            output = root / "generated"
            gate.generate(os.fspath(fake), output)
            calls = _read_calls(log)
            self.assertEqual(calls[0], ["--version"])
            self.assertEqual(len(calls), 3)
            self.assertTrue(all("--experimental" not in call for call in calls))
            self.assertIn("generate-json-schema", calls[1])
            self.assertIn("generate-ts", calls[2])
            self.assertTrue((output / "stable-json").is_dir())
            self.assertTrue((output / "stable-ts").is_dir())

    def test_wrong_or_malformed_version_never_runs_generator_or_creates_output(self) -> None:
        banners = {
            "wrong": "codex-cli 0.148.0-alpha.8\n",
            "malformed": "codex-cli 0.149.0-alpha.4.1 extra\n",
            "extra-line": "codex-cli 0.149.0-alpha.4.1\nuntrusted\n",
            "missing-newline": "codex-cli 0.149.0-alpha.4.1",
        }
        for label, banner in banners.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                fake, log = _write_fake_codex(root, version_stdout=banner)
                output = root / "generated"
                with self.assertRaises(gate.SchemaGateError):
                    gate.generate(os.fspath(fake), output)
                self.assertEqual(_read_calls(log), [["--version"]])
                self.assertFalse(output.exists())

    def test_version_stderr_or_failure_never_runs_generator(self) -> None:
        cases = {
            "stderr": {"version_stderr": "warning\n"},
            "nonzero": {"version_returncode": 2},
        }
        for label, options in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                fake, log = _write_fake_codex(root, **options)
                with self.assertRaises(gate.SchemaGateError):
                    gate.generate(os.fspath(fake), root / "generated")
                self.assertEqual(_read_calls(log), [["--version"]])

    def test_generate_rejects_existing_output_and_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            cases: list[tuple[str, Path]] = []

            existing = root / "existing"
            existing.mkdir()
            cases.append(("existing", existing))

            target = root / "target"
            target.mkdir()
            output_link = root / "output-link"
            output_link.symlink_to(target, target_is_directory=True)
            cases.append(("output-symlink", output_link))

            real_parent = root / "real-parent"
            real_parent.mkdir()
            parent_link = root / "parent-link"
            parent_link.symlink_to(real_parent, target_is_directory=True)
            cases.append(("ancestor-symlink", parent_link / "generated"))

            traversal_parent = root / "traversal-parent"
            traversal_parent.mkdir()
            cases.append(("parent-traversal", traversal_parent / ".." / "generated"))

            for label, output in cases:
                with self.subTest(label=label):
                    fake_root = root / f"fake-{label}"
                    fake_root.mkdir()
                    fake, log = _write_fake_codex(fake_root)
                    with self.assertRaises(gate.SchemaGateError):
                        gate.generate(os.fspath(fake), output)
                    self.assertEqual(_read_calls(log), [["--version"]])


if __name__ == "__main__":
    unittest.main()
