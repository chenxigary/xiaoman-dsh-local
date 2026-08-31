#!/usr/bin/env bash
# Start the authoritative Xiaoman v3 Voice Runtime on loopback.
#
# DSH remains the Agent/Codex/UI owner and proxies STT, TTS, and Avatar audio
# through the versioned v3 boundary. Override XIAOMAN_V3_ROOT when the v3
# checkout lives elsewhere, and XIAOMAN_VOICE_RUNTIME_PYTHON for its MLX env.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
XIAOMAN_V3_ROOT="${XIAOMAN_V3_ROOT:-${REPO_ROOT}/.runtime/macos-local-voice-agents/xiaoman-v3}"
PYTHON="${XIAOMAN_VOICE_RUNTIME_PYTHON:-${XIAOMAN_V3_ROOT}/../.venv-v3-tts312/bin/python}"
HOST="${XIAOMAN_VOICE_RUNTIME_HOST:-127.0.0.1}"
PORT="${XIAOMAN_VOICE_RUNTIME_PORT:-7860}"

if [[ ! -f "${XIAOMAN_V3_ROOT}/gateway/app.py" ]]; then
  echo "[voice-runtime] ERROR: Xiaoman v3 checkout not found at ${XIAOMAN_V3_ROOT}" >&2
  echo "[voice-runtime] set XIAOMAN_V3_ROOT to the authoritative xiaoman-v3 directory" >&2
  exit 1
fi
if [[ ! -x "${PYTHON}" ]]; then
  echo "[voice-runtime] ERROR: v3 Python is not executable: ${PYTHON}" >&2
  echo "[voice-runtime] set XIAOMAN_VOICE_RUNTIME_PYTHON to the v3 MLX Python" >&2
  exit 1
fi
if [[ "${HOST}" != "127.0.0.1" && "${HOST}" != "::1" && "${HOST}" != "localhost" ]]; then
  echo "[voice-runtime] ERROR: Voice Runtime must bind to loopback" >&2
  exit 1
fi
if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || (( 10#${PORT} < 1 || 10#${PORT} > 65535 )); then
  echo "[voice-runtime] ERROR: invalid port: ${PORT}" >&2
  exit 1
fi

export V3_HOST="${HOST}"
export V3_PORT="${PORT}"
export V3_LLM_URL="${V3_LLM_URL:-${LOCAL_LLM_URL:-http://127.0.0.1:8090}}"
export V3_AVATAR_URL="${V3_AVATAR_URL:-${XIAOMAN_AVATAR_URL:-http://127.0.0.1:8010}}"
export V3_AVATAR_ENABLED="${V3_AVATAR_ENABLED:-1}"
export V3_TTS_PRELOAD="${V3_TTS_PRELOAD:-1}"
export V3_ASR_PRELOAD="${V3_ASR_PRELOAD:-1}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

echo "[voice-runtime] starting Xiaoman v3 at http://${HOST}:${PORT}"
echo "[voice-runtime] protocol endpoint: /api/voice-runtime/v1/health"
exec "${PYTHON}" -m uvicorn gateway.app:app \
  --app-dir "${XIAOMAN_V3_ROOT}" \
  --host "${HOST}" \
  --port "${PORT}"
