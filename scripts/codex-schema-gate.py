#!/usr/bin/env python3
"""Reproduce and verify the pinned stable Codex app-server schema surface.

This gate deliberately speaks only to the stable generator.  It never passes
``--experimental`` and never starts an app-server, performs auth, or prints a
generator response.  This script owns exact version attestation, reproducible
generation, artifact/hash comparison, and generated TypeScript compilation.
At runtime, ``StableProtocolValidator`` independently verifies the checked-in
bundle hash and applies the closed per-method business allowlist; this script
does not start the provider or App Server.

Usage::

    python3 scripts/codex-schema-gate.py check
    python3 scripts/codex-schema-gate.py generate --out /absolute/nonexistent/child
    python3 scripts/codex-schema-gate.py compile --tsc /path/to/tsc

``generate`` first verifies the exact canonical CLI version, then creates the
previously nonexistent ``--out`` child and two independent generator outputs
below it.  Existing output paths and symlinked path components are rejected,
so stale or redirected output cannot be mistaken for a fresh stable
generation.  The result can then be compared with the checked-in tree using
``check --generated /absolute/generated/child``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFEST_RELATIVE = Path("agents/codex/protocol-manifest.json")
DEFAULT_SCHEMA_RELATIVE = Path(
    "agents/codex/generated/stable/codex_app_server_protocol.v2.schemas.json"
)
DEFAULT_JSON_ROOT_RELATIVE = Path("agents/codex/generated/stable")
DEFAULT_TS_ROOT_RELATIVE = Path("agents/codex/generated/stable-ts")
EXPECTED_SCHEMA_FILE = "codex_app_server_protocol.v2.schemas.json"
EXPECTED_CLI_VERSION = "0.149.0-alpha.4.1"
EXPECTED_CLI_BANNER = f"codex-cli {EXPECTED_CLI_VERSION}\n"
EXPECTED_SURFACE = "stable"
EXPECTED_TS_ENTRYPOINT = "index.ts"
REQUIRED_CLIENT_METHODS = (
    "initialize",
    "account/read",
    "account/login/start",
    "account/login/cancel",
    "model/list",
    "thread/start",
    "thread/resume",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
)
REQUIRED_CLIENT_NOTIFICATIONS = ("initialized",)
REQUIRED_SERVER_REQUESTS = (
    "account/chatgptAuthTokens/refresh",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
)
REQUIRED_SERVER_NOTIFICATIONS = (
    "remoteControl/status/changed",
    "account/updated",
    "account/rateLimits/updated",
    "account/login/completed",
    "mcpServer/startupStatus/updated",
    "skills/changed",
    "thread/goal/cleared",
    "thread/settings/updated",
    "thread/status/changed",
    "thread/started",
    "thread/tokenUsage/updated",
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
    "item/agentMessage/delta",
    "serverRequest/resolved",
)


class SchemaGateError(RuntimeError):
    """A deterministic, user-actionable schema gate failure."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink():
        raise SchemaGateError(f"schema artifact must not be a symlink: {path}")
    try:
        return _sha256_bytes(path.read_bytes())
    except (OSError, UnicodeError) as exc:
        raise SchemaGateError(f"cannot read schema artifact: {path}") from exc


def tree_digest(root: Path, *, suffix: str = ".ts") -> tuple[str, int, int]:
    """Return a stable digest over relative names and individual file hashes.

    File names are NUL-separated from their hash and records are newline
    terminated.  Sorting uses POSIX relative names, making the digest
    independent of filesystem traversal order and platform path separators.
    """

    if root.is_symlink() or not root.is_dir():
        raise SchemaGateError(f"generated artifact directory is missing: {root}")
    records: list[bytes] = []
    file_count = 0
    byte_count = 0
    for path in sorted(root.rglob(f"*{suffix}"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise SchemaGateError(f"generated artifact must not be a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        file_count += 1
        byte_count += len(data)
        records.append(f"{relative}\0{_sha256_bytes(data)}\n".encode("utf-8"))
    if not records:
        raise SchemaGateError(f"generated artifact directory is empty: {root}")
    return _sha256_bytes(b"".join(records)), file_count, byte_count


def _load_manifest(root: Path) -> Mapping[str, Any]:
    path = root / MANIFEST_RELATIVE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaGateError(f"protocol manifest is unavailable or invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise SchemaGateError("protocol manifest must be a JSON object")
    return value


def _relative_path(value: object, fallback: Path, *, label: str) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise SchemaGateError(f"manifest {label} must be a relative path")
    return Path(value)


def _artifact_layout(root: Path, manifest: Mapping[str, Any]) -> tuple[Path, Path, Path, str]:
    schema_rel = _relative_path(
        manifest.get("generatedSchema"),
        DEFAULT_SCHEMA_RELATIVE,
        label="generatedSchema",
    )
    generated_types = manifest.get("generatedTypes")
    if generated_types is None:
        generated_types = {}
    if not isinstance(generated_types, Mapping):
        raise SchemaGateError("manifest generatedTypes must be an object")
    types_rel = _relative_path(
        generated_types.get("root"),
        DEFAULT_TS_ROOT_RELATIVE,
        label="generatedTypes.root",
    )
    json_root_rel = _relative_path(
        manifest.get("generatedSchemaRoot"),
        DEFAULT_JSON_ROOT_RELATIVE,
        label="generatedSchemaRoot",
    )
    schema_name = manifest.get("schemaBundle", EXPECTED_SCHEMA_FILE)
    if not isinstance(schema_name, str) or schema_name != EXPECTED_SCHEMA_FILE:
        raise SchemaGateError("stable schemaBundle must be the v2 Codex bundle")
    return root / schema_rel, root / json_root_rel, root / types_rel, schema_name


def _assert_no_experimental_flag(arguments: Sequence[str]) -> None:
    if "--experimental" in arguments:
        raise SchemaGateError("stable schema generation must not use --experimental")


def _absolute_without_symlink_resolution(path: Path) -> Path:
    """Return an absolute lexical path without hiding symlink components."""

    if ".." in path.parts:
        # Normalizing parent traversal before lstat would hide a symlink that
        # the kernel resolves before applying `..`.
        raise SchemaGateError("generation output path must not contain parent traversal")
    return path if path.is_absolute() else Path.cwd() / path


def _assert_no_symlink_components(path: Path) -> None:
    """Reject any existing symlink from the filesystem root through ``path``."""

    absolute = _absolute_without_symlink_resolution(path)
    current = Path(absolute.anchor)
    components = absolute.parts[1:]
    for index, component in enumerate(components):
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # Once a component is absent no deeper component can exist.
            return
        except OSError as exc:
            raise SchemaGateError("generation output path could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SchemaGateError("generation output path contains a symlink")
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise SchemaGateError("generation output ancestor is not a directory")


def _assert_codex_version(binary: str) -> None:
    """Accept only the exact, canonical banner from the pinned CLI build."""

    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SchemaGateError("Codex version command could not be executed") from exc
    if result.returncode != 0 or result.stdout != EXPECTED_CLI_BANNER or result.stderr != "":
        raise SchemaGateError("Codex CLI version is not the exact pinned canonical build")


def _prepare_generation_output(output: Path) -> Path:
    output = _absolute_without_symlink_resolution(output)
    _assert_no_symlink_components(output)
    if os.path.lexists(output):
        raise SchemaGateError("generation output must not already exist")
    parent = output.parent
    if not parent.is_dir():
        raise SchemaGateError("generation output parent must already exist")
    try:
        output.mkdir(mode=0o700)
    except OSError as exc:
        raise SchemaGateError("generation output could not be created") from exc
    _assert_no_symlink_components(output)
    return output


def _create_generator_child(output: Path, name: str) -> Path:
    _assert_no_symlink_components(output)
    child = output / name
    if os.path.lexists(child):
        raise SchemaGateError("stable generator child must not already exist")
    try:
        child.mkdir(mode=0o700)
    except OSError as exc:
        raise SchemaGateError("stable generator child directory could not be created") from exc
    _assert_no_symlink_components(child)
    return child


def _run_generator(binary: str, subcommand: str, output: Path) -> None:
    arguments = [binary, "app-server", subcommand, "--out", os.fspath(output)]
    _assert_no_experimental_flag(arguments)
    try:
        result = subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SchemaGateError(f"Codex generator could not be executed: {subcommand}") from exc
    if result.returncode != 0:
        # Do not surface generator stderr: future binaries may include local
        # paths or auth/configuration details in diagnostics.
        raise SchemaGateError(f"Codex stable generator failed: {subcommand}")


def generate(binary: str, output: Path) -> None:
    """Generate stable JSON and TS artifacts in a tool-owned fresh child."""

    # Version is deliberately the first subprocess and happens before any
    # filesystem mutation. A wrong or malformed binary cannot generate data.
    _assert_codex_version(binary)
    output = _prepare_generation_output(output)
    json_root = _create_generator_child(output, "stable-json")
    _run_generator(binary, "generate-json-schema", json_root)
    tree_digest(json_root, suffix=".json")
    ts_root = _create_generator_child(output, "stable-ts")
    _run_generator(binary, "generate-ts", ts_root)
    tree_digest(json_root, suffix=".json")
    tree_digest(ts_root)
    schema = json_root / EXPECTED_SCHEMA_FILE
    if not schema.is_file():
        raise SchemaGateError("stable generator did not produce the v2 JSON schema bundle")


def _assert_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    if manifest.get("manifestVersion") != 1:
        raise SchemaGateError("unsupported protocol manifest version")
    if manifest.get("surface") != EXPECTED_SURFACE:
        raise SchemaGateError("protocol manifest is not stable-only")
    if manifest.get("codexCliVersion") != EXPECTED_CLI_VERSION:
        raise SchemaGateError("protocol manifest Codex version is not the pinned build")
    if manifest.get("experimentalApi") is not False:
        raise SchemaGateError("protocol manifest enables experimental API")
    if manifest.get("requestAttestation") is not False:
        raise SchemaGateError("protocol manifest enables request attestation")
    schema_hash = manifest.get("schemaSha256")
    if not isinstance(schema_hash, str) or len(schema_hash) != 64:
        raise SchemaGateError("protocol manifest schemaSha256 is invalid")
    generated_types = manifest.get("generatedTypes")
    if not isinstance(generated_types, Mapping):
        raise SchemaGateError("protocol manifest generatedTypes metadata is missing")
    if generated_types.get("entrypoint") != EXPECTED_TS_ENTRYPOINT:
        raise SchemaGateError("generated TypeScript entrypoint is not index.ts")
    generated_schema_tree = manifest.get("generatedSchemaTree")
    if not isinstance(generated_schema_tree, Mapping):
        raise SchemaGateError("protocol manifest generatedSchemaTree metadata is missing")
    if generated_schema_tree.get("root") != manifest.get("generatedSchemaRoot"):
        raise SchemaGateError("generated schema tree root is inconsistent")
    if not isinstance(generated_schema_tree.get("fileCount"), int) or not isinstance(
        generated_schema_tree.get("byteCount"), int
    ):
        raise SchemaGateError("generated schema tree size metadata is invalid")
    if not isinstance(generated_schema_tree.get("treeSha256"), str) or len(
        generated_schema_tree["treeSha256"]
    ) != 64:
        raise SchemaGateError("generated schema tree hash metadata is invalid")
    required_wire = manifest.get("requiredWire")
    if not isinstance(required_wire, Mapping):
        raise SchemaGateError("protocol manifest requiredWire metadata is missing")
    for key, expected in (
        ("clientRequests", REQUIRED_CLIENT_METHODS),
        ("clientNotifications", REQUIRED_CLIENT_NOTIFICATIONS),
        ("serverRequests", REQUIRED_SERVER_REQUESTS),
        ("serverNotifications", REQUIRED_SERVER_NOTIFICATIONS),
    ):
        actual = required_wire.get(key)
        if (
            not isinstance(actual, list)
            or not all(isinstance(item, str) for item in actual)
            or set(actual) != set(expected)
            or len(actual) != len(expected)
        ):
            raise SchemaGateError(f"protocol manifest requiredWire.{key} is invalid")
    return generated_types


def _assert_json_tree(
    json_root: Path,
    schema: Path,
    expected_hash: str,
    generated_schema_tree: Mapping[str, Any],
) -> None:
    if not json_root.is_dir():
        raise SchemaGateError(f"generated JSON directory is missing: {json_root}")
    if not schema.is_file():
        raise SchemaGateError(f"stable JSON schema bundle is missing: {schema}")
    if _sha256_file(schema) != expected_hash:
        raise SchemaGateError("stable JSON schema bundle hash does not match protocol manifest")
    files = sorted(path.relative_to(json_root).as_posix() for path in json_root.rglob("*.json") if path.is_file())
    digest, file_count, byte_count = tree_digest(json_root, suffix=".json")
    if file_count != generated_schema_tree.get("fileCount") or len(files) != file_count:
        raise SchemaGateError(f"stable JSON generated file count changed: {file_count}")
    if byte_count != generated_schema_tree.get("byteCount"):
        raise SchemaGateError("stable JSON generated byte count changed")
    if digest != generated_schema_tree.get("treeSha256"):
        raise SchemaGateError("stable JSON generated tree hash changed")
    if EXPECTED_SCHEMA_FILE not in files or "v1/InitializeParams.json" not in files:
        raise SchemaGateError("stable JSON generated closure is incomplete")


def _assert_ts_tree(ts_root: Path, generated_types: Mapping[str, Any]) -> None:
    digest, file_count, byte_count = tree_digest(ts_root)
    if file_count != generated_types.get("fileCount"):
        raise SchemaGateError("stable TypeScript generated file count does not match manifest")
    if byte_count != generated_types.get("byteCount"):
        raise SchemaGateError("stable TypeScript generated byte count does not match manifest")
    if digest != generated_types.get("treeSha256"):
        raise SchemaGateError("stable TypeScript generated tree hash does not match manifest")
    entrypoint = ts_root / EXPECTED_TS_ENTRYPOINT
    if not entrypoint.is_file():
        raise SchemaGateError("stable TypeScript generated entrypoint is missing")


def _assert_required_wire_surface(ts_root: Path) -> None:
    files = {
        "clientRequests": ts_root / "ClientRequest.ts",
        "clientNotifications": ts_root / "ClientNotification.ts",
        "serverRequests": ts_root / "ServerRequest.ts",
        "serverNotifications": ts_root / "ServerNotification.ts",
    }
    for label, path in files.items():
        if not path.is_file():
            raise SchemaGateError(f"stable generated wire type is missing: {label}")
    content = {label: path.read_text(encoding="utf-8") for label, path in files.items()}
    for method in REQUIRED_CLIENT_METHODS:
        if f'"method": "{method}"' not in content["clientRequests"]:
            raise SchemaGateError(f"stable generated ClientRequest is missing: {method}")
    for method in REQUIRED_CLIENT_NOTIFICATIONS:
        if f'"method": "{method}"' not in content["clientNotifications"]:
            raise SchemaGateError(f"stable generated ClientNotification is missing: {method}")
    for method in REQUIRED_SERVER_REQUESTS:
        if f'"method": "{method}"' not in content["serverRequests"]:
            raise SchemaGateError(f"stable generated ServerRequest is missing: {method}")
    for method in REQUIRED_SERVER_NOTIFICATIONS:
        if f'"method": "{method}"' not in content["serverNotifications"]:
            raise SchemaGateError(f"stable generated ServerNotification is missing: {method}")


def _compare_generated(check_root: Path, json_root: Path, ts_root: Path) -> None:
    check_root = _absolute_without_symlink_resolution(check_root)
    _assert_no_symlink_components(check_root)
    if not check_root.is_dir():
        raise SchemaGateError("fresh generated output is not a directory")
    generated_json = check_root / "stable-json"
    generated_ts = check_root / "stable-ts"
    tree_digest(generated_json, suffix=".json")
    tree_digest(generated_ts)
    expected_json = sorted(path.relative_to(generated_json).as_posix() for path in generated_json.rglob("*.json"))
    actual_json = sorted(path.relative_to(json_root).as_posix() for path in json_root.rglob("*.json"))
    if expected_json != actual_json:
        raise SchemaGateError("fresh stable JSON file set differs from checked-in artifact")
    for relative in expected_json:
        if _sha256_file(generated_json / relative) != _sha256_file(json_root / relative):
            raise SchemaGateError(f"fresh stable JSON artifact differs: {relative}")
    expected_files = sorted(path.relative_to(generated_ts).as_posix() for path in generated_ts.rglob("*.ts"))
    actual_files = sorted(path.relative_to(ts_root).as_posix() for path in ts_root.rglob("*.ts"))
    if expected_files != actual_files:
        raise SchemaGateError("fresh stable TypeScript file set differs from checked-in artifact")
    for relative in expected_files:
        if _sha256_file(generated_ts / relative) != _sha256_file(ts_root / relative):
            raise SchemaGateError(f"fresh stable TypeScript artifact differs: {relative}")


def check(root: Path, *, generated: Path | None = None) -> None:
    manifest = _load_manifest(root)
    generated_types = _assert_manifest(manifest)
    schema, json_root, ts_root, _schema_name = _artifact_layout(root, manifest)
    _assert_json_tree(
        json_root,
        schema,
        str(manifest["schemaSha256"]),
        manifest["generatedSchemaTree"],
    )
    _assert_ts_tree(ts_root, generated_types)
    _assert_required_wire_surface(ts_root)
    if generated is not None:
        _compare_generated(generated, json_root, ts_root)


def compile_types(root: Path, tsc: str) -> None:
    manifest = _load_manifest(root)
    _assert_manifest(manifest)
    _schema, _json_root, ts_root, _schema_name = _artifact_layout(root, manifest)
    _assert_ts_tree(ts_root, manifest["generatedTypes"])
    _assert_required_wire_surface(ts_root)
    arguments = [
        tsc,
        "--noEmit",
        "--strict",
        "--module",
        "ESNext",
        "--target",
        "ES2022",
        "--moduleResolution",
        "Bundler",
        "--skipLibCheck",
        os.fspath(ts_root / EXPECTED_TS_ENTRYPOINT),
    ]
    try:
        result = subprocess.run(arguments, cwd=root, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise SchemaGateError("TypeScript compiler could not be executed") from exc
    if result.returncode != 0:
        # Keep compiler diagnostics out of this protocol gate's output.  They
        # can contain local workspace paths and are not needed for the result.
        raise SchemaGateError("stable generated TypeScript failed to compile")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "generate", "compile"))
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, help="nonexistent child path for generate")
    parser.add_argument("--generated", type=Path, help="fresh generate output to compare during check")
    parser.add_argument("--codex", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--tsc", default=os.environ.get("TSC_BIN", "tsc"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.repo_root.resolve()
        if args.command == "generate":
            if args.out is None:
                raise SchemaGateError("generate requires --out")
            generate(args.codex, args.out)
            return 0
        if args.command == "compile":
            compile_types(root, args.tsc)
            return 0
        check(root, generated=args.generated if args.generated else None)
        return 0
    except SchemaGateError as exc:
        print(f"codex-schema-gate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
