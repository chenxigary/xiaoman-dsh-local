#!/usr/bin/env bash
# Friendly one-command launcher for the private, local-model-only distribution.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
PROFILE="auto"
ONLINE=0
WITH_AVATAR=1
OPEN_UI=1
CHECK_ONLY=0
STATUS_ONLY=0

usage() {
  cat <<'EOF'
用法：
  ./scripts/run-local.sh [选项]

选项：
  --profile NAME   auto、efficient、balanced、performance（默认 auto）
  --online        本次允许从 Hugging Face 下载缺失的公开模型
  --offline       严格使用本机缓存（默认）
  --no-avatar     不启动 LiveTalking，便于先测语音/LLM 基线
  --no-open       启动后不自动打开浏览器
  --check         只检查安装与私有素材，不启动
  --status        只检查当前本地服务状态
  -h, --help      显示帮助

M4/64GB 的 auto 会选择 performance：Qwen3-14B Q4、16K context、
Qwen3-TTS 1.7B 4-bit。首次运行请加 --online；日常运行保持默认 offline。
EOF
}

fail() {
  printf '[run-local] ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --profile)
      (($# >= 2)) || fail "--profile 需要参数"
      PROFILE="$2"
      shift
      ;;
    --online) ONLINE=1 ;;
    --offline) ONLINE=0 ;;
    --no-avatar) WITH_AVATAR=0 ;;
    --no-open) OPEN_UI=0 ;;
    --check) CHECK_ONLY=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1（使用 --help 查看帮助）" ;;
  esac
  shift
done

case "${PROFILE}" in
  auto|efficient|balanced|performance) ;;
  *) fail "profile 必须是 auto、efficient、balanced 或 performance" ;;
esac

if (( CHECK_ONLY )); then
  check_args=(--check)
  (( WITH_AVATAR )) || check_args+=(--no-avatar)
  exec "${SCRIPT_DIR}/setup-macos-local.sh" "${check_args[@]}"
fi
if (( STATUS_ONLY )); then
  XIAOMAN_EXPECT_AVATAR="${WITH_AVATAR}" exec "${SCRIPT_DIR}/status-local.sh"
fi

resolve_profile() {
  local requested="$1"
  local memory_bytes
  if [[ "${requested}" != "auto" ]]; then
    printf '%s\n' "${requested}"
    return
  fi
  memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || printf '0')"
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

RESOLVED_PROFILE="$(resolve_profile "${PROFILE}")"
case "${RESOLVED_PROFILE}" in
  efficient)
    TTS_MODEL="mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
    ;;
  balanced)
    TTS_MODEL="mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
    ;;
  performance)
    TTS_MODEL="mlx-community/Qwen3-TTS-12Hz-1.7B-Base-4bit"
    ;;
esac

# Remove cloud-provider credentials from the child environment. The checked-in
# overlay and bridge also disable their routes, so this is an additional fence.
unset OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY
export XIAOMAN_LOCAL_ONLY=1
export XIAOMAN_PERFORMANCE_PROFILE="${RESOLVED_PROFILE}"
export LOCAL_LLM_ALIAS=xiaoman-local
export LOCAL_LLM_ALLOW_DOWNLOAD="${ONLINE}"
export XIAOMAN_V3_ROOT="${XIAOMAN_V3_ROOT:-${REPO_ROOT}/.runtime/macos-local-voice-agents/xiaoman-v3}"
export DSH_VOICE_RUNTIME_MODE=v3
export V3_LLM_BACKEND=local
export V3_TTS_MODEL="${V3_TTS_MODEL:-${TTS_MODEL}}"
export OPEN_BROWSER="${OPEN_UI}"
export LOCAL_LLM_HEALTH_TIMEOUT_SEC="${LOCAL_LLM_HEALTH_TIMEOUT_SEC:-360}"
export VOICE_RUNTIME_HEALTH_TIMEOUT_SEC="${VOICE_RUNTIME_HEALTH_TIMEOUT_SEC:-600}"
export TTS_WARMUP_TIMEOUT_SEC="${TTS_WARMUP_TIMEOUT_SEC:-300}"

if (( ONLINE )); then
  export HF_HUB_OFFLINE=0
  export TRANSFORMERS_OFFLINE=0
else
  export HF_HUB_OFFLINE=1
  export TRANSFORMERS_OFFLINE=1
fi
if (( WITH_AVATAR )); then
  export START_AVATAR=1
  export V3_AVATAR_ENABLED=1
else
  export START_AVATAR=0
  export V3_AVATAR_ENABLED=0
fi

printf '[run-local] local-only profile: %s (requested: %s)\n' "${RESOLVED_PROFILE}" "${PROFILE}"
printf '[run-local] model acquisition: %s\n' "$([[ "${ONLINE}" == 1 ]] && printf 'online for missing public weights' || printf 'offline cache only')"
printf '[run-local] Avatar: %s\n' "$([[ "${WITH_AVATAR}" == 1 ]] && printf 'enabled' || printf 'disabled')"

"${SCRIPT_DIR}/start-all.sh"
XIAOMAN_EXPECT_AVATAR="${WITH_AVATAR}" "${SCRIPT_DIR}/status-local.sh"
