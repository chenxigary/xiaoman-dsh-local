#!/usr/bin/env bash
# One-command local start for THIS machine.
#
# Thin wrapper over scripts/run-local.sh.  It adds the things that script cannot
# know about here:
#   1. LLAMA_API_KEY is exported corp-wide, and llama.cpp reads that exact name
#      as a server-side auth key -> every local client gets 401.
#   2. a site that cannot reach registry.npmjs.org needs a mirror for the DSH
#      steps; that hostname belongs in start.local.sh, not in the repository.
#   3. :8090 runs bridge/model_router.py instead of llama-server directly, so
#      the UI can switch models.  The router keeps exactly one llama-server
#      alive and swaps it on demand; it also carries the tuned flags
#      (-fa / KV quant / ubatch) that start-local-llm.sh cannot pass.
#
# Run it from a real terminal.  MLX's TTS/ASR kernels JIT-compile Metal and that
# is denied inside an agent sandbox.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

CATALOG=config/local-models.json
MODEL_KEY="${MODEL:-}"
PASS_THROUGH=()

python_bin() {
  if [[ -x .venv/bin/python ]]; then printf '%s' .venv/bin/python; else command -v python3; fi
}

catalog_ids() {
  "$(python_bin)" -c "
import json
print(' '.join(m['id'] for m in json.load(open('${CATALOG}'))['models']))
"
}

# The catalog is the single source of truth for ids, paths, and context sizes;
# the launcher only picks which one starts warm.
catalog_report() {
  "$(python_bin)" -c "
import json, os
d = json.load(open('${CATALOG}'))
default = os.environ.get('ROUTER_DEFAULT_MODEL') or d['default']
for m in d['models']:
    ok = 'OK     ' if os.path.isfile(os.path.expanduser(m['path'])) else 'MISSING'
    print(f\"  {'*' if m['id'] == default else ' '} {m['id']:<18} {ok}  {m['name']}\")
"
}

# scripts/stop-local.sh only kills PIDs it recorded in .run/, and still prints
# "expected local stack processes are stopped" when there was no record at all.
# Sweep the known ports afterwards, killing a listener only when its command
# line matches what that port is supposed to be running.  :8190 is the router's
# llama-server child, swept last in case the router died without reaping it.
stop_all() {
  ./scripts/stop-local.sh || true
  ./scripts/stop-local-llm.sh || true
  local entry port want pid command
  for entry in '8090:bridge.model_router' '8190:llama-server' \
               '7860:gateway.app:app' '8765:voice_bridge:app' \
               '8010:run-avatar.py' '3080:apps/cli/src/bin.ts'; do
    port="${entry%%:*}"; want="${entry#*:}"
    while read -r pid; do
      [[ -n "${pid}" ]] || continue
      command="$(ps -o command= -p "${pid}" 2>/dev/null || true)"
      if [[ "${command}" == *"${want}"* ]]; then
        printf '[stop] :%s -> kill %s\n' "${port}" "${pid}"
        kill "${pid}" 2>/dev/null || true
      else
        printf '[stop] :%s 保留 PID %s（不是本栈的进程）\n' "${port}" "${pid}"
      fi
    done < <(lsof -nP -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)
  done
  printf '[stop] done\n'
}

usage() {
  cat <<EOF
用法：./start.sh [选项]

  --model KEY   启动时预热哪个模型（不填用 ${CATALOG} 里的 default）
                UI 里可随时切换，切换会在后台换掉 llama-server
  --models      列出模型目录并退出
  --status      只看状态
  --stop        停掉整栈
  其余参数原样转发给 scripts/run-local.sh
  （--no-avatar / --no-open / --online / --profile NAME）

模型路径和 context 大小改 ${CATALOG}；
改完要同步 config/dsh-local-model.patch.yml 里的 model id。
EOF
}

while (($#)); do
  case "$1" in
    --model)  (($# >= 2)) || { echo '--model 需要参数' >&2; exit 1; }; MODEL_KEY="$2"; shift ;;
    --models) catalog_report; exit 0 ;;
    --status) exec env XIAOMAN_EXPECT_AVATAR=1 ./scripts/status-local.sh ;;
    --stop)   stop_all; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) PASS_THROUGH+=("$1") ;;
  esac
  shift
done

[[ -f "${CATALOG}" ]] || { printf '缺少模型目录：%s\n' "${CATALOG}" >&2; exit 1; }
if [[ -n "${MODEL_KEY}" ]]; then
  if [[ " $(catalog_ids) " != *" ${MODEL_KEY} "* ]]; then
    printf '未知模型：%s\n可用：\n' "${MODEL_KEY}" >&2
    catalog_report >&2
    exit 1
  fi
  export ROUTER_DEFAULT_MODEL="${MODEL_KEY}"
fi

# The pinned v3 (xiaoman.local.lock.json, commit d4fba43) predates the
# avatar-quality work, so it has neither --inference_stride_mode nor
# --idle_motion_scale and its preprocessed avatar still carries the burnt-in
# watermark.  Prefer the working checkout when it is present; XIAOMAN_V3_ROOT
# still wins, and V3_PINNED=1 forces the locked tree back.
V3_WORKING="${HOME}/service/macos-local-voice-agents/xiaoman-v3"
if [[ -z "${XIAOMAN_V3_ROOT:-}" && "${V3_PINNED:-0}" != "1" && -f "${V3_WORKING}/gateway/app.py" ]]; then
  export XIAOMAN_V3_ROOT="${V3_WORKING}"
  printf '[start] v3: working checkout (%s)\n' "$(git -C "${V3_WORKING}/.." rev-parse --short HEAD 2>/dev/null || echo '?')"
else
  printf '[start] v3: %s\n' "${XIAOMAN_V3_ROOT:-pinned}"
fi

unset LLAMA_API_KEY
# Placeholder credential for the local-qwen route; see config/dsh-local-model.patch.yml.
# llama.cpp ignores it -- it only has to be non-empty and header-safe.
export LOCAL_QWEN_API_KEY="${LOCAL_QWEN_API_KEY:-local}"

# Site-local settings live outside the repository (start.local.sh is gitignored)
# so a checkout carries no network- or employer-specific hostnames.  Put an npm
# registry mirror, proxy, or alternate model root there, e.g.
#   export npm_config_registry=https://<your-mirror>/
if [[ -f start.local.sh ]]; then
  # shellcheck source=/dev/null
  source ./start.local.sh
fi
if [[ -z "${npm_config_registry:-}" ]]; then
  printf '[start] 提示：未设置 npm_config_registry；若本机到 registry.npmjs.org 不通，\n'
  printf '[start]       DSH 相关步骤会失败，在 start.local.sh 里配置镜像即可。\n'
fi

mkdir -p logs .run
# run-local.sh starts llama.cpp on :8090 only when that port is cold, so putting
# the router there first is how it, and not a bare llama-server, owns the slot.
if curl -fsS --max-time 2 http://127.0.0.1:8090/health >/dev/null 2>&1; then
  printf '[start] :8090 已在运行，沿用现有进程\n'
else
  [[ -x .venv/bin/python ]] ||
    { printf '缺少 .venv，先跑 ./scripts/setup-macos-local.sh\n' >&2; exit 1; }
  printf '[start] :8090 model router（预热 %s）\n' "${ROUTER_DEFAULT_MODEL:-catalog default}"
  nohup .venv/bin/python -m uvicorn bridge.model_router:app \
    --host 127.0.0.1 --port 8090 >logs/model-router.log 2>&1 &
  printf '%s\n' "$!" >.run/local-llm.pid
  for _ in $(seq 1 180); do
    curl -fsS --max-time 2 http://127.0.0.1:8090/health >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS --max-time 2 http://127.0.0.1:8090/health >/dev/null 2>&1 ||
    { printf '[start] router 未就绪，见 logs/model-router.log\n' >&2; exit 1; }
fi

# macOS ships bash 3.2, where "${arr[@]}" on an empty array trips `set -u`.
exec ./scripts/run-local.sh ${PASS_THROUGH[@]+"${PASS_THROUGH[@]}"}
