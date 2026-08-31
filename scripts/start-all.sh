#!/usr/bin/env bash
# Start the macOS local-only stack: local models + bridge + DSH Web UI.
# NapCat/OneBot is Windows-specific in upstream and is intentionally not
# started here.  Each service gets its own log/pid file so closing this shell
# does not terminate the service that was launched from it.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
RUN_DIR="${RUN_DIR:-${REPO_ROOT}/.run}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/logs}"
mkdir -p "${RUN_DIR}" "${LOG_DIR}"
export XIAOMAN_LOCAL_ONLY=1

BRIDGE_URL="${VOICE_BRIDGE_URL:-http://${VOICE_BRIDGE_HOST:-127.0.0.1}:${VOICE_BRIDGE_PORT:-8765}}"
DSH_URL="${DSH_URL:-http://127.0.0.1:3080}"
LOCAL_LLM_URL="${LOCAL_LLM_URL:-http://${LOCAL_LLM_HOST:-127.0.0.1}:${LOCAL_LLM_PORT:-8090}}"
AVATAR_URL="${XIAOMAN_AVATAR_URL:-http://127.0.0.1:${XIAOMAN_AVATAR_PORT:-8010}}"
VOICE_RUNTIME_MODE="${DSH_VOICE_RUNTIME_MODE:-v3}"
VOICE_RUNTIME_HOST="${XIAOMAN_VOICE_RUNTIME_HOST:-127.0.0.1}"
VOICE_RUNTIME_PORT="${XIAOMAN_VOICE_RUNTIME_PORT:-7860}"
VOICE_RUNTIME_AUTOSTART_URL="http://${VOICE_RUNTIME_HOST}:${VOICE_RUNTIME_PORT}"
VOICE_RUNTIME_URL="${DSH_VOICE_RUNTIME_URL:-${VOICE_RUNTIME_AUTOSTART_URL}}"

is_up() {
  local url="$1"
  command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "${url}" >/dev/null 2>&1
}

if [[ "${START_LOCAL_LLM:-1}" != "0" ]]; then
  if ! is_up "${LOCAL_LLM_URL}/health"; then
    echo "[start-all] starting local Qwen (${XIAOMAN_PERFORMANCE_PROFILE:-auto} profile)"
    nohup "${SCRIPT_DIR}/start-local-llm.sh" \
      >"${LOG_DIR}/local-llm.log" 2>&1 < /dev/null &
    echo "$!" >"${RUN_DIR}/local-llm.pid"
  else
    echo "[start-all] local LLM already healthy at ${LOCAL_LLM_URL}"
  fi

  echo "[start-all] waiting for local LLM health"
  for _ in $(seq 1 "${LOCAL_LLM_HEALTH_TIMEOUT_SEC:-90}"); do
    if is_up "${LOCAL_LLM_URL}/health"; then
      break
    fi
    sleep 1
  done
  if ! is_up "${LOCAL_LLM_URL}/health"; then
    echo "[start-all] WARN: local LLM did not answer; see ${LOG_DIR}/local-llm.log" >&2
  else
    echo "[start-all] local LLM is healthy"
  fi
fi

if [[ "${START_AVATAR:-1}" != "0" ]]; then
  if ! is_up "${AVATAR_URL}/api/admin/config"; then
    echo "[start-all] starting Xiaoman LiveTalking/Wav2Lip"
    nohup "${SCRIPT_DIR}/start-avatar.sh" \
      >"${LOG_DIR}/avatar.log" 2>&1 < /dev/null &
    echo "$!" >"${RUN_DIR}/avatar.pid"
  else
    echo "[start-all] Avatar already healthy at ${AVATAR_URL}"
  fi

  echo "[start-all] waiting for Avatar model warm-up"
  for _ in $(seq 1 "${AVATAR_HEALTH_TIMEOUT_SEC:-180}"); do
    if is_up "${AVATAR_URL}/api/admin/config"; then
      break
    fi
    sleep 1
  done
  if ! is_up "${AVATAR_URL}/api/admin/config"; then
    echo "[start-all] WARN: Avatar did not answer; see ${LOG_DIR}/avatar.log" >&2
  else
    echo "[start-all] Avatar is healthy"
  fi
fi

if [[ "${VOICE_RUNTIME_MODE}" == "v3" && "${START_VOICE_RUNTIME:-1}" != "0" ]]; then
  if [[ "${VOICE_RUNTIME_URL}" != "${VOICE_RUNTIME_AUTOSTART_URL}" ]]; then
    echo "[start-all] ERROR: DSH_VOICE_RUNTIME_URL differs from the autostart origin" >&2
    echo "[start-all] set matching XIAOMAN_VOICE_RUNTIME_HOST/PORT, or START_VOICE_RUNTIME=0" >&2
    exit 1
  fi
  if ! is_up "${VOICE_RUNTIME_URL}/api/voice-runtime/v1/health"; then
    echo "[start-all] starting authoritative Xiaoman v3 Voice Runtime"
    LOCAL_LLM_URL="${LOCAL_LLM_URL}" \
      XIAOMAN_AVATAR_URL="${AVATAR_URL}" \
      XIAOMAN_VOICE_RUNTIME_HOST="${VOICE_RUNTIME_HOST}" \
      XIAOMAN_VOICE_RUNTIME_PORT="${VOICE_RUNTIME_PORT}" \
      nohup "${SCRIPT_DIR}/start-voice-runtime.sh" \
      >"${LOG_DIR}/voice-runtime.log" 2>&1 < /dev/null &
    echo "$!" >"${RUN_DIR}/voice-runtime.pid"
  else
    echo "[start-all] Voice Runtime already healthy at ${VOICE_RUNTIME_URL}"
  fi

  echo "[start-all] waiting for v3 Voice Runtime model warm-up"
  for _ in $(seq 1 "${VOICE_RUNTIME_HEALTH_TIMEOUT_SEC:-240}"); do
    if is_up "${VOICE_RUNTIME_URL}/api/voice-runtime/v1/health"; then
      break
    fi
    sleep 1
  done
  if ! is_up "${VOICE_RUNTIME_URL}/api/voice-runtime/v1/health"; then
    echo "[start-all] WARN: v3 Voice Runtime did not become ready; see ${LOG_DIR}/voice-runtime.log" >&2
  else
    echo "[start-all] v3 Voice Runtime is ready"
  fi
elif [[ "${VOICE_RUNTIME_MODE}" == "local" ]]; then
  echo "[start-all] explicit local voice fallback enabled; v3 Voice Runtime skipped"
else
  echo "[start-all] v3 Voice Runtime autostart disabled"
fi

if ! is_up "${BRIDGE_URL}/api/health"; then
  echo "[start-all] starting voice bridge"
  DSH_VOICE_RUNTIME_MODE="${VOICE_RUNTIME_MODE}" \
    DSH_VOICE_RUNTIME_URL="${VOICE_RUNTIME_URL}" \
    nohup "${SCRIPT_DIR}/start-bridge.sh" \
    >"${LOG_DIR}/voice-bridge.log" 2>&1 < /dev/null &
  echo "$!" >"${RUN_DIR}/voice-bridge.pid"
else
  echo "[start-all] voice bridge already healthy at ${BRIDGE_URL}"
fi

echo "[start-all] waiting for bridge health"
for _ in $(seq 1 "${BRIDGE_HEALTH_TIMEOUT_SEC:-30}"); do
  if is_up "${BRIDGE_URL}/api/health"; then
    break
  fi
  sleep 1
done
if ! is_up "${BRIDGE_URL}/api/health"; then
  echo "[start-all] WARN: bridge did not answer; see ${LOG_DIR}/voice-bridge.log" >&2
else
  echo "[start-all] bridge is healthy"
fi

# Ensure TTS is warm before the browser's first reply. In the default mode the
# model is owned by v3; the bridge only proxies this request. The generic
# character deliberately avoids driving the Avatar.
if [[ "${WARM_TTS:-1}" != "0" ]] && is_up "${BRIDGE_URL}/api/health"; then
  if ! curl -fsS --max-time 2 "${BRIDGE_URL}/api/health" | grep -q '"tts":true'; then
    echo "[start-all] warming TTS through the configured Voice Runtime"
    if ! curl -fsS --max-time "${TTS_WARMUP_TIMEOUT_SEC:-120}" \
      -X POST "${BRIDGE_URL}/api/tts" \
      -H 'Content-Type: application/json' \
      --data '{"text":"你好。","character":"default"}' \
      >/dev/null; then
      echo "[start-all] WARN: TTS warm-up failed; the first spoken reply may be slower" >&2
    else
      echo "[start-all] TTS is warm"
    fi
  fi
fi

HARNESS="${DSH_HARNESS:-${REPO_ROOT}/.runtime/deepseek-harness}"
if [[ "${START_DSH:-1}" != "0" && ! -f "${HARNESS}/package.json" ]]; then
  echo "[start-all] DSH Web skipped: harness not found at ${HARNESS}"
  echo "[start-all] bootstrap it with: ${REPO_ROOT}/scripts/bootstrap-dsh.sh"
  echo "[start-all] set DSH_HARNESS=/absolute/path/to/deepseek-harness to enable it"
elif [[ "${START_DSH:-1}" != "0" ]]; then
  if is_up "${DSH_URL}"; then
    echo "[start-all] DSH Web already reachable at ${DSH_URL}"
  else
    echo "[start-all] starting DSH Web (DSH_HARNESS=${DSH_HARNESS:-unset})"
    nohup "${SCRIPT_DIR}/start-dsh.sh" \
      >"${LOG_DIR}/dsh-web.log" 2>&1 < /dev/null &
    echo "$!" >"${RUN_DIR}/dsh-web.pid"
    echo "[start-all] waiting for DSH Web"
    for _ in $(seq 1 "${DSH_HEALTH_TIMEOUT_SEC:-60}"); do
      if is_up "${DSH_URL}"; then
        break
      fi
      sleep 1
    done
    if ! is_up "${DSH_URL}"; then
      echo "[start-all] WARN: DSH Web did not answer; see ${LOG_DIR}/dsh-web.log" >&2
    fi
  fi
fi

if [[ "${OPEN_BROWSER:-1}" != "0" ]] && command -v open >/dev/null 2>&1 && is_up "${DSH_URL}"; then
  open "${DSH_URL}" >/dev/null 2>&1 || true
fi

echo "[start-all] done; services are detached"
echo "[start-all] local-only boundary: DeepSeek and Codex model routes are disabled"
echo "[start-all] logs: ${LOG_DIR}"
echo "[start-all] stop the local LLM with: ${SCRIPT_DIR}/stop-local-llm.sh"
