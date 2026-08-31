#!/usr/bin/env bash
# Dependency-light checks for the macOS baseline. Model inference is opt-in:
# set CHECK_RUNNING_BRIDGE=1 to probe a running bridge, and pass
# SMOKE_STT_FILE/SMOKE_TTS_TEXT for real STT/TTS requests.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
source "${SCRIPT_DIR}/python-runtime.sh"
if ! PYTHON="$(select_supported_python)"; then
  echo "smoke-check requires Python 3.10+ (set PYTHON_BIN to override)." >&2
  exit 1
fi

bash -n "${SCRIPT_DIR}/setup-macos-local.sh" "${SCRIPT_DIR}/run-local.sh" "${SCRIPT_DIR}/status-local.sh" "${SCRIPT_DIR}/stop-local.sh" \
  "${SCRIPT_DIR}/start-local-llm.sh" "${SCRIPT_DIR}/stop-local-llm.sh" "${SCRIPT_DIR}/start-avatar.sh" "${SCRIPT_DIR}/start-voice-runtime.sh" "${SCRIPT_DIR}/start-bridge.sh" "${SCRIPT_DIR}/start-dsh.sh" "${SCRIPT_DIR}/start-all.sh" "${SCRIPT_DIR}/test-avatar-sync.sh" \
  "${SCRIPT_DIR}/bootstrap-dsh.sh" "${SCRIPT_DIR}/install-dsh-plugin.sh"
"${PYTHON}" -m unittest discover -s "${REPO_ROOT}/tests" -p 'test_*.py'
"${PYTHON}" -m py_compile "${REPO_ROOT}/bridge/voice_bridge.py" "${REPO_ROOT}/bridge/voice_runtime_client.py" "${REPO_ROOT}/bridge/avatar_relay.py" "${REPO_ROOT}/bridge/av_quality.py" "${REPO_ROOT}/bridge/livetalking_continuity.py" "${REPO_ROOT}/bridge/livetalking_video.py" "${REPO_ROOT}/bridge/livetalking_warmup.py" "${REPO_ROOT}/bridge/latency.py" "${REPO_ROOT}/scripts/run-avatar.py" "${REPO_ROOT}/scripts/test-avatar-sync.py"

if [[ "${CHECK_RUNNING_BRIDGE:-0}" == "1" ]]; then
  curl -fsS "${VOICE_BRIDGE_URL:-http://127.0.0.1:8765}/api/health"
  echo
  if [[ -n "${SMOKE_STT_FILE:-}" ]]; then
    "${PYTHON}" "${REPO_ROOT}/bridge/smoke_stt.py" --file "${SMOKE_STT_FILE}"
  fi
  if [[ -n "${SMOKE_TTS_TEXT:-}" ]]; then
    "${PYTHON}" "${REPO_ROOT}/bridge/smoke_tts.py" --text "${SMOKE_TTS_TEXT}"
  fi
fi
echo "smoke checks passed"
