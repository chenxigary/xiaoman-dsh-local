#!/usr/bin/env bash
# Safely stop the llama.cpp process started by scripts/start-all.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
RUN_DIR="${RUN_DIR:-${REPO_ROOT}/.run}"
PID_FILE="${RUN_DIR}/local-llm.pid"
PORT="${LOCAL_LLM_PORT:-8090}"
STOP_TIMEOUT_SECONDS="${LOCAL_LLM_STOP_TIMEOUT_SECONDS:-15}"

if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[stop-local-llm] ERROR: LOCAL_LLM_PORT must be an integer from 1 to 65535." >&2
  exit 1
fi
if [[ ! "${STOP_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "[stop-local-llm] ERROR: LOCAL_LLM_STOP_TIMEOUT_SECONDS must be a non-negative integer." >&2
  exit 1
fi

is_expected_process() {
  local pid="$1"
  local command_line process_uid padded_command

  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "${pid}" 2>/dev/null || return 1
  process_uid="$(ps -p "${pid}" -o uid= 2>/dev/null | tr -d '[:space:]')"
  [[ -n "${process_uid}" && "${process_uid}" == "$(id -u)" ]] || return 1
  command_line="$(ps -ww -p "${pid}" -o command= 2>/dev/null)" || return 1
  [[ "${command_line}" =~ (^|[[:space:]/])llama-server([[:space:]]|$) ]] || return 1

  padded_command=" ${command_line} "
  [[ "${padded_command}" == *" --port ${PORT} "* ||
     "${padded_command}" == *" --port=${PORT} "* ]]
}

pid_from_file=""
if [[ -f "${PID_FILE}" ]]; then
  pid_from_file="$(<"${PID_FILE}")"
  pid_from_file="${pid_from_file//[[:space:]]/}"
fi

target_pid=""
if is_expected_process "${pid_from_file}"; then
  target_pid="${pid_from_file}"
elif [[ -n "${pid_from_file}" ]]; then
  echo "[stop-local-llm] ignoring stale or mismatched PID file (${pid_from_file})" >&2
fi

# start-all may encounter a server that was already running, in which case its
# PID file can be absent or stale. On macOS, resolve the loopback listener and
# apply the same owner/command/port checks before accepting it.
if [[ -z "${target_pid}" ]] && command -v lsof >/dev/null 2>&1; then
  while IFS= read -r listener_pid; do
    if is_expected_process "${listener_pid}"; then
      if [[ -n "${target_pid}" && "${target_pid}" != "${listener_pid}" ]]; then
        echo "[stop-local-llm] ERROR: multiple matching llama-server listeners found." >&2
        exit 1
      fi
      target_pid="${listener_pid}"
    fi
  done < <(lsof -nP -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)
fi

if [[ -z "${target_pid}" ]]; then
  rm -f -- "${PID_FILE}"
  echo "[stop-local-llm] no managed llama-server is running on port ${PORT}"
  exit 0
fi

# Recheck immediately before signaling to reduce the chance of acting on a
# recycled PID.
if ! is_expected_process "${target_pid}"; then
  echo "[stop-local-llm] ERROR: process ${target_pid} changed before it could be stopped." >&2
  exit 1
fi

echo "[stop-local-llm] stopping llama-server PID ${target_pid}"
kill -TERM "${target_pid}"

for (( waited = 0; waited < STOP_TIMEOUT_SECONDS; waited += 1 )); do
  if ! is_expected_process "${target_pid}"; then
    rm -f -- "${PID_FILE}"
    echo "[stop-local-llm] stopped; model memory released"
    exit 0
  fi
  sleep 1
done

if is_expected_process "${target_pid}"; then
  echo "[stop-local-llm] graceful stop timed out; force-stopping PID ${target_pid}" >&2
  kill -KILL "${target_pid}"
fi
rm -f -- "${PID_FILE}"
echo "[stop-local-llm] stopped; model memory released"
