#!/usr/bin/env bash
# Start the DSH Web UI from an already-installed deepseek-harness checkout.
# This is intentionally limited to the upstream Voice Plugin boundary: it
# does not configure or connect Codex/OpenAI credentials.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
HARNESS="${DSH_HARNESS:-${REPO_ROOT}/.runtime/deepseek-harness}"

if [[ ! -f "${HARNESS}/package.json" ]]; then
  echo "[dsh-web] ERROR: DSH harness not found: ${HARNESS}" >&2
  echo "[dsh-web]        Bootstrap it with: ${REPO_ROOT}/scripts/bootstrap-dsh.sh" >&2
  echo "[dsh-web]        Or set DSH_HARNESS=/absolute/path/to/deepseek-harness." >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "[dsh-web] ERROR: Node.js LTS and npm are required." >&2
  exit 1
fi

PACKAGE_MANAGER="$(node -e "const p=require(process.argv[1]).packageManager||''; process.stdout.write(p)" "${HARNESS}/package.json")"
if [[ ! "${PACKAGE_MANAGER}" =~ ^pnpm@[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "[dsh-web] ERROR: pinned packageManager is missing or invalid: ${PACKAGE_MANAGER}" >&2
  exit 1
fi
PNPM_VERSION="${PACKAGE_MANAGER#pnpm@}"

echo "[dsh-web] starting from ${HARNESS}"
cd "${HARNESS}"
# The project overlay removes DeepSeek-backed routes and registers the local
# llama.cpp Qwen provider. Callers can supply another audited overlay explicitly.
MODEL_PATCH="${DSH_MODEL_PATCH:-${REPO_ROOT}/config/dsh-local-model.patch.yml}"
if [[ ! -f "${MODEL_PATCH}" ]]; then
  echo "[dsh-web] ERROR: model overlay not found: ${MODEL_PATCH}" >&2
  exit 1
fi
"${SCRIPT_DIR}/install-xiaoman-preset.sh"
exec npm exec --yes --package="pnpm@${PNPM_VERSION}" -- pnpm dsh web --patch "${MODEL_PATCH}"
