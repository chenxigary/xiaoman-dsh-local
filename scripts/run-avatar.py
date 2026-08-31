#!/usr/bin/env python3
"""Load the DSH continuity overlay, then execute authoritative LiveTalking."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

livetalking_root = Path(os.environ.get("XIAOMAN_LIVETALKING_ROOT", Path.cwd())).resolve()
app_path = livetalking_root / "app.py"
if not app_path.is_file():
    raise SystemExit(f"LiveTalking app.py is missing: {app_path}")
if str(livetalking_root) not in sys.path:
    sys.path.insert(0, str(livetalking_root))

from bridge.livetalking_continuity import install_livetalking_continuity  # noqa: E402
from bridge.livetalking_warmup import install_session_warmup  # noqa: E402
from bridge.livetalking_video import install_video_codec_preference  # noqa: E402
from server import webrtc  # type: ignore  # noqa: E402
from server.session_manager import SessionManager  # type: ignore  # noqa: E402

install_livetalking_continuity(webrtc)
session_warmup = os.environ.get("XIAOMAN_AVATAR_SESSION_WARMUP", "1").strip().lower()
if session_warmup not in {"0", "false", "no", "off"}:
    install_session_warmup(SessionManager)
video_codec = os.environ.get("XIAOMAN_AVATAR_VIDEO_CODEC", "").strip()
if video_codec:
    from aiortc.rtcrtptransceiver import RTCRtpTransceiver  # noqa: E402

    install_video_codec_preference(RTCRtpTransceiver, video_codec)
runpy.run_path(str(app_path), run_name="__main__")
