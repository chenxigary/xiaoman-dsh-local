#!/usr/bin/env bash
# Install the repository-owned Xiaoman preset into DSH's supported user root.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
SOURCE="${REPO_ROOT}/config/agent-presets/xiaoman"
DSH_DATA_HOME="${DSH_HOME:-${HOME}/.dsh}"
TARGET_ROOT="${DSH_DATA_HOME}/.agent-presets"
TARGET="${TARGET_ROOT}/xiaoman"
MANIFEST=".xiaoman-dsh-managed.sha256"

if [[ ! -f "${SOURCE}/agent.cordis.yml" || ! -f "${SOURCE}/preset.yml" || ! -f "${SOURCE}/${MANIFEST}" ]]; then
  echo "[xiaoman-preset] ERROR: managed preset source is incomplete: ${SOURCE}" >&2
  exit 1
fi
if [[ -L "${DSH_DATA_HOME}" || -L "${TARGET_ROOT}" || -L "${TARGET}" ]]; then
  echo "[xiaoman-preset] ERROR: refusing to install through a symlinked DSH preset path." >&2
  exit 1
fi

mkdir -p "${TARGET_ROOT}"
if [[ -d "${TARGET}" ]]; then
  if [[ ! -f "${TARGET}/${MANIFEST}" ]]; then
    echo "[xiaoman-preset] ERROR: preset id 'xiaoman' already exists and is not project-managed: ${TARGET}" >&2
    exit 1
  fi
  if ! (cd "${TARGET}" && shasum -a 256 -c "${MANIFEST}" >/dev/null); then
    echo "[xiaoman-preset] ERROR: managed Xiaoman preset was edited; refusing to overwrite: ${TARGET}" >&2
    exit 1
  fi
elif [[ -e "${TARGET}" ]]; then
  echo "[xiaoman-preset] ERROR: preset target is not a directory: ${TARGET}" >&2
  exit 1
else
  mkdir "${TARGET}"
fi

install -m 0644 "${SOURCE}/agent.cordis.yml" "${TARGET}/agent.cordis.yml"
install -m 0644 "${SOURCE}/preset.yml" "${TARGET}/preset.yml"
install -m 0644 "${SOURCE}/${MANIFEST}" "${TARGET}/${MANIFEST}"
echo "[xiaoman-preset] synchronized ${TARGET}"
