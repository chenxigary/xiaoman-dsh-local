#!/usr/bin/env bash
# Synchronize dsh-plugin and register it in one DSH checkout.
#
# The Python implementation performs a read-only preflight first.  It never
# removes files and refuses to overwrite a destination that differs from the
# source tree, so this wrapper is safe to rerun after a build.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[dsh-plugin] ERROR: Python 3 is required (set PYTHON_BIN to override)." >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/install-dsh-plugin.py" "$@"
