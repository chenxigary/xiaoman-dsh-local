#!/usr/bin/env bash
# Verify that every expected Xiaoman service is local, reachable, and ready.
set -euo pipefail

EXPECT_AVATAR="${XIAOMAN_EXPECT_AVATAR:-1}"
LLM_URL="${LOCAL_LLM_URL:-http://127.0.0.1:8090}"
AVATAR_URL="${XIAOMAN_AVATAR_URL:-http://127.0.0.1:8010}"
VOICE_URL="${DSH_VOICE_RUNTIME_URL:-http://127.0.0.1:7860}"
BRIDGE_URL="${VOICE_BRIDGE_URL:-http://127.0.0.1:8765}"
DSH_URL="${DSH_URL:-http://127.0.0.1:3080}"
FAILURES=0

ok() {
  printf '[status-local] OK: %s\n' "$*"
}

bad() {
  printf '[status-local] FAIL: %s\n' "$*" >&2
  FAILURES=$((FAILURES + 1))
}

probe() {
  local label="$1"
  local url="$2"
  if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
    ok "${label} -> ${url}"
  else
    bad "${label} unreachable -> ${url}"
  fi
}

probe "llama.cpp" "${LLM_URL}/health"
probe "Xiaoman Voice Runtime" "${VOICE_URL}/api/voice-runtime/v1/health"
probe "voice bridge" "${BRIDGE_URL}/api/health"
probe "DSH Web" "${DSH_URL}"
if [[ "${EXPECT_AVATAR}" == "1" ]]; then
  probe "LiveTalking Avatar" "${AVATAR_URL}/api/admin/config"
fi

BRIDGE_JSON="$(curl -fsS --max-time 5 "${BRIDGE_URL}/api/health" 2>/dev/null || true)"
if python3 - "${BRIDGE_JSON}" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if value.get("local_only") is True else 1)
PY
then
  ok "bridge reports local_only=true"
else
  bad "bridge did not prove the local-only build boundary"
fi

CODEX_JSON="$(curl -fsS --max-time 5 "${BRIDGE_URL}/api/codex/health" 2>/dev/null || true)"
if python3 - "${CODEX_JSON}" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
codex = value.get("codex", {})
raise SystemExit(0 if value.get("status") == "disabled" and codex.get("enabled") is False else 1)
PY
then
  ok "Codex model route is disabled"
else
  bad "Codex route is not conclusively disabled"
fi

if (( FAILURES > 0 )); then
  printf '[status-local] %s check(s) failed. Inspect logs/ before retrying.\n' "${FAILURES}" >&2
  exit 1
fi
printf '%s\n' '[status-local] all expected local services are ready'
