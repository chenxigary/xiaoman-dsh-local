#!/usr/bin/env bash
# Run the real WebRTC A/V continuity test in the v3 Avatar environment.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
XIAOMAN_ROOT="${XIAOMAN_V3_ROOT:-${REPO_ROOT}/.runtime/macos-local-voice-agents/xiaoman-v3}"
AVATAR_PYTHON="${XIAOMAN_AVATAR_PYTHON:-${XIAOMAN_ROOT}/../.venv-v3-avatar/bin/python}"
AVATAR_URL="${XIAOMAN_AVATAR_URL:-http://127.0.0.1:8010}"

if [[ ! -x "${AVATAR_PYTHON}" ]]; then
  echo "[avatar-sync] INCOMPLETE: Avatar Python is missing: ${AVATAR_PYTHON}" >&2
  exit 2
fi
if ! curl -fsS --max-time 3 "${AVATAR_URL}/api/admin/config" >/dev/null; then
  echo "[avatar-sync] INCOMPLETE: LiveTalking is not reachable at ${AVATAR_URL}" >&2
  echo "[avatar-sync] start it with ./scripts/start-all.sh, then retry" >&2
  exit 2
fi

exec "${AVATAR_PYTHON}" "${REPO_ROOT}/scripts/test-avatar-sync.py" \
  --avatar-url "${AVATAR_URL}" "$@"
