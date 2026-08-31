#!/usr/bin/env bash
# Start the local voice bridge on macOS/Linux.
#
# The script does not activate a virtualenv, so it is safe to launch from
# Finder, another shell, or start-all.sh.  Override PYTHON_BIN when the speech
# environment lives elsewhere; VOICE_BRIDGE_CONFIG/HOST/PORT are also
# supported by the bridge.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
BRIDGE_DIR="${REPO_ROOT}/bridge"

source "${SCRIPT_DIR}/python-runtime.sh"
if ! PYTHON="$(select_supported_python)"; then
  echo "[voice-bridge] ERROR: Python 3.10+ is required. Set PYTHON_BIN or create .venv." >&2
  exit 1
fi

if [[ ! -f "${BRIDGE_DIR}/bridge-config.json" ]]; then
  echo "[voice-bridge] INFO: bridge-config.json is absent; using the checked-in defaults." >&2
fi

HOST="${VOICE_BRIDGE_HOST:-127.0.0.1}"
PORT="${VOICE_BRIDGE_PORT:-8765}"
# The selected models are already cached for this deployment. Avoid network
# metadata checks during daily startup unless the operator explicitly opts in.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export DSH_VOICE_RUNTIME_MODE="${DSH_VOICE_RUNTIME_MODE:-v3}"
export DSH_VOICE_RUNTIME_URL="${DSH_VOICE_RUNTIME_URL:-http://127.0.0.1:7860}"
echo "[voice-bridge] starting http://${HOST}:${PORT}"
if [[ "${DSH_VOICE_RUNTIME_MODE}" == "v3" ]]; then
  echo "[voice-bridge] proxying voice to ${DSH_VOICE_RUNTIME_URL} (no MLX models loaded here)"
else
  echo "[voice-bridge] explicit local fallback mode; models load lazily on first use"
fi

exec "${PYTHON}" -m uvicorn voice_bridge:app \
  --app-dir "${BRIDGE_DIR}" \
  --host "${HOST}" \
  --port "${PORT}"
