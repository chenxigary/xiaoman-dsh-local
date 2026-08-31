#!/usr/bin/env bash
# Shared Python runtime selection for the macOS launchers.
#
# Source this file from a Bash script and call select_supported_python.  The
# function prints exactly one executable path and only accepts Python >= 3.10,
# which is required by the bridge's type syntax and ML dependencies.

python_supported() {
  local candidate="$1"
  [[ -x "${candidate}" ]] || return 1
  "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

select_supported_python() {
  local candidate

  # An explicit override is authoritative, but never bypasses the version
  # guard.  This prevents a hidden Python 3.9 process from failing later with
  # a less actionable import/syntax error.
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if python_supported "${PYTHON_BIN}"; then
      printf '%s\n' "${PYTHON_BIN}"
      return 0
    fi
    echo "Python override is missing or < 3.10: ${PYTHON_BIN}" >&2
    return 1
  fi

  local candidates=(
    "${REPO_ROOT}/.venv/bin/python"
    "${REPO_ROOT}/venv-speech/bin/python"
  )
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      candidates+=("$(command -v "${candidate}")")
    fi
  done

  for candidate in "${candidates[@]}"; do
    if python_supported "${candidate}"; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  echo "No supported Python found (requires Python >= 3.10)." >&2
  echo "Install Python 3.10+ or set PYTHON_BIN=/absolute/path/to/python." >&2
  return 1
}
