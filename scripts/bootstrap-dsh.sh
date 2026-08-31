#!/usr/bin/env bash
# Prepare the pinned DeepSeek Harness and install this repository's voice
# plugin.  The default checkout is private runtime state under this project;
# ordinary start scripts never invoke this command automatically.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOCK_FILE="${DSH_LOCK_FILE:-${REPO_ROOT}/dsh.lock.json}"
HARNESS_INPUT="${DSH_HARNESS:-${REPO_ROOT}/.runtime/deepseek-harness}"
HOST_SOURCE_INPUT="${DSH_HOST_SOURCE:-}"
SKIP_INSTALL=0
SKIP_BUILD=0
DRY_RUN=0
PNPM_CMD=()
PNPM_VERSION=""
PINNED_PNPM_VERSION=""
PNPM_DISPLAY='npm exec --yes --package=pnpm@<pinned> -- pnpm'

usage() {
  cat <<'USAGE'
Usage: scripts/bootstrap-dsh.sh [options]

Prepare the pinned DeepSeek Harness in .runtime/deepseek-harness and install
the local dsh-plugin.  Set DSH_HARNESS or pass --harness to use another exact
checkout path.

Options:
  --harness PATH    exact checkout directory (overrides DSH_HARNESS)
  --skip-install    skip pnpm install (useful for fixture tests)
  --skip-build      skip the ui-voice typecheck/bundle steps
  --dry-run         validate inputs and print planned operations only
  -h, --help        show this help
USAGE
}

die() {
  echo "[dsh-bootstrap] ERROR: $*" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --harness)
      (($# >= 2)) || die "--harness requires a path"
      HARNESS_INPUT="$2"
      shift 2
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (try --help)"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "Python 3 is required (set PYTHON_BIN to override)"

if [[ "${HARNESS_INPUT}" != /* ]]; then
  HARNESS="${REPO_ROOT}/${HARNESS_INPUT}"
else
  HARNESS="${HARNESS_INPUT}"
fi
# Collapse `.`/`..` without following a target symlink so broad aliases such
# as `${REPO_ROOT}/.runtime/../` cannot bypass the exact-target guard.  Parent
# symlinks such as macOS's /var -> /private/var are normal and are allowed.
HARNESS="$("${PYTHON_BIN}" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "${HARNESS}")" || \
  die "cannot resolve harness target: ${HARNESS_INPUT}"
if [[ "${HARNESS}" == "/" || "${HARNESS}" == "${REPO_ROOT}" ]]; then
  die "refusing to operate on a broad target: ${HARNESS}"
fi
if [[ "${HARNESS}" == */. || "${HARNESS}" == */.. || -L "${HARNESS}" ]]; then
  die "harness target must be an exact non-symlink directory: ${HARNESS}"
fi

[[ -f "${LOCK_FILE}" ]] || die "lock manifest not found: ${LOCK_FILE}"

MANIFEST_VALUES="$("${PYTHON_BIN}" - "${LOCK_FILE}" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot parse lock manifest {path}: {exc}")

if data.get("schemaVersion") != 1:
    raise SystemExit("unsupported dsh.lock.json schemaVersion")
repository = data.get("repository")
plugin = data.get("plugin")
host = data.get("host")
if not isinstance(repository, dict) or not isinstance(plugin, dict) or not isinstance(host, dict):
    raise SystemExit("lock manifest must contain repository, plugin, and host objects")
url = repository.get("url")
commit = repository.get("commit")
source = plugin.get("source", "dsh-plugin")
target = plugin.get("target")
host_source = host.get("source")
host_target = host.get("target")
host_package = host.get("packageName")
if not isinstance(url, str) or not url:
    raise SystemExit("lock manifest repository.url must be a non-empty string")
if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
    raise SystemExit("lock manifest repository.commit must be a 40-character SHA-1")
if not isinstance(source, str) or not source or not isinstance(target, str):
    raise SystemExit("lock manifest plugin source/target fields are invalid")
if not isinstance(host_source, str) or not host_source or not isinstance(host_target, str):
    raise SystemExit("lock manifest host source/target fields are invalid")
if host_package != "@deepseek-ai/dsh-host-codex":
    raise SystemExit("lock manifest host packageName must be @deepseek-ai/dsh-host-codex")
print(f"{url}\t{commit.lower()}\t{source}\t{target}\t{host_source}\t{host_target}")
PY
)" || die "invalid lock manifest: ${LOCK_FILE}"
IFS=$'\t' read -r REPO_URL LOCK_COMMIT PLUGIN_SOURCE PLUGIN_TARGET HOST_SOURCE HOST_TARGET <<< "${MANIFEST_VALUES}"
[[ "${PLUGIN_SOURCE}" == "dsh-plugin" ]] || die "lock plugin source must be dsh-plugin"
[[ "${PLUGIN_TARGET}" == "packages/client/ui-voice" ]] || die "lock plugin target drifted"
[[ "${HOST_SOURCE}" == "dsh-host-codex" ]] || die "lock host source must be dsh-host-codex"
[[ "${HOST_TARGET}" == "packages/host/codex" ]] || die "lock host target drifted"
if [[ -z "${HOST_SOURCE_INPUT}" ]]; then
  HOST_SOURCE_INPUT="${REPO_ROOT}/${HOST_SOURCE}"
elif [[ "${HOST_SOURCE_INPUT}" != /* ]]; then
  HOST_SOURCE_INPUT="${REPO_ROOT}/${HOST_SOURCE_INPUT}"
fi

MANAGED_MANIFEST="${HARNESS}/packages/client/ui-voice/.dsh-managed/manifest.json"
PNPM_VERSION=""

normalize_url() {
  local value="$1"
  value="${value%/}"
  value="${value%.git}"
  printf '%s' "${value}"
}

# Report the state of the lockfile recorded by the plugin manifest.  The
# manifest is deliberately the authority for a generated lockfile: a missing
# record means this is the one explicit reconciliation step, a matching digest
# with legacy importer semantics needs one reconciliation, and a digest
# mismatch is a hard stop rather than an implicit lock refresh.
managed_lock_state() {
  "${PYTHON_BIN}" - "${MANAGED_MANIFEST}" "${HARNESS}/pnpm-lock.yaml" "${PNPM_VERSION}" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
pnpm_version = sys.argv[3]
if not manifest_path.is_file() or not lock_path.is_file():
    print("missing")
    raise SystemExit(0)
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = manifest.get("lock")
except (OSError, json.JSONDecodeError):
    print("mismatch")
    raise SystemExit(0)
if not isinstance(lock, dict) or lock.get("path") != "pnpm-lock.yaml":
    print("missing" if lock is None else "mismatch")
    raise SystemExit(0)
expected = lock.get("sha256")
actual = hashlib.sha256(lock_path.read_bytes()).hexdigest()
if not isinstance(expected, str) or actual != expected:
    print("mismatch")
    raise SystemExit(0)
recorded_version = lock.get("pnpmVersion")
if recorded_version and pnpm_version and recorded_version != pnpm_version:
    print("mismatch")
    raise SystemExit(0)

def importer_blocks(text: str) -> dict[str, list[str]]:
    """Read only the pinned lockfile's importer map without a YAML package."""

    blocks: dict[str, list[str]] = {}
    in_importers = False
    current = None
    for line in text.splitlines():
        if not in_importers:
            if line == "importers:":
                in_importers = True
            continue
        if line and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([^ ].*):[ \t]*", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks

def has_host_remote_specifier(block: list[str]) -> bool:
    dependency = re.compile(
        r"^      ['\"]?@deepseek-ai/dsh-host-codex['\"]?:[ \t]*$"
    )
    for index, line in enumerate(block):
        if not dependency.fullmatch(line):
            continue
        fields: list[str] = []
        for candidate in block[index + 1 :]:
            if re.match(r"^      \S", candidate):
                break
            fields.append(candidate)
        return (
            any(re.fullmatch(r"        specifier: workspace:\^[ \t]*", item) for item in fields)
            and any(
                re.fullmatch(r"        version: link:\.\./\.\./host/codex[ \t]*", item)
                for item in fields
            )
        )
    return False

def has_model_selection_specifier(block: list[str]) -> bool:
    dependency = re.compile(
        r"^      ['\"]?@deepseek-ai/dsh-client-ui-model-selection['\"]?:[ \t]*$"
    )
    for index, line in enumerate(block):
        if not dependency.fullmatch(line):
            continue
        fields: list[str] = []
        for candidate in block[index + 1 :]:
            if re.match(r"^      \S", candidate):
                break
            fields.append(candidate)
        return (
            any(re.fullmatch(r"        specifier: workspace:\^[ \t]*", item) for item in fields)
            and any(
                re.fullmatch(r"        version: link:\.\./ui-model-selection[ \t]*", item)
                for item in fields
            )
        )
    return False

def semantic_errors(text: str) -> list[str]:
    blocks = importer_blocks(text)
    required = (
        "packages/client/ui-voice",
        "packages/host/codex",
        "packages/api/remotes",
    )
    errors = [f"missing importer {name}" for name in required if name not in blocks]
    if "packages/api/remotes" in blocks and not has_host_remote_specifier(
        blocks["packages/api/remotes"]
    ):
        errors.append("missing api/remotes host dependency specifier")
    if "packages/client/ui-voice" in blocks and not has_model_selection_specifier(
        blocks["packages/client/ui-voice"]
    ):
        errors.append("missing ui-voice model-selection dependency specifier")
    return errors

try:
    semantic = semantic_errors(lock_path.read_text(encoding="utf-8"))
except OSError:
    print("mismatch")
    raise SystemExit(0)
print("reconcile" if semantic else "valid")
PY
}

# Report whether bootstrap's last generated persistence catalog is still
# intact.  The pinned checkout is intentionally left dirty by the overlay and
# by generated catalog outputs; only outputs whose exact hashes are recorded
# in the plugin manifest are accepted on a rerun.
managed_catalog_state() {
  "${PYTHON_BIN}" - "${MANAGED_MANIFEST}" "${HARNESS}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
harness = Path(sys.argv[2])
if not manifest_path.is_file():
    print("missing")
    raise SystemExit(0)
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = manifest.get("catalog")
    files = catalog.get("files") if isinstance(catalog, dict) else None
except (OSError, json.JSONDecodeError):
    print("mismatch")
    raise SystemExit(0)
expected = {
    "docs/persistence-catalog.md",
    "packages/core/session/src/known-event-types.ts",
}

if not isinstance(catalog, dict) or catalog.get("generator") != "gen-persistence-catalog":
    print("missing")
    raise SystemExit(0)
if not isinstance(files, dict) or set(files) != expected:
    print("mismatch")
    raise SystemExit(0)
for relative, digest in files.items():
    path = harness / relative
    if path.is_symlink() or not path.is_file() or not isinstance(digest, str):
        print("mismatch")
        raise SystemExit(0)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        print("mismatch")
        raise SystemExit(0)
print("valid")
PY
}

# Check one dirty overlay/registration path against the installer manifest.
# Prefix-only allowlists are unsafe: a user edit inside packages/host/codex or
# ui-voice must be rejected before checkout/fetch, while build outputs remain
# permitted only under the explicitly generated top-level directories.
managed_overlay_state() {
  local relative="$1"
  "${PYTHON_BIN}" - "${MANAGED_MANIFEST}" "${HARNESS}" "${relative}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
harness = Path(sys.argv[2])
relative = sys.argv[3]
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("missing")
    raise SystemExit(0)

expected = None
registrations = manifest.get("registrations")
if isinstance(registrations, dict):
    expected = registrations.get(relative)

for prefix, key in (
    ("packages/client/ui-voice", "files"),
    ("packages/host/codex", "hostFiles"),
):
    marker = prefix + "/"
    if relative.startswith(marker):
        child = relative[len(marker):]
        if child == ".dsh-managed/manifest.json":
            print("valid")
            raise SystemExit(0)
        if child.split("/", 1)[0] in {"lib", "node_modules", ".turbo", ".cache"}:
            print("valid")
            raise SystemExit(0)
        values = manifest.get(key)
        if isinstance(values, dict):
            expected = values.get(child)
        break

path = harness / relative
if not isinstance(expected, str) or path.is_symlink() or not path.is_file():
    print("mismatch")
    raise SystemExit(0)
actual = hashlib.sha256(path.read_bytes()).hexdigest()
print("valid" if actual == expected else "mismatch")
PY
}

verify_lock_overlay() {
  "${PYTHON_BIN}" - "${HARNESS}/pnpm-lock.yaml" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    text = path.read_text(encoding="utf-8")
except OSError as exc:
    raise SystemExit(f"cannot read generated lockfile: {exc}")

def importer_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    in_importers = False
    current = None
    for line in text.splitlines():
        if not in_importers:
            if line == "importers:":
                in_importers = True
            continue
        if line and not line.startswith(" "):
            break
        match = re.fullmatch(r"  ([^ ].*):[ \t]*", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks

def has_host_remote_specifier(block: list[str]) -> bool:
    dependency = re.compile(
        r"^      ['\"]?@deepseek-ai/dsh-host-codex['\"]?:[ \t]*$"
    )
    for index, line in enumerate(block):
        if not dependency.fullmatch(line):
            continue
        fields: list[str] = []
        for candidate in block[index + 1 :]:
            if re.match(r"^      \S", candidate):
                break
            fields.append(candidate)
        return (
            any(re.fullmatch(r"        specifier: workspace:\^[ \t]*", item) for item in fields)
            and any(
                re.fullmatch(r"        version: link:\.\./\.\./host/codex[ \t]*", item)
                for item in fields
            )
        )
    return False

def has_model_selection_specifier(block: list[str]) -> bool:
    dependency = re.compile(
        r"^      ['\"]?@deepseek-ai/dsh-client-ui-model-selection['\"]?:[ \t]*$"
    )
    for index, line in enumerate(block):
        if not dependency.fullmatch(line):
            continue
        fields: list[str] = []
        for candidate in block[index + 1 :]:
            if re.match(r"^      \S", candidate):
                break
            fields.append(candidate)
        return (
            any(re.fullmatch(r"        specifier: workspace:\^[ \t]*", item) for item in fields)
            and any(
                re.fullmatch(r"        version: link:\.\./ui-model-selection[ \t]*", item)
                for item in fields
            )
        )
    return False

blocks = importer_blocks(text)
required = (
    "packages/client/ui-voice",
    "packages/host/codex",
    "packages/api/remotes",
)
missing = [name for name in required if name not in blocks]
if "packages/api/remotes" in blocks and not has_host_remote_specifier(
    blocks["packages/api/remotes"]
):
    missing.append("packages/api/remotes:@deepseek-ai/dsh-host-codex")
if "packages/client/ui-voice" in blocks and not has_model_selection_specifier(
    blocks["packages/client/ui-voice"]
):
    missing.append("packages/client/ui-voice:@deepseek-ai/dsh-client-ui-model-selection")
if missing:
    raise SystemExit(
        "generated pnpm-lock.yaml is missing required importer/specifier(s): "
        + ", ".join(missing)
    )
PY
}

pinned_pnpm_version() {
  "${PYTHON_BIN}" - "${HARNESS}/package.json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    package = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"cannot parse pinned DSH package.json: {exc}")
value = package.get("packageManager")
if not isinstance(value, str):
    value = ""
match = re.fullmatch(r"pnpm@([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)", value or "")
if match is None:
    raise SystemExit(
        "pinned DSH package.json must declare an exact packageManager like pnpm@11.7.0"
    )
print(match.group(1))
PY
}

configure_pnpm() {
  PINNED_PNPM_VERSION="$(pinned_pnpm_version)" || \
    die "cannot determine the pinned DSH packageManager"
  PNPM_CMD=(npm exec --yes "--package=pnpm@${PINNED_PNPM_VERSION}" -- pnpm)
  PNPM_DISPLAY="npm exec --yes --package=pnpm@${PINNED_PNPM_VERSION} -- pnpm"
  if ((DRY_RUN)); then
    PNPM_VERSION="${PINNED_PNPM_VERSION}"
    return
  fi
  command -v npm >/dev/null 2>&1 || die "npm is required to provision pinned pnpm"
  PNPM_VERSION="$("${PNPM_CMD[@]}" --version)" || \
    die "cannot determine exact pnpm version via ${PNPM_DISPLAY}"
  [[ "${PNPM_VERSION}" == "${PINNED_PNPM_VERSION}" ]] || \
    die "${PNPM_DISPLAY} resolved pnpm ${PNPM_VERSION}, expected ${PINNED_PNPM_VERSION}"
}

git_top_level() {
  git -C "${HARNESS}" rev-parse --show-toplevel 2>/dev/null
}

assert_clean_checkout() {
  local dirty dirty_line relative head
  if ! dirty="$(git -C "${HARNESS}" status --porcelain=v1 --untracked-files=all 2>/dev/null)"; then
    die "cannot inspect existing harness checkout: ${HARNESS}"
  fi
  [[ -z "${dirty}" ]] && return 0

  # A successful bootstrap intentionally leaves the two overlay trees and the
  # exact registration files dirty relative to upstream.  Those paths
  # are the only managed changes we permit on a rerun; an arbitrary local
  # change still stops before fetch/checkout.
  head="$(git -C "${HARNESS}" rev-parse HEAD 2>/dev/null)" || \
    die "cannot inspect existing harness HEAD: ${HARNESS}"
  [[ "${head}" == "${LOCK_COMMIT}" ]] || \
    die "existing harness checkout is dirty at a non-pinned commit; refusing to modify user changes: ${HARNESS}"
  while IFS= read -r dirty_line; do
    relative="${dirty_line:3}"
    case "${relative}" in
      pnpm-lock.yaml)
        lock_state="$(managed_lock_state)"
        [[ "${lock_state}" == "valid" || "${lock_state}" == "reconcile" ]] || \
          die "pnpm-lock.yaml is dirty without a matching managed lock digest; refusing to modify user changes: ${HARNESS}"
        ;;
      packages/client/ui-voice/*|packages/host/codex/*|tsconfig.client.json|tsconfig.host.json|packages/api/remotes/src/client/index.ts|packages/api/remotes/package.json|packages/api/remotes/tsconfig.client.json|packages/bundle/web-app/cordis.patch.yml|packages/bundle/web-app/package.json|packages/host/apiproxy/src/api-proxy.ts|packages/client/runtime/src/client/sessions/manager.ts)
        [[ "$(managed_overlay_state "${relative}")" == "valid" ]] || \
          die "managed DSH overlay drifted; refusing to modify user changes: ${relative}"
        ;;
      docs/persistence-catalog.md|packages/core/session/src/known-event-types.ts)
        [[ "$(managed_catalog_state)" == "valid" ]] || \
          die "generated persistence catalog drifted without a matching managed digest; refusing to modify user changes: ${HARNESS}"
        ;;
      *)
        die "existing harness checkout is dirty; refusing to modify user changes: ${HARNESS}"
        ;;
    esac
  done <<< "${dirty}"
}

checkout_lock() {
  local head force="${1:-0}"
  head="$(git -C "${HARNESS}" rev-parse HEAD)"
  if [[ "${head}" == "${LOCK_COMMIT}" && "${force}" != "1" && -f "${HARNESS}/package.json" ]]; then
    if ! git -C "${HARNESS}" symbolic-ref --quiet HEAD >/dev/null 2>&1; then
      echo "[dsh-bootstrap] already pinned at ${head}"
      return
    fi
  fi
  git -C "${HARNESS}" checkout --detach "${LOCK_COMMIT}"
  head="$(git -C "${HARNESS}" rev-parse HEAD)"
  [[ "${head}" == "${LOCK_COMMIT}" ]] || \
    die "pinned checkout verification failed: expected ${LOCK_COMMIT}, got ${head}"
  echo "[dsh-bootstrap] pinned DSH at ${head}"
}

prepare_checkout() {
  local fresh_clone=0
  if [[ ! -e "${HARNESS}" ]]; then
    echo "[dsh-bootstrap] clone ${REPO_URL} -> ${HARNESS}"
    if ((DRY_RUN)); then
      return
    fi
    mkdir -p "$(dirname -- "${HARNESS}")"
    # --no-checkout keeps the first checkout tied to the lock below rather
    # than briefly exposing the repository's moving default branch.
    git clone --no-checkout "${REPO_URL}" "${HARNESS}"
    fresh_clone=1
  else
    [[ -d "${HARNESS}" ]] || die "existing harness target is not a directory: ${HARNESS}"
    local top_level origin_url expected_url
    top_level="$(git_top_level)" || die "existing target is not a git checkout: ${HARNESS}"
    [[ "$(cd -- "${top_level}" && pwd -P)" == "$(cd -- "${HARNESS}" && pwd -P)" ]] || \
      die "git checkout root does not match exact target: ${HARNESS}"
    origin_url="$(git -C "${HARNESS}" remote get-url origin 2>/dev/null)" || \
      die "existing checkout has no origin remote: ${HARNESS}"
    expected_url="$(normalize_url "${REPO_URL}")"
    [[ "$(normalize_url "${origin_url}")" == "${expected_url}" ]] || \
      die "origin remote drifted (expected ${REPO_URL}, got ${origin_url}); refusing to modify ${HARNESS}"
    assert_clean_checkout
  fi

  if ((DRY_RUN)); then
    echo "[dsh-bootstrap] fetch origin ${LOCK_COMMIT}"
    echo "[dsh-bootstrap] checkout --detach ${LOCK_COMMIT}"
    return
  fi

  git -C "${HARNESS}" fetch --prune origin "${LOCK_COMMIT}"
  if ((fresh_clone == 0)); then
    assert_clean_checkout
  fi
  checkout_lock "${fresh_clone}"
}

prepare_checkout

# Every real package-manager/build invocation goes through the exact version
# declared by the pinned checkout.  A dry-run of a not-yet-cloned target cannot
# read package.json yet, so it retains the explicit placeholder in its plan.
if [[ -f "${HARNESS}/package.json" ]]; then
  configure_pnpm
elif (( !DRY_RUN )); then
  die "pinned checkout has no package.json: ${HARNESS}"
fi

if ((DRY_RUN)); then
  echo "[dsh-bootstrap] would install overlay with ${SCRIPT_DIR}/install-dsh-plugin.sh --harness ${HARNESS} --host-source ${HOST_SOURCE_INPUT}"
else
  "${SCRIPT_DIR}/install-dsh-plugin.sh" \
    --harness "${HARNESS}" \
    --host-source "${HOST_SOURCE_INPUT}"
fi

if ((SKIP_INSTALL)); then
  echo "[dsh-bootstrap] skip pnpm install"
elif ((DRY_RUN)); then
  echo "[dsh-bootstrap] would use exact pnpm: ${PNPM_DISPLAY}"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} install --lockfile-only --no-frozen-lockfile)"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} install --frozen-lockfile)"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} run gen-persistence-catalog)"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} run verify-persistence-catalog)"
else
  lock_state="$(managed_lock_state)"
  case "${lock_state}" in
    valid)
      echo "[dsh-bootstrap] verified managed pnpm-lock.yaml (pnpm ${PNPM_VERSION})"
      ;;
    missing|reconcile)
      if [[ "${lock_state}" == "reconcile" ]]; then
        echo "[dsh-bootstrap] reconcile legacy plugin importer semantics: ${PNPM_DISPLAY} install --lockfile-only --no-frozen-lockfile"
      else
        echo "[dsh-bootstrap] reconcile plugin importer: ${PNPM_DISPLAY} install --lockfile-only --no-frozen-lockfile"
      fi
      (cd -- "${HARNESS}" && "${PNPM_CMD[@]}" install --lockfile-only --no-frozen-lockfile)
      verify_lock_overlay || die "pnpm lockfile reconciliation did not record the required importer/specifier set"
      "${SCRIPT_DIR}/install-dsh-plugin.sh" \
        --harness "${HARNESS}" \
        --host-source "${HOST_SOURCE_INPUT}" \
        --record-lock \
        --pnpm-version "${PNPM_VERSION}"
      [[ "$(managed_lock_state)" == "valid" ]] || \
        die "managed pnpm-lock.yaml digest verification failed after reconciliation"
      ;;
    mismatch)
      die "managed pnpm-lock.yaml drifted (or was generated by another pnpm version); refusing silent lock refresh: ${HARNESS}"
      ;;
    *)
      die "unknown managed lock state: ${lock_state}"
      ;;
  esac
  echo "[dsh-bootstrap] ${PNPM_DISPLAY} install --frozen-lockfile"
  (cd -- "${HARNESS}" && "${PNPM_CMD[@]}" install --frozen-lockfile)
fi

if ((SKIP_INSTALL)); then
  echo "[dsh-bootstrap] skip persistence catalog generation (pnpm install skipped)"
elif ((DRY_RUN)); then
  : # The dry-run messages are emitted with the install plan above.
else
  echo "[dsh-bootstrap] generate persistence catalog"
  (
    cd -- "${HARNESS}"
    "${PNPM_CMD[@]}" run gen-persistence-catalog
    "${PNPM_CMD[@]}" run verify-persistence-catalog
  )
  # Generated files are outside the plugin target. Record exact hashes only
  # after both generation and the freshness check succeed.
  "${SCRIPT_DIR}/install-dsh-plugin.sh" \
    --harness "${HARNESS}" \
    --host-source "${HOST_SOURCE_INPUT}" \
    --record-catalog
fi

if ((SKIP_BUILD)); then
  echo "[dsh-bootstrap] skip ui-voice build"
elif ((DRY_RUN)); then
  echo "[dsh-bootstrap] would run: ${PYTHON_BIN} ${SCRIPT_DIR}/clean-dsh-generated.py --harness ${HARNESS}"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} run build:lib:host)"
  echo "[dsh-bootstrap] would run: (cd ${HARNESS} && ${PNPM_DISPLAY} run build:lib:client)"
  echo "[dsh-bootstrap] would run: ${PYTHON_BIN} ${SCRIPT_DIR}/clean-dsh-generated.py --harness ${HARNESS} --post-build"
else
  echo "[dsh-bootstrap] build Host then all Client bundles"
  # Build output is disposable and may outlive a source file that was removed
  # from the overlay.  Clear only the two exact managed lib directories before
  # Typert/tsdown runs; the helper validates the target tree and symlink
  # boundaries before deleting anything.
  "${PYTHON_BIN}" "${SCRIPT_DIR}/clean-dsh-generated.py" --harness "${HARNESS}"
  (
    cd -- "${HARNESS}"
    # Keep the heap setting local to the build phase. Preserve an explicitly
    # supplied NODE_OPTIONS, but add the bounded 8 GiB ceiling when it does
    # not already specify one so a default bootstrap cannot OOM tsdown.
    BUILD_NODE_OPTIONS="${DSH_NODE_OPTIONS:-${NODE_OPTIONS:-}}"
    if [[ "${BUILD_NODE_OPTIONS}" != *--max-old-space-size=* ]]; then
      [[ -z "${BUILD_NODE_OPTIONS}" ]] || BUILD_NODE_OPTIONS+=" "
      BUILD_NODE_OPTIONS+="--max-old-space-size=8192"
    fi
    export NODE_OPTIONS="${BUILD_NODE_OPTIONS}"
    echo "[dsh-bootstrap] build NODE_OPTIONS=${NODE_OPTIONS}"
    # Host Typert artifacts must exist before api/remotes' generated Remote is
    # reflected.  The root Client phase typechecks and bundles api/remotes and
    # ui-voice together, so no stale lib/client.js can satisfy the seam.
    "${PNPM_CMD[@]}" run build:lib:host
    "${PNPM_CMD[@]}" run build:lib:client
  )
  # A macOS file-provider race can leave Finder-style `` 2``/`` 3``/... copies
  # after a successful build. Validate and remove only recognized duplicates
  # while preserving canonical output; differing/unexpected conflicts fail closed.
  "${PYTHON_BIN}" "${SCRIPT_DIR}/clean-dsh-generated.py" \
    --harness "${HARNESS}" \
    --post-build
fi

echo "[dsh-bootstrap] ready: ${HARNESS}"
