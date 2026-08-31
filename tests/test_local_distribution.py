from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LocalDistributionTests(unittest.TestCase):
    def test_distribution_lock_is_private_and_pins_runtime_inputs(self) -> None:
        lock = json.loads((ROOT / "xiaoman.local.lock.json").read_text(encoding="utf-8"))

        self.assertEqual(lock["schemaVersion"], 1)
        self.assertEqual(lock["distribution"]["visibility"], "PRIVATE")
        self.assertEqual(lock["distribution"]["repository"], "chenxigary/xiaoman-dsh-local")
        self.assertRegex(lock["xiaomanV3"]["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(lock["privateAssets"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(lock["privateAssets"]["sileroVadSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(lock["profiles"]["performance"]["context"], 16384)
        self.assertIn("Qwen3-14B", lock["profiles"]["performance"]["llm"])

    def test_checked_in_personal_assets_match_manifest(self) -> None:
        asset_root = ROOT / "assets" / "xiaoman"
        for line in (asset_root / "assets.sha256").read_text(encoding="utf-8").splitlines():
            expected, relative = line.split(maxsplit=1)
            payload = (asset_root / relative).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), expected, relative)

    def test_runtime_launchers_have_no_old_machine_absolute_path(self) -> None:
        launchers = [
            ROOT / "scripts" / "setup-macos-local.sh",
            ROOT / "scripts" / "run-local.sh",
            ROOT / "scripts" / "start-avatar.sh",
            ROOT / "scripts" / "start-voice-runtime.sh",
            ROOT / "scripts" / "test-avatar-sync.sh",
        ]
        source = "\n".join(path.read_text(encoding="utf-8") for path in launchers)
        self.assertNotIn("/Users/xichen", source)
        self.assertIn(".runtime/macos-local-voice-agents/xiaoman-v3", source)

    def test_local_only_boundary_is_defense_in_depth(self) -> None:
        bridge = (ROOT / "bridge" / "voice_bridge.py").read_text(encoding="utf-8")
        provider = (ROOT / "agents" / "codex" / "provider.py").read_text(encoding="utf-8")
        config = json.loads((ROOT / "bridge" / "bridge-config.example.json").read_text(encoding="utf-8"))
        overlay = (ROOT / "config" / "dsh-local-model.patch.yml").read_text(encoding="utf-8")

        self.assertIn("LOCAL_ONLY_BUILD = True", bridge)
        self.assertIn("CODEX_TURN_EXECUTION_ENABLED = False", bridge)
        self.assertIn("_TURN_EXECUTION_ENABLED = False", provider)
        self.assertFalse(config["codex"]["enabled"])
        self.assertIn("baseURL: http://127.0.0.1:8090/v1", overlay)
        self.assertNotIn("Authorization:", overlay)
        self.assertIn("- id: llm-deepseek\n  disabled: true", overlay)
        self.assertIn("- id: web-search-deepseek\n  disabled: true", overlay)

        local_llm = (ROOT / "scripts" / "start-local-llm.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('HOST="${LOCAL_LLM_HOST:-127.0.0.1}"', local_llm)
        self.assertIn("--offline", local_llm)
        self.assertIn("--parallel 1", local_llm)
        self.assertNotIn("--api-key", local_llm)

    def test_local_launchers_have_safe_help_and_no_recursive_delete(self) -> None:
        scripts = [
            ROOT / "scripts" / "setup-macos-local.sh",
            ROOT / "scripts" / "run-local.sh",
            ROOT / "scripts" / "stop-local.sh",
        ]
        source = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
        self.assertNotIn("rm -rf", source)
        for script in scripts[:2]:
            result = subprocess.run(
                ["bash", str(script), "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("用法", result.stdout)

        run_local = (ROOT / "scripts" / "run-local.sh").read_text(encoding="utf-8")
        self.assertIn('check_args+=(--no-avatar)', run_local)


if __name__ == "__main__":
    unittest.main()
