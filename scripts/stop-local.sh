#!/usr/bin/env bash
# Stop only the detached Xiaoman processes recorded by this checkout.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
RUN_DIR="${RUN_DIR:-${REPO_ROOT}/.run}"
CURRENT_UID="$(id -u)"

expected_fragment() {
  case "$1" in
    dsh-web) printf 'npm|pnpm|dsh' ;;
    voice-bridge) printf 'voice_bridge:app' ;;
    voice-runtime) printf 'gateway.app:app' ;;
    avatar) printf 'run-avatar.py' ;;
    *) return 1 ;;
  esac
}

stop_recorded() {
  local service="$1"
  local pid_file="${RUN_DIR}/${service}.pid"
  local pid command owner expected
  if [[ ! -f "${pid_file}" ]]; then
    printf '[stop-local] %s: no PID record\n' "${service}"
    return
  fi
  pid="$(tr -d '[:space:]' < "${pid_file}")"
  if [[ ! "${pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${pid}" 2>/dev/null; then
    printf '[stop-local] %s: stale PID record (%s)\n' "${service}" "${pid:-invalid}"
    return
  fi
  owner="$(ps -o uid= -p "${pid}" | tr -d '[:space:]')"
  command="$(ps -o command= -p "${pid}")"
  expected="$(expected_fragment "${service}")"
  if [[ "${owner}" != "${CURRENT_UID}" ]] || ! printf '%s\n' "${command}" | grep -Eq "${expected}"; then
    printf '[stop-local] REFUSED: %s PID %s is not the expected current-user process\n' "${service}" "${pid}" >&2
    return 1
  fi
  printf '[stop-local] stopping %s (PID %s)\n' "${service}" "${pid}"
  kill -TERM "${pid}"
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "${pid}" 2>/dev/null || return 0
    sleep 1
  done
  command="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
  if printf '%s\n' "${command}" | grep -Eq "${expected}"; then
    printf '[stop-local] %s did not stop after TERM; sending KILL\n' "${service}" >&2
    kill -KILL "${pid}"
  else
    printf '[stop-local] REFUSED KILL: PID %s identity changed\n' "${pid}" >&2
    return 1
  fi
}

failures=0
for service in dsh-web voice-bridge voice-runtime avatar; do
  stop_recorded "${service}" || failures=$((failures + 1))
done
"${SCRIPT_DIR}/stop-local-llm.sh" || failures=$((failures + 1))

if (( failures > 0 )); then
  printf '[stop-local] completed with %s refusal/failure(s)\n' "${failures}" >&2
  exit 1
fi
printf '%s\n' '[stop-local] expected local stack processes are stopped'
