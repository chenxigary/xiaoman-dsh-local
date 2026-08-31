import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BaselineSmokeTests(unittest.TestCase):
    def test_launchers_are_executable_and_bash_parseable(self) -> None:
        launchers = [
            ROOT / "scripts" / "setup-macos-local.sh",
            ROOT / "scripts" / "run-local.sh",
            ROOT / "scripts" / "status-local.sh",
            ROOT / "scripts" / "stop-local.sh",
            ROOT / "scripts" / "start-bridge.sh",
            ROOT / "scripts" / "start-dsh.sh",
            ROOT / "scripts" / "start-all.sh",
            ROOT / "scripts" / "start-local-llm.sh",
            ROOT / "scripts" / "stop-local-llm.sh",
            ROOT / "scripts" / "start-avatar.sh",
            ROOT / "scripts" / "start-voice-runtime.sh",
            ROOT / "scripts" / "bootstrap-dsh.sh",
            ROOT / "scripts" / "install-dsh-plugin.sh",
            ROOT / "scripts" / "smoke-check.sh",
        ]
        for launcher in launchers:
            self.assertTrue(os.access(launcher, os.X_OK), launcher)
        result = subprocess.run(
            ["bash", "-n", *(str(path) for path in launchers)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mac_launchers_do_not_depend_on_windows_paths_or_napcat(self) -> None:
        bridge = (ROOT / "scripts" / "start-bridge.sh").read_text()
        dsh = (ROOT / "scripts" / "start-dsh.sh").read_text()
        all_script = (ROOT / "scripts" / "start-all.sh").read_text()
        self.assertNotIn("Scripts\\python.exe", bridge)
        self.assertNotIn("D:\\", bridge + dsh + all_script)
        self.assertNotIn("start-napcat", all_script)
        self.assertIn("--app-dir", bridge)
        self.assertIn("pnpm dsh web", dsh)

    def test_avatar_defaults_match_xiaoman_v3_accepted_baseline(self) -> None:
        avatar = (ROOT / "scripts" / "start-avatar.sh").read_text()
        self.assertIn('V3_AVATAR_DEVICE="${XIAOMAN_AVATAR_DEVICE:-${V3_AVATAR_DEVICE:-auto}}"', avatar)
        self.assertIn('--inference_stride "${XIAOMAN_AVATAR_INFERENCE_STRIDE:-4}"', avatar)
        self.assertIn('XIAOMAN_AVATAR_VIDEO_CODEC:-VP8', avatar)
        self.assertIn('XIAOMAN_AVATAR_SESSION_WARMUP:-1', avatar)
        self.assertIn('"${AVATAR_PYTHON}" "${AVATAR_RUNNER}"', avatar)
        runner = (ROOT / "scripts" / "run-avatar.py").read_text()
        self.assertIn("install_video_codec_preference", runner)
        self.assertIn("install_session_warmup", runner)

    def test_example_config_is_portable_and_latency_configured(self) -> None:
        config = json.loads((ROOT / "bridge" / "bridge-config.example.json").read_text())
        self.assertEqual(config["stt"]["device"], "auto")
        self.assertEqual(config["tts"]["device"], "auto")
        self.assertTrue(config["latency"]["enabled"])
        self.assertIn("sample_rate", config["latency"])
        self.assertNotIn("C:/", config["tts"]["model_name"])
        self.assertEqual(config["codex"]["command"], ["codex", "app-server", "--stdio"])
        self.assertEqual(config["codex"]["runtime_state"], "runtime/codex-thread-map.json")
        self.assertEqual(config["codex"]["sandbox"], "read-only")
        self.assertEqual(config["codex"]["approval_policy"], "never")
        self.assertEqual(config["codex"]["expected_cli_version"], "0.149.0-alpha.4.1")
        self.assertFalse(config["codex"]["enabled"])
        self.assertNotIn("C:/", config["tts"]["model_name"])
        self.assertNotIn("\\\\", config["tts"]["model_name"])
        self.assertEqual(config["tts"]["ref_audio"], "assets/xiaoman/voice/ref.wav")
        self.assertEqual(config["xiaoman"]["character"], "xiaoman")
        self.assertEqual(config["xiaoman"]["provider_config"], "assets/xiaoman/config/providers.json")
        self.assertEqual(config["voice_runtime"]["mode"], "v3")
        self.assertEqual(config["voice_runtime"]["base_url"], "http://127.0.0.1:7860")

    def test_bridge_source_has_checkout_root_vad_path_and_config_fallback(self) -> None:
        source = (ROOT / "bridge" / "voice_bridge.py").read_text()
        self.assertIn('REPO_ROOT / "models" / "silero-vad"', source)
        self.assertIn("EXAMPLE_CONFIG_PATH", source)
        self.assertIn("resolve_device", source)
        self.assertIn("/api/codex/ws", source)
        self.assertIn("_codex_workspace", source)
        self.assertIn("_shutdown_codex", source)

    def test_bridge_runtime_includes_websocket_transport(self) -> None:
        requirements = {
            line.split("#", 1)[0].strip().split("==", 1)[0].lower()
            for line in (ROOT / "bridge" / "requirements.txt").read_text().splitlines()
        }
        self.assertIn("websockets", requirements)
        self.assertIn("httpx", requirements)

    def test_voice_runtime_launcher_uses_authoritative_v3_process(self) -> None:
        runtime = (ROOT / "scripts" / "start-voice-runtime.sh").read_text()
        all_script = (ROOT / "scripts" / "start-all.sh").read_text()
        self.assertIn("XIAOMAN_V3_ROOT", runtime)
        self.assertIn("gateway.app:app", runtime)
        self.assertIn("/api/voice-runtime/v1/health", runtime + all_script)
        self.assertIn("DSH_VOICE_RUNTIME_MODE", all_script)

    def test_codex_protocol_manifest_is_pinned_and_stable(self) -> None:
        manifest = json.loads((ROOT / "agents" / "codex" / "protocol-manifest.json").read_text())
        self.assertEqual(manifest["codexCliVersion"], "0.149.0-alpha.4.1")
        self.assertEqual(len(manifest["schemaSha256"]), 64)
        self.assertFalse(manifest["experimentalApi"])
        self.assertFalse(manifest["requestAttestation"])

    def test_model_smoke_scripts_use_checkout_relative_defaults(self) -> None:
        stt = (ROOT / "bridge" / "smoke_stt.py").read_text()
        tts = (ROOT / "bridge" / "smoke_tts.py").read_text()
        self.assertIn("REPO_ROOT / \"ref_audio.wav\"", stt)
        self.assertIn("REPO_ROOT / \"tts_out.wav\"", tts)
        self.assertNotIn("C:\\\\Users", stt)
        self.assertNotIn("D:\\\\speech-to-speech", tts)

    def test_python_runtime_selects_python_310_or_newer(self) -> None:
        helper = ROOT / "scripts" / "python-runtime.sh"
        result = subprocess.run(
            ["bash", "-c", f"REPO_ROOT={ROOT!s}; source {helper!s}; select_supported_python"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = result.stdout.strip()
        self.assertTrue(selected)
        version = subprocess.run(
            [selected, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        major, minor, _patch = (int(part) for part in version.split("."))
        self.assertGreaterEqual((major, minor), (3, 10))


if __name__ == "__main__":
    unittest.main()
