#!/usr/bin/env bash
# Reproducible fresh-Mac setup for the private Xiaoman DSH local-only build.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
LOCK_FILE="${REPO_ROOT}/xiaoman.local.lock.json"
CHECK_ONLY=0
WITH_AVATAR=1
ASSETS_FROM=""

usage() {
  cat <<'EOF'
用法：
  ./scripts/setup-macos-local.sh [选项]

选项：
  --assets-from PATH  使用已解压的本地私有素材目录，跳过 Release 下载
  --no-avatar         不安装 Avatar 环境（LLM、ASR、TTS、DSH 仍安装）
  --check             只读检查；不安装、不下载、不复制
  -h, --help          显示帮助

默认行为会：安装公开依赖、固定 Xiaoman v3 源码、下载私有素材 Release、
创建 Python 环境，并准备固定版本的 DeepSeek Harness。
EOF
}

say() {
  printf '[setup-local] %s\n' "$*"
}

warn() {
  printf '[setup-local] WARNING: %s\n' "$*" >&2
}

fail() {
  printf '[setup-local] ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --assets-from)
      (($# >= 2)) || fail "--assets-from 需要目录"
      ASSETS_FROM="$2"
      shift
      ;;
    --no-avatar) WITH_AVATAR=0 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1（使用 --help 查看帮助）" ;;
  esac
  shift
done

[[ -f "${LOCK_FILE}" ]] || fail "缺少锁文件：${LOCK_FILE}"
command -v python3 >/dev/null 2>&1 || fail "需要系统 Python 3 读取锁文件"

lock_value() {
  python3 - "${LOCK_FILE}" "$1" <<'PY'
import json
import sys

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
for part in sys.argv[2].split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

DISTRIBUTION_REPO="$(lock_value distribution.repository)"
V3_REPOSITORY="$(lock_value xiaomanV3.repository)"
V3_COMMIT="$(lock_value xiaomanV3.commit)"
V3_CHECKOUT_REL="$(lock_value xiaomanV3.checkout)"
ASSET_REPOSITORY="$(lock_value privateAssets.repository)"
ASSET_TAG="$(lock_value privateAssets.tag)"
ASSET_NAME="$(lock_value privateAssets.name)"
ASSET_SHA256="$(lock_value privateAssets.sha256)"
SILERO_SHA256="$(lock_value privateAssets.sileroVadSha256)"

V3_CHECKOUT="${REPO_ROOT}/${V3_CHECKOUT_REL}"
V3_ROOT="${XIAOMAN_V3_ROOT:-${V3_CHECKOUT}/xiaoman-v3}"
V3_CHECKOUT="$(cd -- "$(dirname -- "${V3_ROOT}")" 2>/dev/null && pwd -P || printf '%s' "${V3_CHECKOUT}")"

require_target_mac() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "只支持 macOS"
  [[ "$(uname -m)" == "arm64" ]] || fail "需要 Apple Silicon（arm64）"
}

check_command() {
  local command="$1"
  if command -v "${command}" >/dev/null 2>&1; then
    say "命令 OK：${command} -> $(command -v "${command}")"
    return 0
  fi
  warn "缺少命令：${command}"
  return 1
}

verify_private_repo() {
  local private
  private="$(gh repo view "${DISTRIBUTION_REPO}" --json isPrivate --jq .isPrivate 2>/dev/null || true)"
  [[ "${private}" == "true" ]] || {
    warn "无法确认 ${DISTRIBUTION_REPO} 是私有仓库"
    return 1
  }
  say "私有仓库 OK：${DISTRIBUTION_REPO}"
}

verify_local_assets() {
  local failures=0
  if (
    cd -- "${REPO_ROOT}/assets/xiaoman"
    shasum -a 256 -c assets.sha256
  ); then
    say "仓库内小满素材 SHA-256 校验通过"
  else
    warn "仓库内小满素材校验失败"
    failures=$((failures + 1))
  fi
  local silero="${REPO_ROOT}/models/silero-vad/silero_vad.jit"
  if [[ -f "${silero}" ]] && [[ "$(shasum -a 256 "${silero}" | awk '{print $1}')" == "${SILERO_SHA256}" ]]; then
    say "Silero VAD 权重校验通过"
  else
    warn "Silero VAD 权重缺失或哈希不匹配：${silero}"
    failures=$((failures + 1))
  fi
  return "${failures}"
}

run_checks() {
  local failures=0
  local command
  for command in git gh curl ditto shasum uv llama-server ffmpeg node npm; do
    check_command "${command}" || failures=$((failures + 1))
  done
  gh auth status >/dev/null 2>&1 || {
    warn "GitHub CLI 尚未登录；运行 gh auth login"
    failures=$((failures + 1))
  }
  verify_private_repo || failures=$((failures + 1))
  [[ -x "${REPO_ROOT}/.venv/bin/python" ]] || {
    warn "缺少 bridge Python 环境：${REPO_ROOT}/.venv"
    failures=$((failures + 1))
  }
  [[ -f "${REPO_ROOT}/.runtime/deepseek-harness/package.json" ]] || {
    warn "缺少固定 DSH checkout：${REPO_ROOT}/.runtime/deepseek-harness"
    failures=$((failures + 1))
  }
  [[ -x "${V3_ROOT}/setup-macos.sh" ]] || {
    warn "缺少固定 Xiaoman v3 checkout：${V3_ROOT}"
    failures=$((failures + 1))
  }
  verify_local_assets || failures=$((failures + $?))
  if [[ -x "${V3_ROOT}/setup-macos.sh" ]]; then
    local v3_args=(--check)
    (( WITH_AVATAR )) || v3_args+=(--no-avatar)
    "${V3_ROOT}/setup-macos.sh" "${v3_args[@]}" || failures=$((failures + 1))
  fi
  if (( failures == 0 )); then
    say "只读检查通过"
    return 0
  fi
  warn "检查发现 ${failures} 项缺失或不匹配"
  return 1
}

if (( CHECK_ONLY )); then
  require_target_mac
  run_checks || exit 1
  exit 0
fi

require_target_mac
command -v xcode-select >/dev/null 2>&1 || fail "系统缺少 xcode-select"
xcode-select -p >/dev/null 2>&1 ||
  fail "请先运行 xcode-select --install，完成后重试"
command -v brew >/dev/null 2>&1 ||
  fail "请先从 https://brew.sh 安装 Homebrew"

install_formula_for_command() {
  local command="$1"
  local formula="$2"
  if ! command -v "${command}" >/dev/null 2>&1; then
    say "安装公开依赖：${formula}"
    brew install "${formula}"
  fi
}

install_formula_for_command gh gh
install_formula_for_command uv uv
install_formula_for_command llama-server llama.cpp
install_formula_for_command ffmpeg ffmpeg
install_formula_for_command node node

gh auth status >/dev/null 2>&1 || fail "请先运行 gh auth login，然后重试"
gh auth setup-git >/dev/null
verify_private_repo || fail "拒绝从未确认的公开仓库安装私人素材"

if [[ "${V3_ROOT}" == "${REPO_ROOT}/${V3_CHECKOUT_REL}/xiaoman-v3" ]]; then
  if [[ ! -d "${V3_CHECKOUT}/.git" ]]; then
    say "克隆固定 Xiaoman v3 私有源码"
    mkdir -p "$(dirname -- "${V3_CHECKOUT}")"
    gh repo clone "${V3_REPOSITORY}" "${V3_CHECKOUT}"
  fi
  [[ -d "${V3_CHECKOUT}/.git" ]] || fail "Xiaoman v3 目标不是 Git checkout：${V3_CHECKOUT}"
  git -C "${V3_CHECKOUT}" diff --quiet || fail "Xiaoman v3 checkout 有未提交改动，拒绝覆盖"
  git -C "${V3_CHECKOUT}" diff --cached --quiet || fail "Xiaoman v3 checkout 有已暂存改动，拒绝覆盖"
  say "固定 Xiaoman v3 commit：${V3_COMMIT}"
  git -C "${V3_CHECKOUT}" fetch --quiet origin "${V3_COMMIT}"
  git -C "${V3_CHECKOUT}" checkout --detach "${V3_COMMIT}"
else
  say "使用显式 XIAOMAN_V3_ROOT：${V3_ROOT}"
fi

if [[ -z "${ASSETS_FROM}" ]]; then
  DOWNLOAD_DIR="${REPO_ROOT}/.runtime/downloads/${ASSET_TAG}"
  EXTRACT_DIR="${REPO_ROOT}/.runtime/private-assets/${ASSET_SHA256}"
  mkdir -p "${DOWNLOAD_DIR}" "${EXTRACT_DIR}"
  say "下载私有素材 Release：${ASSET_REPOSITORY}/${ASSET_TAG}"
  gh release download "${ASSET_TAG}" \
    --repo "${ASSET_REPOSITORY}" \
    --pattern "${ASSET_NAME}" \
    --clobber \
    --dir "${DOWNLOAD_DIR}"
  ASSET_ARCHIVE="${DOWNLOAD_DIR}/${ASSET_NAME}"
  [[ "$(shasum -a 256 "${ASSET_ARCHIVE}" | awk '{print $1}')" == "${ASSET_SHA256}" ]] ||
    fail "私有素材包 SHA-256 不匹配"
  say "解压已验证的私有素材"
  ditto -x -k "${ASSET_ARCHIVE}" "${EXTRACT_DIR}"
  ASSETS_FROM="${EXTRACT_DIR}/xiaoman-assets"
fi

[[ -d "${ASSETS_FROM}/xiaoman-v3" ]] || fail "素材目录缺少 xiaoman-v3/：${ASSETS_FROM}"
SILERO_SOURCE="${ASSETS_FROM}/dsh-local/models/silero-vad/silero_vad.jit"
[[ -f "${SILERO_SOURCE}" ]] || fail "素材目录缺少 Silero VAD 权重"
[[ "$(shasum -a 256 "${SILERO_SOURCE}" | awk '{print $1}')" == "${SILERO_SHA256}" ]] ||
  fail "Silero VAD 权重 SHA-256 不匹配"
mkdir -p "${REPO_ROOT}/models/silero-vad"
cp -p "${SILERO_SOURCE}" "${REPO_ROOT}/models/silero-vad/silero_vad.jit"

say "准备 Xiaoman v3 的 ASR、TTS 与 Avatar 环境"
v3_setup_args=(--assets-from "${ASSETS_FROM}")
(( WITH_AVATAR )) || v3_setup_args+=(--no-avatar)
"${V3_ROOT}/setup-macos.sh" "${v3_setup_args[@]}"

say "创建本仓库 bridge Python 3.12 环境"
uv python install 3.12
if [[ ! -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  uv venv --python 3.12 "${REPO_ROOT}/.venv"
fi
uv pip install --python "${REPO_ROOT}/.venv/bin/python" -r "${REPO_ROOT}/bridge/requirements.txt"

say "准备固定版本的 DeepSeek Harness 与 Xiaoman 插件"
"${SCRIPT_DIR}/bootstrap-dsh.sh"

say "执行最终只读检查"
run_checks || fail "安装完成，但最终检查未通过"
say "安装完成。首次运行：./scripts/run-local.sh --online"
