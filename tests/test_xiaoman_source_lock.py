from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "bridge" / "xiaoman_v3_adapters" / "source-lock.json"


class XiaomanSourceLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    def test_authority_is_pinned_to_an_exact_v3_revision(self) -> None:
        authority = self.lock["authority"]
        self.assertEqual(authority["subdirectory"], "xiaoman-v3")
        self.assertRegex(authority["revision"], re.compile(r"^[0-9a-f]{40}$"))

    def test_mlx_version_drift_is_explicit_not_silent(self) -> None:
        mlx = self.lock["dependency_alignment"]["mlx-audio"]
        self.assertEqual(mlx["v3_authority"], "0.4.7")
        self.assertEqual(mlx["dsh_current"], "0.5.0")
        self.assertIn("resolved-by-process-boundary", mlx["status"])

    def test_default_consumption_is_versioned_runtime_without_auto_fallback(self) -> None:
        consumption = self.lock["consumption"]
        self.assertEqual(consumption["default"], "xiaoman.voice-runtime.v1")
        self.assertEqual(consumption["local_adapters"], "explicit-fallback-only")
        self.assertFalse(consumption["automatic_fallback"])

    def test_derived_model_providers_are_fallback_only(self) -> None:
        model_entries = {
            entry["local"]: entry["runtime"]
            for entry in self.lock["files"]
            if "/stt/" in entry["local"] or entry["local"].endswith(
                ("/tts/base.py", "/tts/omnivoice.py", "/tts/qwen3.py")
            )
        }
        self.assertTrue(model_entries)
        self.assertEqual(set(model_entries.values()), {"explicit-local-fallback"})

    def test_every_derived_file_matches_its_audited_local_hash(self) -> None:
        for entry in self.lock["files"]:
            path = ROOT / entry["local"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, entry["local_sha256"], entry["local"])

    def test_exact_copies_match_the_locked_v3_source_hash(self) -> None:
        exact = [entry for entry in self.lock["files"] if entry["mode"] == "exact-copy"]
        self.assertTrue(exact)
        for entry in exact:
            self.assertEqual(
                entry["local_sha256"],
                entry["source_sha256"],
                entry["local"],
            )

    def test_non_runtime_copies_are_explicitly_quarantined(self) -> None:
        quarantined = {
            entry["local"]
            for entry in self.lock["files"]
            if entry["runtime"] == "compatibility-test-only"
        }
        self.assertEqual(
            quarantined,
            {
                "bridge/xiaoman_v3_adapters/audio/bus.py",
                "bridge/xiaoman_v3_adapters/tts/text_segmentation.py",
                "bridge/xiaoman_v3_adapters/vad/energy_vad.py",
            },
        )

    def test_registry_import_does_not_eagerly_load_quarantined_modules(self) -> None:
        script = """
import sys
from bridge.xiaoman_v3_adapters.registry import ProviderRegistry
assert ProviderRegistry
blocked = {
    'bridge.xiaoman_v3_adapters.audio.bus',
    'bridge.xiaoman_v3_adapters.tts.text_segmentation',
    'bridge.xiaoman_v3_adapters.vad.energy_vad',
}
loaded = blocked.intersection(sys.modules)
assert not loaded, sorted(loaded)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_bridge_import_does_not_load_duplicate_mlx_models(self) -> None:
        script = """
import sys
from bridge import voice_bridge
assert voice_bridge.VOICE_RUNTIME.enabled
assert voice_bridge.models._stt is None
assert voice_bridge.models._tts is None
assert not any(name == 'mlx_audio' or name.startswith('mlx_audio.') for name in sys.modules)
"""
        env = dict(os.environ)
        env["VOICE_BRIDGE_CONFIG"] = str(ROOT / "bridge/bridge-config.example.json")
        env.pop("DSH_VOICE_RUNTIME_MODE", None)
        env.pop("DSH_VOICE_RUNTIME_URL", None)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
