from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap-dsh.sh"
CLEAN_GENERATED = ROOT / "scripts" / "clean-dsh-generated.py"
INSTALLER = ROOT / "scripts" / "install-dsh-plugin.sh"
HOST_PACKAGE = "@deepseek-ai/dsh-host-codex"


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def make_host_source(base: Path) -> Path:
    """Build a minimal independent Host source tree for installer fixtures."""

    source = base / "host source"
    (source / "src").mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": HOST_PACKAGE,
                "version": "0.1.0-rc.5",
                "type": "module",
                "exports": {
                    ".": {"types": "./lib/types/index.d.ts", "default": "./lib/index.js"},
                    "./types": {"types": "./lib/types/types.d.ts", "default": "./lib/types/types.js"},
                    "./typert": {"types": "./lib/typert.host.d.ts", "default": "./lib/typert.host.js"},
                    "./remote": {"types": "./lib/typert.remote-client.d.ts", "default": "./lib/typert.remote-client.js"},
                },
                "files": [
                    "lib/index.js",
                    "lib/invariant.js",
                    "lib/types/**/*.js",
                    "lib/types/**/*.d.ts",
                    "lib/typert.host.js",
                    "lib/typert.host.d.ts",
                    "lib/typert.remote-client.js",
                    "lib/typert.remote-client.d.ts",
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "tsconfig.json").write_text(
        """{
  "extends": "../.runtime/deepseek-harness/tsconfig.base.json",
  "compilerOptions": { "rootDir": "src", "outDir": "lib/types" },
  "include": ["src"],
  "references": [
    { "path": "../.runtime/deepseek-harness/vendor/cordis" },
    { "path": "../.runtime/deepseek-harness/packages/core/agent" }
  ]
}
""",
        encoding="utf-8",
    )
    (source / "src" / "index.ts").write_text(
        "export function apply(): void {}\n", encoding="utf-8"
    )
    (source / "src" / "types.ts").write_text(
        "export type CodexJson = string | number | boolean | null\n", encoding="utf-8"
    )
    (source / "tests").mkdir()
    (source / "tests" / "react-loop-quarantine.host.spec.ts").write_text(
        "import { Context } from '../../.runtime/deepseek-harness/vendor/cordis/src/index.ts'\n",
        encoding="utf-8",
    )
    # Simulate stale output left by a previous pinned build.  It is not source
    # provenance and must never enter the managed Host tree or manifest.
    (source / "lib").mkdir()
    (source / "lib" / "tsconfig.tsbuildinfo").write_text("generated\n", encoding="utf-8")
    return source


def installer_command(harness: Path, host_source: Path, *extra: str) -> list[str]:
    return [
        str(INSTALLER),
        "--harness",
        str(harness),
        "--host-source",
        str(host_source),
        *extra,
    ]


def make_harness_fixture(base: Path) -> Path:
    harness = base / "harness fixture with spaces"
    web = harness / "packages" / "bundle" / "web-app"
    web.mkdir(parents=True)
    (harness / "packages" / "client").mkdir()
    api_remotes = harness / "packages" / "api" / "remotes"
    (api_remotes / "src" / "client").mkdir(parents=True)
    api_proxy = harness / "packages" / "host" / "apiproxy" / "src" / "api-proxy.ts"
    api_proxy.parent.mkdir(parents=True)
    api_proxy.write_text(
        """function sessionBlank(session: Session): boolean {
  return !session.events.some(event => event.type === 'turn/start')
}

function applySessionListMetadata(state: SessionListMetadata, event: SessionEvent): SessionListMetadata {
  const blank = state.blank && event.type !== 'turn/start'
  const lastPromptAt = event.type === 'user/message' && event.data.source.kind === 'user'
    ? event.time
    : state.lastPromptAt
  return blank === state.blank && lastPromptAt === state.lastPromptAt
    ? state
    : { blank, lastPromptAt }
}

function register(projectionCtx: ProjectionContext): void {
  projectionCtx.sessionProjections.register<'sessionListMetadata', SessionListMetadata>({
      key: 'sessionListMetadata',
      schema: sessionListMetadataProjectionSchema,
      init: () => ({ blank: true, lastPromptAt: null }),
      apply: applySessionListMetadata,
      view: state => state,
      stateVersion: 1,
    })
}
""",
        encoding="utf-8",
    )
    client_manager = harness / "packages" / "client" / "runtime" / "src" / "client" / "sessions" / "manager.ts"
    client_manager.parent.mkdir(parents=True)
    client_manager.write_text(
        """function apply(frame: MuxFrame): void {
    if (frame.type === 'session/projection') {
      // Finished host-computed value: land it in the resident store whether or
      // not the Session is instantiated (list rows read the 'title' key). The
      // synchronous markDirty keeps the list snapshot same-tick fresh (the
      // store's own any-key channel is microtask-batched).
      this.projectionStore(frame.sessionId).apply(frame.key, frame.value, frame.seq)
      this.notifier.markDirty()
      return
    }
}
""",
        encoding="utf-8",
    )
    # The real pinned harness owns these generated artifacts.  Keeping small
    # tracked stand-ins in the bootstrap fixture lets the fake pnpm path cover
    # catalog recording and the rerun drift gate without invoking tsx.
    (harness / "docs").mkdir(parents=True)
    (harness / "docs" / "persistence-catalog.md").write_text(
        "# fixture persistence catalog\n", encoding="utf-8"
    )
    generated_events = harness / "packages" / "core" / "session" / "src"
    generated_events.mkdir(parents=True)
    (generated_events / "known-event-types.ts").write_text(
        "export const KNOWN_SESSION_EVENT_TYPES = new Set(['fixture']);\n",
        encoding="utf-8",
    )
    (harness / "package.json").write_text(
        '{"name":"fixture-harness","packageManager":"pnpm@9.0.0"}\n',
        encoding="utf-8",
    )
    (harness / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters:\n  packages/client/ui-trajectory: {}\n",
        encoding="utf-8",
    )
    (harness / "tsconfig.client.json").write_text(
        """{
  "references": [
    { "path": "./packages/client/ui-trajectory" },
    { "path": "./packages/client/ui-voice" },
    { "path": "./packages/client/ui-workspace" }
  ]
}
""",
        encoding="utf-8",
    )
    (harness / "tsconfig.host.json").write_text(
        """{
  "references": [
    { "path": "./packages/api/remotes/tsconfig.host.json" },
    { "path": "./packages/host/plugin-inventory" },
    { "path": "./packages/core/session" }
  ]
}
""",
        encoding="utf-8",
    )
    (web / "cordis.patch.yml").write_text(
        """- insert:
    - id: ui-trajectory
      name: '@deepseek-ai/dsh-client-ui-trajectory'
    - id: plugin-inventory
      name: '@deepseek-ai/dsh-host-plugin-inventory'
    - id: ui-workspace
      name: '@deepseek-ai/dsh-client-ui-workspace'
""",
        encoding="utf-8",
    )
    (web / "package.json").write_text(
        json.dumps(
            {
                "name": "@deepseek-ai/dsh-web-app",
                "dependencies": {
                    "@deepseek-ai/dsh-client-ui-trajectory": "workspace:^",
                    "@deepseek-ai/dsh-client-ui-workspace": "workspace:^",
                    "@deepseek-ai/dsh-host-plugin-inventory": "workspace:^",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (api_remotes / "package.json").write_text(
        json.dumps(
            {
                "name": "@deepseek-ai/dsh-api-remotes",
                "peerDependencies": {
                    "@deepseek-ai/dsh-host-plugin-inventory": "workspace:^",
                },
                "devDependencies": {
                    "@deepseek-ai/dsh-host-plugin-inventory": "workspace:^",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (api_remotes / "tsconfig.client.json").write_text(
        """{
  "references": [
    { "path": "../../host/plugin-inventory" },
    { "path": "../../interaction/commands" }
  ]
}
""",
        encoding="utf-8",
    )
    (api_remotes / "src" / "client" / "index.ts").write_text(
        """import pluginInventoryRemote from '@deepseek-ai/dsh-host-plugin-inventory/remote'
export type {} from '@deepseek-ai/dsh-host-plugin-inventory/remote'
export type {} from '@deepseek-ai/dsh-message-feedback/remote'
const selected = [pluginInventoryRemote,]
""",
        encoding="utf-8",
    )
    return harness


def make_git_origin(base: Path) -> tuple[Path, str]:
    seed = make_harness_fixture(base)
    run(["git", "init", "-b", "main"], cwd=seed).check_returncode()
    run(["git", "config", "user.email", "fixture@example.test"], cwd=seed).check_returncode()
    run(["git", "config", "user.name", "Fixture"], cwd=seed).check_returncode()
    run(["git", "add", "."], cwd=seed).check_returncode()
    run(["git", "commit", "-m", "fixture"], cwd=seed).check_returncode()
    commit = run(["git", "rev-parse", "HEAD"], cwd=seed).stdout.strip()
    origin = base / "deepseek harness origin.git"
    run(["git", "init", "--bare", str(origin)]).check_returncode()
    run(["git", "remote", "add", "origin", str(origin)], cwd=seed).check_returncode()
    run(["git", "push", "origin", "main"], cwd=seed).check_returncode()
    return origin, commit


class DshBootstrapTests(unittest.TestCase):
    def test_scripts_are_parseable_and_have_no_recursive_delete(self) -> None:
        scripts = [
            BOOTSTRAP,
            INSTALLER,
            ROOT / "scripts" / "start-dsh.sh",
            ROOT / "scripts" / "start-all.sh",
        ]
        result = run(["bash", "-n", *(str(path) for path in scripts)])
        self.assertEqual(result.returncode, 0, result.stderr)
        for script in scripts:
            self.assertNotIn("rm -rf", script.read_text(encoding="utf-8"))

    def test_build_phase_refreshes_api_remote_client_artifact(self) -> None:
        bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('"${PNPM_CMD[@]}" run build:lib:host', bootstrap)
        self.assertIn('"${PNPM_CMD[@]}" run build:lib:client', bootstrap)
        self.assertNotIn("--filter @deepseek-ai/dsh-client-ui-voice bundle", bootstrap)
        self.assertIn('BUILD_NODE_OPTIONS="${DSH_NODE_OPTIONS:-${NODE_OPTIONS:-}}"', bootstrap)
        self.assertIn('BUILD_NODE_OPTIONS+="--max-old-space-size=8192"', bootstrap)
        self.assertIn('clean-dsh-generated.py', bootstrap)
        self.assertIn('--post-build', bootstrap)
        # The root client build is the phase that invokes each package's
        # tsdown client face, including api/remotes/lib/client.js.
        self.assertIn("api/remotes' generated Remote", bootstrap)

    def test_generated_cleanup_is_exact_bounded_idempotent_and_symlink_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh generated cleanup ") as temporary:
            base = Path(temporary)
            harness = base / "pinned harness"
            client_root = harness / "packages/client/ui-voice/lib"
            host_root = harness / "packages/host/codex/lib"
            client_lib = client_root / "types/client"
            host_lib = host_root / "types"
            client_lib.mkdir(parents=True)
            host_lib.mkdir(parents=True)
            (client_lib / "stale.js").write_text("old\n", encoding="utf-8")
            (host_lib / "stale.d.ts").write_text("old\n", encoding="utf-8")
            user_source = harness / "packages/client/ui-voice/src/keep.ts"
            user_source.parent.mkdir(parents=True)
            user_source.write_text("keep\n", encoding="utf-8")
            external = base / "external"
            external.mkdir()
            external_file = external / "must-survive.txt"
            external_file.write_text("outside\n", encoding="utf-8")

            first = run([sys.executable, str(CLEAN_GENERATED), "--harness", str(harness)])
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertFalse(client_root.exists())
            self.assertFalse(host_root.exists())
            self.assertEqual(user_source.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(external_file.read_text(encoding="utf-8"), "outside\n")

            second = run([sys.executable, str(CLEAN_GENERATED), "--harness", str(harness)])
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("cleared 0 generated file(s)", second.stdout)

            # Both targets are preflighted before deletion.  A symlink in one
            # target must fail closed without touching a valid sibling tree.
            client_root.symlink_to(external, target_is_directory=True)
            host_lib = host_root / "types"
            host_lib.mkdir(parents=True)
            host_stale = host_lib / "still-there.js"
            host_stale.write_text("keep on refusal\n", encoding="utf-8")
            refused = run([sys.executable, str(CLEAN_GENERATED), "--harness", str(harness)])
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("must not contain a symlink", refused.stderr)
            self.assertTrue(host_stale.exists())
            self.assertEqual(external_file.read_text(encoding="utf-8"), "outside\n")

    def test_post_build_cleanup_only_removes_recognized_finder_conflicts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh post-build conflicts ") as temporary:
            base = Path(temporary)
            harness = base / "pinned harness"
            client_lib = harness / "packages/client/ui-voice/lib"
            host_lib = harness / "packages/host/codex/lib"
            client_lib.mkdir(parents=True)
            host_lib.mkdir(parents=True)

            def pair(root: Path, name: str, duplicate: str | None = None) -> None:
                canonical = root / name
                canonical.write_text(f"canonical:{name}\n", encoding="utf-8")
                duplicate_name = duplicate or name.replace(".", " 2.", 1)
                (root / duplicate_name).write_bytes(canonical.read_bytes())

            pair(client_lib, "index.js")
            pair(client_lib, "tsconfig.client.tsbuildinfo", "tsconfig.client 2.tsbuildinfo")
            pair(host_lib, "invariant.js")
            pair(host_lib, "typert.host.d.ts", "typert.host.d 2.ts")
            pair(host_lib, "typert.remote-client.d.ts.map", "typert.remote-client.d.ts 2.map")
            for root in (client_lib, host_lib):
                (root / "types").mkdir()
                (root / "types" / "canonical.js").write_text("canonical\n", encoding="utf-8")
                (root / "types 2").mkdir()
                (root / "types 2" / "canonical.js").write_text("canonical\n", encoding="utf-8")
                (root / "types 3").mkdir()
            keep_inside = client_lib / "build-note.txt"
            keep_inside.write_text("not a Finder conflict\n", encoding="utf-8")

            outside = base / "outside.txt"
            outside.write_text("must survive\n", encoding="utf-8")
            result = run([
                sys.executable,
                str(CLEAN_GENERATED),
                "--harness",
                str(harness),
                "--post-build",
            ])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("removed 9 recognized post-build conflict file(s)", result.stdout)
            self.assertFalse((client_lib / "index 2.js").exists())
            self.assertFalse((host_lib / "typert.host.d 2.ts").exists())
            self.assertFalse((host_lib / "typert.remote-client.d.ts 2.map").exists())
            self.assertFalse((client_lib / "types 2").exists())
            self.assertFalse((client_lib / "types 3").exists())
            self.assertFalse((host_lib / "types 2").exists())
            self.assertFalse((host_lib / "types 3").exists())
            self.assertEqual((client_lib / "types" / "canonical.js").read_text(), "canonical\n")
            self.assertEqual((host_lib / "types" / "canonical.js").read_text(), "canonical\n")
            self.assertEqual(keep_inside.read_text(), "not a Finder conflict\n")
            self.assertEqual(outside.read_text(encoding="utf-8"), "must survive\n")
            second = run([
                sys.executable,
                str(CLEAN_GENERATED),
                "--harness",
                str(harness),
                "--post-build",
            ])
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("removed 0 recognized post-build conflict file(s)", second.stdout)

            # A differing file conflict is a fail-closed signal; the helper
            # must not guess whether tsc or tsdown produced the newest copy.
            (host_lib / "index.js").write_text("canonical host index\n", encoding="utf-8")
            differing = host_lib / "index 2.js"
            differing.write_text("newer host index\n", encoding="utf-8")
            refused = run([
                sys.executable,
                str(CLEAN_GENERATED),
                "--harness",
                str(harness),
                "--post-build",
            ])
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("not byte-identical", refused.stderr)
            self.assertTrue(differing.exists())
            self.assertEqual((host_lib / "index.js").read_text(), "canonical host index\n")
            differing.unlink()

            # An unrecognized conflict is also a fail-closed signal, not a
            # reason to delete a suspicious file or touch outside the target.
            unexpected = host_lib / "unexpected 2.js"
            unexpected.write_text("no canonical counterpart\n", encoding="utf-8")
            refused = run([
                sys.executable,
                str(CLEAN_GENERATED),
                "--harness",
                str(harness),
                "--post-build",
            ])
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("canonical file missing", refused.stderr)
            self.assertTrue(unexpected.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "must survive\n")

    def test_start_dsh_uses_runtime_default_and_reports_bootstrap_command(self) -> None:
        start_dsh = ROOT / "scripts" / "start-dsh.sh"
        start_all = ROOT / "scripts" / "start-all.sh"
        self.assertIn("${REPO_ROOT}/.runtime/deepseek-harness", start_dsh.read_text(encoding="utf-8"))
        self.assertIn("${REPO_ROOT}/.runtime/deepseek-harness", start_all.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="dsh missing ") as temporary:
            missing = Path(temporary) / "missing harness"
            result = run(
                [str(start_dsh)],
                env={**os.environ, "DSH_HARNESS": str(missing)},
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("scripts/bootstrap-dsh.sh", result.stderr)

    def test_installer_is_idempotent_with_spaces_and_copies_complete_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh integration ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            command = installer_command(harness, host_source)
            first = run(command)
            second = run(command)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already synchronized", second.stdout)

            generated_top_level = {"lib", "node_modules", ".turbo", ".cache"}
            source_files = {
                path.relative_to(ROOT / "dsh-plugin")
                for path in (ROOT / "dsh-plugin").rglob("*")
                if path.is_file()
                and path.relative_to(ROOT / "dsh-plugin").parts[0] not in generated_top_level
            }
            destination_files = {
                path.relative_to(harness / "packages/client/ui-voice")
                for path in (harness / "packages/client/ui-voice").rglob("*")
                if path.is_file() and ".dsh-managed" not in path.relative_to(
                    harness / "packages/client/ui-voice"
                ).parts
            }
            self.assertEqual(destination_files, source_files)
            managed = json.loads(
                (harness / "packages/client/ui-voice/.dsh-managed/manifest.json").read_text()
            )
            self.assertEqual(managed["schemaVersion"], 1)
            self.assertEqual(set(managed["files"]), {path.as_posix() for path in source_files})
            self.assertEqual(
                set(managed["hostFiles"]),
                {
                    path.relative_to(host_source).as_posix()
                    for path in host_source.rglob("*")
                    if path.is_file()
                    and path.relative_to(host_source).parts[0] not in generated_top_level
                },
            )
            self.assertFalse((harness / "packages/client/ui-voice/lib").exists())
            self.assertFalse((harness / "packages/host/codex/lib").exists())
            self.assertNotIn("lib/tsconfig.tsbuildinfo", managed["files"])
            self.assertNotIn("lib/tsconfig.tsbuildinfo", managed["hostFiles"])
            self.assertEqual(
                set(managed["registrations"]),
                {
                    "tsconfig.client.json",
                    "tsconfig.host.json",
                    "packages/api/remotes/src/client/index.ts",
                    "packages/api/remotes/package.json",
                    "packages/api/remotes/tsconfig.client.json",
                    "packages/bundle/web-app/cordis.patch.yml",
                    "packages/bundle/web-app/package.json",
                    "packages/host/apiproxy/src/api-proxy.ts",
                    "packages/client/runtime/src/client/sessions/manager.ts",
                },
            )
            tsconfig = (harness / "tsconfig.client.json").read_text(encoding="utf-8")
            self.assertEqual(
                tsconfig.count('./packages/client/ui-voice'), 1
            )
            source_leaf = (ROOT / "dsh-plugin/tsconfig.client.json").read_text(encoding="utf-8")
            installed_leaf = (harness / "packages/client/ui-voice/tsconfig.client.json").read_text(encoding="utf-8")
            self.assertIn("../.runtime/deepseek-harness/", source_leaf)
            self.assertNotIn("../.runtime/deepseek-harness", installed_leaf)
            self.assertIn('"extends": "../../../tsconfig.base.client.json"', installed_leaf)
            self.assertIn('"path": "../../../packages/client/runtime"', installed_leaf)
            self.assertIn('"path": "../../../packages/api/remotes/tsconfig.client.json"', installed_leaf)
            source_host_test = (host_source / "tests/react-loop-quarantine.host.spec.ts").read_text(encoding="utf-8")
            installed_host_test = (harness / "packages/host/codex/tests/react-loop-quarantine.host.spec.ts").read_text(encoding="utf-8")
            self.assertIn("../../.runtime/deepseek-harness/", source_host_test)
            self.assertIn("../../../../vendor/cordis/src/index.ts", installed_host_test)
            self.assertNotIn(".runtime/deepseek-harness", installed_host_test)
            source_host_leaf = (host_source / "tsconfig.json").read_text(encoding="utf-8")
            installed_host_leaf = (harness / "packages/host/codex/tsconfig.json").read_text(encoding="utf-8")
            self.assertIn("../.runtime/deepseek-harness/", source_host_leaf)
            self.assertNotIn("../.runtime/deepseek-harness", installed_host_leaf)
            self.assertIn('"extends": "../../../tsconfig.base.json"', installed_host_leaf)
            self.assertIn('"path": "../../../vendor/cordis"', installed_host_leaf)
            self.assertIn('"path": "../../core/agent"', installed_host_leaf)
            host_tsconfig = (harness / "tsconfig.host.json").read_text(encoding="utf-8")
            self.assertEqual(
                host_tsconfig.count('./packages/host/codex'), 1
            )
            patch = (harness / "packages/bundle/web-app/cordis.patch.yml").read_text(encoding="utf-8")
            self.assertEqual(patch.count("id: ui-voice"), 1)
            self.assertEqual(patch.count("id: codex"), 1)
            api_proxy_text = (harness / "packages/host/apiproxy/src/api-proxy.ts").read_text()
            self.assertIn("'codex/user-start'", api_proxy_text)
            self.assertIn("stateVersion: 2", api_proxy_text)
            client_manager_text = (harness / "packages/client/runtime/src/client/sessions/manager.ts").read_text()
            self.assertIn("frame.key === 'sessionListMetadata'", client_manager_text)
            package = json.loads((harness / "packages/bundle/web-app/package.json").read_text())
            self.assertEqual(
                package["dependencies"]["@deepseek-ai/dsh-client-ui-voice"], "workspace:^"
            )
            self.assertEqual(package["dependencies"][HOST_PACKAGE], "workspace:^")
            api_client = (harness / "packages/api/remotes/src/client/index.ts").read_text()
            self.assertEqual(api_client.count("'@deepseek-ai/dsh-host-codex/remote'"), 2)
            self.assertEqual(api_client.count("@deepseek-ai/dsh-host-codex/remote-types"), 1)
            self.assertEqual(api_client.count("@deepseek-ai/dsh-host-codex/event-types"), 1)
            self.assertNotIn("@deepseek-ai/dsh-host-codex/types", api_client)

    def test_installer_rejects_file_drift_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh drift ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            command = installer_command(harness, make_host_source(base))
            self.assertEqual(run(command).returncode, 0)
            changed = harness / "packages/client/ui-voice/src/index.ts"
            changed.write_text("user modification\n", encoding="utf-8")
            result = run(command)
            self.assertEqual(result.returncode, 2)
            self.assertIn("drifted", result.stderr)
            self.assertEqual(changed.read_text(encoding="utf-8"), "user modification\n")

    def test_installer_migrates_legacy_three_anchor_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh anchor migration ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            command = installer_command(harness, make_host_source(base))
            self.assertEqual(run(command).returncode, 0)

            client_path = harness / "tsconfig.client.json"
            client_path.write_text(
                client_path.read_text(encoding="utf-8").replace(
                    './packages/client/ui-voice" },',
                    './packages/client/ui-voice/tsconfig.client.json" },',
                ),
                encoding="utf-8",
            )
            host_path = harness / "tsconfig.host.json"
            host_path.write_text(
                host_path.read_text(encoding="utf-8").replace(
                    '    { "path": "./packages/host/codex" },\n',
                    "",
                ),
                encoding="utf-8",
            )
            manifest_path = harness / "packages/client/ui-voice/.dsh-managed/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in [
                "tsconfig.host.json",
                "packages/api/remotes/src/client/index.ts",
                "packages/api/remotes/package.json",
                "packages/api/remotes/tsconfig.client.json",
                "packages/host/apiproxy/src/api-proxy.ts",
                "packages/client/runtime/src/client/sessions/manager.ts",
            ]:
                manifest["registrations"].pop(key)
            manifest["registrations"]["tsconfig.client.json"] = hashlib.sha256(
                client_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

            migrated = run(command)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(
                set(json.loads(manifest_path.read_text())["registrations"]),
                {
                    "tsconfig.client.json",
                    "tsconfig.host.json",
                    "packages/api/remotes/src/client/index.ts",
                    "packages/api/remotes/package.json",
                    "packages/api/remotes/tsconfig.client.json",
                    "packages/bundle/web-app/cordis.patch.yml",
                    "packages/bundle/web-app/package.json",
                    "packages/host/apiproxy/src/api-proxy.ts",
                    "packages/client/runtime/src/client/sessions/manager.ts",
                },
            )
            self.assertIn('./packages/client/ui-voice"', client_path.read_text())
            self.assertIn('./packages/host/codex', host_path.read_text())

    def test_installer_rejects_manifestless_modified_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh manifestless drift ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            command = installer_command(harness, make_host_source(base))
            self.assertEqual(run(command).returncode, 0)
            manifest = harness / "packages/client/ui-voice/.dsh-managed/manifest.json"
            manifest.unlink()
            changed = harness / "packages/client/ui-voice/src/index.ts"
            changed.write_text("legacy user modification\n", encoding="utf-8")
            result = run(command)
            self.assertEqual(result.returncode, 2)
            self.assertIn("manifestless", result.stderr)
            self.assertEqual(changed.read_text(encoding="utf-8"), "legacy user modification\n")

    def test_installer_rejects_registration_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh registration drift ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            command = installer_command(harness, make_host_source(base))
            self.assertEqual(run(command).returncode, 0)
            tsconfig = harness / "tsconfig.client.json"
            tsconfig.write_text(
                tsconfig.read_text(encoding="utf-8").replace(
                    './packages/client/ui-trajectory" },', './packages/client/ui-trajectory" }'
                ),
                encoding="utf-8",
            )
            result = run(command)
            self.assertEqual(result.returncode, 2)
            self.assertIn("anchor", result.stderr)

    def test_installer_allows_safe_source_upgrade_but_rejects_target_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh managed upgrade ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            source = base / "plugin source with spaces"
            shutil.copytree(ROOT / "dsh-plugin", source)
            command = installer_command(harness, host_source, "--source", str(source))
            self.assertEqual(run(command).returncode, 0)

            source_file = source / "src/index.ts"
            source_file.write_text(source_file.read_text(encoding="utf-8") + "\n// safe upgrade\n", encoding="utf-8")
            source_package = source / "package.json"
            source_package.write_text(source_package.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            upgraded = run(command)
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            target_file = harness / "packages/client/ui-voice/src/index.ts"
            self.assertIn("safe upgrade", target_file.read_text(encoding="utf-8"))
            self.assertTrue(
                (harness / "packages/client/ui-voice/package.json").read_text(
                    encoding="utf-8"
                ).endswith("\n\n")
            )

            target_file.write_text("user edit must survive\n", encoding="utf-8")
            source_file.write_text(source_file.read_text(encoding="utf-8") + "// second source revision\n", encoding="utf-8")
            refused = run(command)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("drifted", refused.stderr)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "user edit must survive\n")

    def test_installer_rejects_missing_or_symlinked_host_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh host source gate ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            missing = base / "missing host source"
            missing_result = run(installer_command(harness, missing))
            self.assertEqual(missing_result.returncode, 2)
            self.assertIn("Codex Host source does not exist", missing_result.stderr)

            real_source = make_host_source(base)
            link = base / "host source link"
            link.symlink_to(real_source, target_is_directory=True)
            linked_result = run(installer_command(harness, link))
            self.assertEqual(linked_result.returncode, 2)
            self.assertIn("must not be a symlink", linked_result.stderr)
            self.assertFalse((harness / "packages/host/codex").exists())

    def test_installer_rejects_host_parent_symlink_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh host parent gate ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            external = base / "external host target"
            external.mkdir()
            shutil.rmtree(harness / "packages" / "host")
            (harness / "packages" / "host").symlink_to(external, target_is_directory=True)
            result = run(installer_command(harness, host_source))
            self.assertEqual(result.returncode, 2)
            self.assertIn("Codex Host target parent must not be a symlink", result.stderr)
            self.assertEqual(list(external.iterdir()), [])

    def test_installer_rejects_host_tree_drift_and_legacy_unproven_host(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh host drift ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            command = installer_command(harness, host_source)
            self.assertEqual(run(command).returncode, 0)
            changed = harness / "packages/host/codex/src/index.ts"
            changed.write_text("user host edit\n", encoding="utf-8")
            drift = run(command)
            self.assertEqual(drift.returncode, 2)
            self.assertIn("Codex Host target drifted", drift.stderr)

            # A pre-split manifest has no host provenance.  Keeping a Host
            # target file under it must fail closed rather than be adopted.
            manifest_path = harness / "packages/client/ui-voice/.dsh-managed/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("hostFiles")
            manifest.pop("hostSourceTreeSha256")
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            changed.unlink()
            legacy = run(command)
            self.assertEqual(legacy.returncode, 2)
            self.assertIn("Codex Host target contains files outside its managed manifest", legacy.stderr)

    def test_installer_migrates_legacy_manifest_with_generated_lib_and_rejects_bypass_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh generated output ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            command = installer_command(harness, host_source)
            self.assertEqual(run(command).returncode, 0)

            # A pre-split managed manifest is still valid provenance.  Build
            # output from the previous install must not be treated as a user
            # edit when the manifest is migrated to the current registration
            # shape.
            manifest_path = harness / "packages/client/ui-voice/.dsh-managed/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in [
                "tsconfig.host.json",
                "packages/api/remotes/src/client/index.ts",
                "packages/api/remotes/package.json",
                "packages/api/remotes/tsconfig.client.json",
                "packages/host/apiproxy/src/api-proxy.ts",
                "packages/client/runtime/src/client/sessions/manager.ts",
            ]:
                manifest["registrations"].pop(key)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

            for target in [
                harness / "packages/client/ui-voice",
                harness / "packages/host/codex",
            ]:
                output = target / "lib" / "index.js"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("// pinned build output\n", encoding="utf-8")

            migrated = run(command)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertTrue(
                (harness / "packages/client/ui-voice/lib/index.js").is_file()
            )
            self.assertTrue((harness / "packages/host/codex/lib/index.js").is_file())

            # Only the exact generated top-level directories are ignored;
            # similarly named paths and source-tree additions remain drift.
            bypasses = [
                (
                    harness / "packages/client/ui-voice" / "lib-extra" / "rogue.js",
                    "plugin target contains unexpected files",
                ),
                (
                    harness / "packages/host/codex" / "src" / "rogue.js",
                    "Codex Host target contains unexpected files",
                ),
            ]
            for path, message in bypasses:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("user drift\n", encoding="utf-8")
                refused = run(command)
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertIn(message, refused.stderr)
                path.unlink()

    def test_installer_rejects_generated_directory_symlink_for_client_and_host(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh generated symlink ") as temporary:
            base = Path(temporary)
            harness = make_harness_fixture(base)
            host_source = make_host_source(base)
            command = installer_command(harness, host_source)
            self.assertEqual(run(command).returncode, 0)

            for target, label in [
                (harness / "packages/client/ui-voice", "plugin"),
                (harness / "packages/host/codex", "Codex Host"),
            ]:
                external = base / f"{label.replace(' ', '-')}-lib"
                external.mkdir()
                (external / "index.js").write_text("external output\n", encoding="utf-8")
                existing = target / "lib"
                if existing.is_dir() and not existing.is_symlink():
                    shutil.rmtree(existing)
                (target / "lib").symlink_to(external, target_is_directory=True)
                refused = run(command)
                self.assertEqual(refused.returncode, 2, refused.stderr)
                self.assertIn("target contains symlink(s)", refused.stderr)
                self.assertTrue((external / "index.js").is_file())
                (target / "lib").unlink()

    def test_bootstrap_dry_run_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh dry run ") as temporary:
            base = Path(temporary)
            lock = base / "dsh.lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": {"url": str(base / "unused-origin.git"), "commit": "a" * 40},
                        "plugin": {
                            "source": "dsh-plugin",
                            "target": "packages/client/ui-voice",
                            "packageName": "@deepseek-ai/dsh-client-ui-voice",
                        },
                        "host": {
                            "source": "dsh-host-codex",
                            "target": "packages/host/codex",
                            "packageName": HOST_PACKAGE,
                        },
                    }
                ),
                encoding="utf-8",
            )
            target = base / "target with spaces"
            environment = {**os.environ, "DSH_LOCK_FILE": str(lock)}
            result = run(
                [str(BOOTSTRAP), "--harness", str(target), "--dry-run", "--skip-install", "--skip-build"],
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would install overlay", result.stdout)
            self.assertFalse(target.exists())

    def test_bootstrap_prepares_local_pinned_fixture_and_is_dirty_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh bootstrap ") as temporary:
            base = Path(temporary)
            origin, commit = make_git_origin(base)
            host_source = make_host_source(base)
            lock = base / "dsh.lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": {"url": str(origin), "commit": commit},
                        "plugin": {
                            "source": "dsh-plugin",
                            "target": "packages/client/ui-voice",
                            "packageName": "@deepseek-ai/dsh-client-ui-voice",
                        },
                        "host": {
                            "source": "dsh-host-codex",
                            "target": "packages/host/codex",
                            "packageName": HOST_PACKAGE,
                        },
                    }
                ),
                encoding="utf-8",
            )
            target = base / "new target with spaces"
            environment = {
                **os.environ,
                "DSH_LOCK_FILE": str(lock),
                "DSH_HOST_SOURCE": str(host_source),
            }
            result = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-install", "--skip-build"],
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip(), commit)
            self.assertTrue((target / "packages/client/ui-voice/src/index.ts").is_file())
            rerun = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-install", "--skip-build"],
                env=environment,
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)

            dirty = target / "user-change.txt"
            dirty.write_text("keep me\n", encoding="utf-8")
            before = dirty.read_bytes()
            refused = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-install", "--skip-build"],
                env=environment,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("dirty", refused.stderr)
            self.assertEqual(dirty.read_bytes(), before)

    def test_bootstrap_reconciles_lock_once_then_uses_frozen_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dsh managed lock ") as temporary:
            base = Path(temporary)
            origin, commit = make_git_origin(base)
            host_source = make_host_source(base)
            lock = base / "dsh.lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "repository": {"url": str(origin), "commit": commit},
                        "plugin": {
                            "source": "dsh-plugin",
                            "target": "packages/client/ui-voice",
                            "packageName": "@deepseek-ai/dsh-client-ui-voice",
                        },
                        "host": {
                            "source": "dsh-host-codex",
                            "target": "packages/host/codex",
                            "packageName": HOST_PACKAGE,
                        },
                    }
                ),
                encoding="utf-8",
            )
            fake_bin = base / "fake bin"
            fake_bin.mkdir()
            log = base / "pnpm calls.log"
            fake_pnpm = fake_bin / "pnpm"
            fake_pnpm.write_text(
                """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_PNPM_LOG"
if [ "$1" = "--version" ]; then
  printf '%s\\n' "$FAKE_PNPM_VERSION"
  exit 0
fi
if [ "$1" = "install" ] && [ "$2" = "--lockfile-only" ]; then
  cat >> pnpm-lock.yaml <<'EOF'
  packages/api/remotes:
    dependencies:
      '@deepseek-ai/dsh-host-codex':
        specifier: workspace:^
        version: link:../../host/codex
  packages/client/ui-voice:
    dependencies:
      '@deepseek-ai/dsh-client-ui-model-selection':
        specifier: workspace:^
        version: link:../ui-model-selection
  packages/host/codex:
    dependencies: {}
EOF
fi
""",
                encoding="utf-8",
            )
            fake_pnpm.chmod(0o755)
            fake_npm = fake_bin / "npm"
            fake_npm.write_text(
                """#!/bin/sh
set -eu
[ "$1" = "exec" ]
shift
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then
    shift
    break
  fi
  shift
done
[ "$1" = "pnpm" ]
shift
exec "$FAKE_PNPM_BIN" "$@"
""",
                encoding="utf-8",
            )
            fake_npm.chmod(0o755)
            target = base / "managed lock target"
            environment = {
                **os.environ,
                "DSH_LOCK_FILE": str(lock),
                "DSH_HOST_SOURCE": str(host_source),
                "FAKE_PNPM_LOG": str(log),
                "FAKE_PNPM_VERSION": "9.0.0",
                "FAKE_PNPM_BIN": str(fake_pnpm),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
            }
            first = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("lockfile-only --no-frozen-lockfile", first.stdout)
            self.assertIn("pnpm install --frozen-lockfile", first.stdout)
            managed = json.loads(
                (target / "packages/client/ui-voice/.dsh-managed/manifest.json").read_text()
            )
            self.assertEqual(managed["lock"]["pnpmVersion"], "9.0.0")
            self.assertEqual(
                set(managed["catalog"]["files"]),
                {
                    "docs/persistence-catalog.md",
                    "packages/core/session/src/known-event-types.ts",
                },
            )
            self.assertIn("packages/client/ui-voice:", (target / "pnpm-lock.yaml").read_text())

            second = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("verified managed pnpm-lock.yaml", second.stdout)
            self.assertNotIn("lockfile-only --no-frozen-lockfile", second.stdout)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("install --lockfile-only --no-frozen-lockfile"), 1)

            # Simulate a checkout produced by the pre-split four-anchor
            # installer: its managed digest is valid, but the lockfile lacks
            # the new Host/API importer semantics.  An unrelated lock edit
            # must remain a hard stop and must not trigger reconciliation.
            registration_paths = [
                "tsconfig.client.json",
                "tsconfig.host.json",
                "packages/api/remotes/src/client/index.ts",
                "packages/api/remotes/package.json",
                "packages/api/remotes/tsconfig.client.json",
                "packages/bundle/web-app/cordis.patch.yml",
                "packages/bundle/web-app/package.json",
                "packages/host/apiproxy/src/api-proxy.ts",
                "packages/client/runtime/src/client/sessions/manager.ts",
            ]
            upstream_registration = {
                relative: run(["git", "show", f"HEAD:{relative}"], cwd=target).stdout
                for relative in registration_paths
            }
            legacy_registration_paths = [
                "tsconfig.client.json",
                "tsconfig.host.json",
                "packages/bundle/web-app/cordis.patch.yml",
                "packages/bundle/web-app/package.json",
            ]
            for relative, content in upstream_registration.items():
                (target / relative).write_text(content, encoding="utf-8")
            legacy_lock = (
                "lockfileVersion: '9.0'\n"
                "importers:\n"
                "  packages/client/ui-trajectory: {}\n"
            )
            lock_path = target / "pnpm-lock.yaml"
            lock_path.write_text(legacy_lock, encoding="utf-8")
            legacy_manifest_path = target / "packages/client/ui-voice/.dsh-managed/manifest.json"
            legacy_manifest = json.loads(legacy_manifest_path.read_text())
            legacy_manifest["registrations"] = {
                relative: hashlib.sha256(
                    upstream_registration[relative].encode("utf-8")
                ).hexdigest()
                for relative in legacy_registration_paths
            }
            legacy_manifest["lock"]["sha256"] = hashlib.sha256(
                legacy_lock.encode("utf-8")
            ).hexdigest()
            legacy_manifest_path.write_text(
                json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n"
            )

            lock_path.write_text(legacy_lock + "# unrelated lock drift\n", encoding="utf-8")
            malicious = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(malicious.returncode, 2)
            self.assertIn("dirty without a matching managed lock digest", malicious.stderr)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines().count(
                    "install --lockfile-only --no-frozen-lockfile"
                ),
                1,
            )
            lock_path.write_text(legacy_lock, encoding="utf-8")

            migrated = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertIn("legacy plugin importer semantics", migrated.stdout)
            migrated_manifest = json.loads(legacy_manifest_path.read_text())
            self.assertEqual(
                set(migrated_manifest["registrations"]), set(registration_paths)
            )
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines().count(
                    "install --lockfile-only --no-frozen-lockfile"
                ),
                2,
            )

            migrated_rerun = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(migrated_rerun.returncode, 0, migrated_rerun.stderr)
            self.assertIn("verified managed pnpm-lock.yaml", migrated_rerun.stdout)
            self.assertNotIn("legacy plugin importer semantics", migrated_rerun.stdout)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines().count(
                    "install --lockfile-only --no-frozen-lockfile"
                ),
                2,
            )

            generated = target / "docs/persistence-catalog.md"
            generated.write_text("user edit must not be overwritten\n", encoding="utf-8")
            drift = run(
                [str(BOOTSTRAP), "--harness", str(target), "--skip-build"],
                env=environment,
            )
            self.assertEqual(drift.returncode, 2)
            self.assertIn("generated persistence catalog drifted", drift.stderr)
            self.assertEqual(
                generated.read_text(encoding="utf-8"),
                "user edit must not be overwritten\n",
            )

            mismatch_target = base / "version mismatch target"
            mismatch_environment = {**environment, "FAKE_PNPM_VERSION": "9.1.0"}
            mismatch = run(
                [str(BOOTSTRAP), "--harness", str(mismatch_target), "--skip-build"],
                env=mismatch_environment,
            )
            self.assertEqual(mismatch.returncode, 2)
            self.assertIn("resolved pnpm 9.1.0, expected 9.0.0", mismatch.stderr)
            mismatch_calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                mismatch_calls.count("install --lockfile-only --no-frozen-lockfile"), 2
            )


if __name__ == "__main__":
    unittest.main()
