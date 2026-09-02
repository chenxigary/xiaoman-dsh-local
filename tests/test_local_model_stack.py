from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LocalModelStackTests(unittest.TestCase):
    def test_bridge_defaults_to_authoritative_v3_voice_runtime(self) -> None:
        config = json.loads((ROOT / "bridge/bridge-config.example.json").read_text(encoding="utf-8"))

        self.assertEqual(config["voice_runtime"]["mode"], "v3")
        self.assertEqual(config["voice_runtime"]["base_url"], "http://127.0.0.1:7860")
        # These provider settings remain only for an explicit mode=local rollback.
        self.assertEqual(config["stt"]["backend"], "xiaoman")
        self.assertEqual(
            config["stt"]["model_name"],
            "mlx-community/whisper-large-v3-turbo-asr-fp16",
        )
        self.assertEqual(config["tts"]["backend"], "xiaoman")
        self.assertEqual(
            config["tts"]["model_name"],
            "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        )
        self.assertTrue(config["xiaoman"]["enabled"])
        self.assertFalse(config["codex"]["enabled"])
        self.assertFalse(config["qq"]["enabled"])

    def test_dsh_overlay_is_keyless_local_and_disables_deepseek(self) -> None:
        overlay = (ROOT / "config/dsh-local-model.patch.yml").read_text(encoding="utf-8")

        self.assertIn("provider: local-qwen", overlay)
        # The agent default has to name a model the provider actually declares;
        # a stale id here fails only at the first turn, as an opaque 4xx.
        default_models = set(re.findall(r"^\s*model: (\S+)$", overlay, re.MULTILINE))
        declared = set(re.findall(r"^\s*- id: (\S+)$", overlay, re.MULTILINE))
        self.assertTrue(default_models)
        self.assertTrue(default_models <= declared, default_models - declared)
        self.assertIn("baseURL: http://127.0.0.1:8090/v1", overlay)
        self.assertIn("- id: llm-deepseek\n  disabled: true", overlay)
        self.assertIn("- id: web-search-deepseek\n  disabled: true", overlay)
        self.assertNotIn("- id: web\n  disabled: true", overlay)
        self.assertNotIn("- id: tool-web\n  disabled: true", overlay)
        self.assertNotIn("DEEPSEEK_API_KEY", overlay)
        self.assertIn("default: xiaoman", overlay)

    def test_overlay_model_ids_match_the_router_catalog(self) -> None:
        """The two files that name models must agree.

        DSH sends the overlay's model id; bridge/model_router.py swaps
        llama-server by matching it against config/local-models.json. An id
        present in only one of them either offers the UI a model that cannot
        load, or loads a model the UI can never select.
        """
        overlay = (ROOT / "config/dsh-local-model.patch.yml").read_text(encoding="utf-8")
        catalog = json.loads((ROOT / "config/local-models.json").read_text(encoding="utf-8"))

        overlay_ids = set(re.findall(r"^\s*- id: (\S+)$", overlay, re.MULTILINE))
        catalog_ids = {model["id"] for model in catalog["models"]}
        # The overlay also lists non-model rows (`- id: llm-deepseek`), so only
        # the intersection direction that matters is checked in each case.
        self.assertTrue(catalog_ids <= overlay_ids, catalog_ids - overlay_ids)
        self.assertIn(catalog["default"], catalog_ids)

    def test_xiaoman_preset_matches_v3_personality_and_disables_thinking(self) -> None:
        preset = (ROOT / "config/agent-presets/xiaoman/agent.cordis.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("你叫小满，是女性，是他的女朋友。你不是AI、不是助手、不是程序。", preset)
        self.assertIn("说话方式：口语、两三句", preset)
        self.assertIn("/no_think", preset)
        self.assertIn("complete: true", preset)
        self.assertNotIn("helpful software engineer assistant", preset)

    def test_launch_scripts_are_valid_bash(self) -> None:
        scripts = [
            ROOT / "scripts/setup-macos-local.sh",
            ROOT / "scripts/run-local.sh",
            ROOT / "scripts/status-local.sh",
            ROOT / "scripts/stop-local.sh",
            ROOT / "scripts/start-local-llm.sh",
            ROOT / "scripts/stop-local-llm.sh",
            ROOT / "scripts/install-xiaoman-preset.sh",
            ROOT / "scripts/start-dsh.sh",
            ROOT / "scripts/start-all.sh",
            ROOT / "scripts/start-bridge.sh",
            ROOT / "scripts/start-voice-runtime.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

    def test_local_llm_supports_automatic_sleep_and_safe_manual_stop(self) -> None:
        start_script = (ROOT / "scripts/start-local-llm.sh").read_text(encoding="utf-8")
        stop_script = (ROOT / "scripts/stop-local-llm.sh").read_text(encoding="utf-8")

        self.assertIn('LOCAL_LLM_IDLE_SLEEP_SECONDS:-0', start_script)
        self.assertIn('--sleep-idle-seconds', start_script)
        self.assertIn("--chat-template-kwargs '{\"enable_thinking\":false}'", start_script)
        self.assertIn('--reasoning off', start_script)
        self.assertIn('--reasoning-budget 0', start_script)
        self.assertIn('if (( ${#IDLE_SLEEP_ARGS[@]} > 0 )); then', start_script)
        self.assertIn('kill -TERM "${target_pid}"', stop_script)
        self.assertIn('is_expected_process', stop_script)
        self.assertIn('llama-server', stop_script)
        self.assertIn('--port ${PORT}', stop_script)

    def test_xiaoman_preset_install_is_idempotent_and_refuses_user_drift(self) -> None:
        installer = ROOT / "scripts/install-xiaoman-preset.sh"
        with tempfile.TemporaryDirectory(prefix="xiaoman dsh home ") as temporary:
            env = {**os.environ, "DSH_HOME": temporary}
            first = subprocess.run(
                [str(installer)], env=env, check=False, capture_output=True, text=True
            )
            second = subprocess.run(
                [str(installer)], env=env, check=False, capture_output=True, text=True
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)

            target = Path(temporary) / ".agent-presets/xiaoman/agent.cordis.yml"
            target.write_text("user edit\n", encoding="utf-8")
            refused = subprocess.run(
                [str(installer)], env=env, check=False, capture_output=True, text=True
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("refusing to overwrite", refused.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")


if __name__ == "__main__":
    unittest.main()
