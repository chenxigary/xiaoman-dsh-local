#!/usr/bin/env bash
# Start the hardware-profiled local Qwen model through loopback-only llama.cpp.
set -euo pipefail

PROFILE="${XIAOMAN_PERFORMANCE_PROFILE:-auto}"
MODEL_PATH="${LOCAL_LLM_MODEL_PATH:-}"
MODEL_REPO_OVERRIDE="${LOCAL_LLM_HF_REPO:-}"
MODEL_ALIAS="${LOCAL_LLM_ALIAS:-xiaoman-local}"
ALLOW_DOWNLOAD="${LOCAL_LLM_ALLOW_DOWNLOAD:-0}"
LLAMA_BIN="${LLAMA_SERVER_BIN:-$(command -v llama-server || true)}"
HOST="${LOCAL_LLM_HOST:-127.0.0.1}"
PORT="${LOCAL_LLM_PORT:-8090}"
IDLE_SLEEP_SECONDS="${LOCAL_LLM_IDLE_SLEEP_SECONDS:-0}"

fail() {
  printf '[local-llm] ERROR: %s\n' "$*" >&2
  exit 1
}

unified_memory_bytes() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sysctl -n hw.memsize 2>/dev/null || printf '0\n'
  else
    printf '0\n'
  fi
}

resolve_profile() {
  local requested="$1"
  local memory_bytes
  if [[ "${requested}" != "auto" ]]; then
    printf '%s\n' "${requested}"
    return
  fi
  memory_bytes="$(unified_memory_bytes)"
  if [[ ! "${memory_bytes}" =~ ^[0-9]+$ ]]; then
    memory_bytes=0
  fi
  if (( memory_bytes >= 56 * 1024 * 1024 * 1024 )); then
    printf 'performance\n'
  elif (( memory_bytes >= 28 * 1024 * 1024 * 1024 )); then
    printf 'balanced\n'
  else
    printf 'efficient\n'
  fi
}

PROFILE="$(resolve_profile "${PROFILE}")"
case "${PROFILE}" in
  efficient)
    DEFAULT_MODEL_REPO="Qwen/Qwen3-4B-GGUF:Q4_K_M"
    DEFAULT_CONTEXT=4096
    MODEL_LABEL="Qwen3 4B Q4_K_M"
    ;;
  balanced)
    DEFAULT_MODEL_REPO="Qwen/Qwen3-8B-GGUF:Q4_K_M"
    DEFAULT_CONTEXT=8192
    MODEL_LABEL="Qwen3 8B Q4_K_M"
    ;;
  performance)
    DEFAULT_MODEL_REPO="Qwen/Qwen3-14B-GGUF:Q4_K_M"
    DEFAULT_CONTEXT=16384
    MODEL_LABEL="Qwen3 14B Q4_K_M"
    ;;
  *)
    fail "XIAOMAN_PERFORMANCE_PROFILE must be auto, efficient, balanced, or performance"
    ;;
esac

MODEL_REPO="${MODEL_REPO_OVERRIDE:-${DEFAULT_MODEL_REPO}}"
CTX="${LOCAL_LLM_CONTEXT_SIZE:-${DEFAULT_CONTEXT}}"

[[ -n "${LLAMA_BIN}" && -x "${LLAMA_BIN}" ]] ||
  fail "llama-server not found; run ./scripts/setup-macos-local.sh"
[[ "${HOST}" == "127.0.0.1" || "${HOST}" == "::1" || "${HOST}" == "localhost" ]] ||
  fail "LOCAL_LLM_HOST must remain loopback"
[[ "${PORT}" =~ ^[0-9]+$ ]] && (( 10#${PORT} >= 1 && 10#${PORT} <= 65535 )) ||
  fail "LOCAL_LLM_PORT must be a valid TCP port"
[[ "${CTX}" =~ ^[0-9]+$ ]] && (( CTX >= 1024 && CTX <= 131072 )) ||
  fail "LOCAL_LLM_CONTEXT_SIZE must be an integer from 1024 to 131072"
[[ "${IDLE_SLEEP_SECONDS}" =~ ^[0-9]+$ ]] ||
  fail "LOCAL_LLM_IDLE_SLEEP_SECONDS must be a non-negative integer"
[[ "${ALLOW_DOWNLOAD}" == "0" || "${ALLOW_DOWNLOAD}" == "1" ]] ||
  fail "LOCAL_LLM_ALLOW_DOWNLOAD must be 0 or 1"

MODEL_ARGS=()
if [[ -n "${MODEL_PATH}" ]]; then
  [[ -f "${MODEL_PATH}" ]] || fail "LOCAL_LLM_MODEL_PATH does not exist: ${MODEL_PATH}"
  MODEL_ARGS=(--model "${MODEL_PATH}")
  MODEL_LABEL="$(basename "${MODEL_PATH}")"
else
  MODEL_ARGS=(-hf "${MODEL_REPO}")
  if [[ "${ALLOW_DOWNLOAD}" == "0" ]]; then
    MODEL_ARGS+=(--offline)
  fi
fi

IDLE_SLEEP_ARGS=()
if (( IDLE_SLEEP_SECONDS > 0 )); then
  LLAMA_HELP="$("${LLAMA_BIN}" --help 2>&1 || true)"
  if [[ "${LLAMA_HELP}" == *"--sleep-idle-seconds"* ]]; then
    IDLE_SLEEP_ARGS=(--sleep-idle-seconds "${IDLE_SLEEP_SECONDS}")
    printf '[local-llm] automatic model sleep after %ss idle\n' "${IDLE_SLEEP_SECONDS}"
  else
    printf '%s\n' '[local-llm] WARN: llama-server lacks --sleep-idle-seconds; use stop-local-llm.sh.' >&2
  fi
else
  printf '%s\n' '[local-llm] automatic model sleep disabled'
fi

printf '[local-llm] profile: %s\n' "${PROFILE}"
printf '[local-llm] model: %s\n' "${MODEL_LABEL}"
printf '[local-llm] context: %s; endpoint: http://%s:%s\n' "${CTX}" "${HOST}" "${PORT}"
if [[ -z "${MODEL_PATH}" && "${ALLOW_DOWNLOAD}" == "1" ]]; then
  printf '%s\n' '[local-llm] online model acquisition enabled for this launch'
else
  printf '%s\n' '[local-llm] model loading is offline-only'
fi

LLAMA_ARGS=(
  "${MODEL_ARGS[@]}"
  --alias "${MODEL_ALIAS}"
  --host "${HOST}"
  --port "${PORT}"
  --ctx-size "${CTX}"
  --parallel 1
  --chat-template-kwargs '{"enable_thinking":false}'
  --reasoning off
  --reasoning-budget 0
)
if (( ${#IDLE_SLEEP_ARGS[@]} > 0 )); then
  LLAMA_ARGS+=("${IDLE_SLEEP_ARGS[@]}")
fi
LLAMA_ARGS+=(--jinja)

exec "${LLAMA_BIN}" "${LLAMA_ARGS[@]}"
