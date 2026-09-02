#!/usr/bin/env bash
# Start the existing Xiaoman v3 LiveTalking/Wav2Lip runtime on loopback.
# The heavyweight model and preprocessed avatar stay in the source checkout;
# override XIAOMAN_V3_ROOT when that checkout moves.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
XIAOMAN_ROOT="${XIAOMAN_V3_ROOT:-${REPO_ROOT}/.runtime/macos-local-voice-agents/xiaoman-v3}"
LIVETALKING_ROOT="${XIAOMAN_LIVETALKING_ROOT:-${XIAOMAN_ROOT}/avatar/livetalking}"
AVATAR_PYTHON="${XIAOMAN_AVATAR_PYTHON:-${XIAOMAN_ROOT}/../.venv-v3-avatar/bin/python}"
AVATAR_PORT="${XIAOMAN_AVATAR_PORT:-8010}"
AVATAR_HOST="${XIAOMAN_AVATAR_HOST:-127.0.0.1}"
AVATAR_ID="${XIAOMAN_AVATAR_ID:-xiaoman-v3-original-idle}"
AVATAR_RUNNER="${REPO_ROOT}/scripts/run-avatar.py"

required=(
  "${LIVETALKING_ROOT}/app.py"
  "${LIVETALKING_ROOT}/models/wav2lip.pth"
  "${LIVETALKING_ROOT}/models/s3fd.pth"
  "${LIVETALKING_ROOT}/data/avatars/${AVATAR_ID}"
  "${AVATAR_PYTHON}"
  "${AVATAR_RUNNER}"
)
for path in "${required[@]}"; do
  if [[ ! -e "${path}" ]]; then
    echo "[avatar] ERROR: required Xiaoman v3 runtime path is missing: ${path}" >&2
    echo "[avatar] set XIAOMAN_V3_ROOT or XIAOMAN_AVATAR_PYTHON to the correct local path" >&2
    exit 1
  fi
done
if [[ ! "${AVATAR_PORT}" =~ ^[0-9]+$ ]] || (( AVATAR_PORT < 1 || AVATAR_PORT > 65535 )); then
  echo "[avatar] ERROR: XIAOMAN_AVATAR_PORT must be a valid TCP port" >&2
  exit 1
fi
if [[ "${AVATAR_HOST}" != "127.0.0.1" && "${AVATAR_HOST}" != "::1" ]]; then
  echo "[avatar] ERROR: XIAOMAN_AVATAR_HOST must remain loopback (127.0.0.1 or ::1)" >&2
  exit 1
fi

export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export PYTHONUNBUFFERED=1
export LIVETALKING_HOST="${AVATAR_HOST}"
# Match Xiaoman v3's accepted baseline. CPU remains an explicit isolation
# experiment, but is too slow to be the product default on Apple Silicon.
export V3_AVATAR_DEVICE="${XIAOMAN_AVATAR_DEVICE:-${V3_AVATAR_DEVICE:-auto}}"
# On this full-resolution Avatar, aiortc's H.264 path intermittently blocks
# both RTP tracks for 0.6-1.4s. Repeated fresh-process WebRTC probes kept VP8
# below 144ms for video and 26ms for audio. Preserve an explicit restart-scoped
# escape hatch for future codec/device A/B work.
export XIAOMAN_AVATAR_VIDEO_CODEC="${XIAOMAN_AVATAR_VIDEO_CODEC:-VP8}"
export XIAOMAN_AVATAR_SESSION_WARMUP="${XIAOMAN_AVATAR_SESSION_WARMUP:-1}"
echo "[avatar] source: ${LIVETALKING_ROOT}"
echo "[avatar] WebRTC signaling: http://${AVATAR_HOST}:${AVATAR_PORT}"
echo "[avatar] preferred video codec: ${XIAOMAN_AVATAR_VIDEO_CODEC}"
echo "[avatar] per-session Wav2Lip warm-up: ${XIAOMAN_AVATAR_SESSION_WARMUP}"
cd -- "${LIVETALKING_ROOT}"
# Keep the same stride as Xiaoman v3's signed-off strict-sync baseline.
export XIAOMAN_LIVETALKING_ROOT="${LIVETALKING_ROOT}"

# Xiaoman v3's avatar-quality work added flags this repo's pinned LiveTalking
# does not have yet, so each one is offered only when the target checkout
# declares it -- passing an unknown flag would make argparse abort at boot.
supports_flag() {
  grep -q -- "'$1'" "${LIVETALKING_ROOT}/config.py" 2>/dev/null
}
quality_args=()
# fixed stride 4 reuses one Wav2Lip pose across four frames; v3 measured 73% of
# frames repeating a pose and only 1.35x idle mouth motion.  The adaptive
# controller retunes from measured inference latency and reached 2.27-2.46x.
if supports_flag --inference_stride_mode; then
  quality_args+=(--inference_stride_mode "${XIAOMAN_AVATAR_STRIDE_MODE:-adaptive}")
fi
# The idle loop otherwise advances one source frame per rendered frame, which
# reads as restless on a short clip.  0.5 holds each source frame twice.
if supports_flag --idle_motion_scale; then
  quality_args+=(--idle_motion_scale "${XIAOMAN_AVATAR_IDLE_MOTION_SCALE:-0.5}")
fi
if ((${#quality_args[@]})); then
  echo "[avatar] quality flags: ${quality_args[*]}"
else
  echo "[avatar] pinned LiveTalking predates the v3 quality flags; using defaults"
fi

exec "${AVATAR_PYTHON}" "${AVATAR_RUNNER}" \
  --config '' \
  --transport webrtc \
  --model wav2lip \
  --batch_size "${XIAOMAN_AVATAR_BATCH_SIZE:-1}" \
  --inference_stride "${XIAOMAN_AVATAR_INFERENCE_STRIDE:-4}" \
  --speech_rms_threshold "${XIAOMAN_AVATAR_SPEECH_RMS_THRESHOLD:-0.006}" \
  ${quality_args[@]+"${quality_args[@]}"} \
  --avatar_id "${AVATAR_ID}" \
  --listenport "${AVATAR_PORT}"
