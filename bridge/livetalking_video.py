"""Runtime-only LiveTalking video transport controls.

The authoritative Xiaoman v3 checkout remains read-only.  This module gives
the DSH launcher a restart-scoped codec A/B switch while preserving
LiveTalking's normal codec list and negotiation behavior.
"""

from __future__ import annotations

from typing import Any, Iterable


SUPPORTED_VIDEO_CODECS = {"H264", "VP8"}


def _codec_name(codec: Any) -> str:
    value = getattr(codec, "name", "") or getattr(codec, "mimeType", "")
    return str(value).rsplit("/", 1)[-1].upper()


def prefer_video_codec(codecs: Iterable[Any], preferred: str) -> list[Any]:
    """Return stable codec preferences with one primary video codec first."""

    normalized = str(preferred).strip().upper()
    if normalized not in SUPPORTED_VIDEO_CODECS:
        raise ValueError(f"unsupported Avatar video codec: {preferred}")
    values = list(codecs)
    primary = [codec for codec in values if _codec_name(codec) == normalized]
    alternatives = [
        codec
        for codec in values
        if _codec_name(codec) not in {normalized, "RTX"}
    ]
    retransmission = [codec for codec in values if _codec_name(codec) == "RTX"]
    return primary + alternatives + retransmission


def install_video_codec_preference(transceiver_class: Any, preferred: str) -> bool:
    """Patch aiortc codec ordering before LiveTalking builds an answer."""

    normalized = str(preferred).strip().upper()
    if normalized not in SUPPORTED_VIDEO_CODECS:
        raise ValueError(f"unsupported Avatar video codec: {preferred}")
    installed = getattr(transceiver_class, "_xiaoman_video_codec", None)
    if installed is not None:
        if installed != normalized:
            raise RuntimeError(
                f"Avatar video codec already pinned to {installed}, cannot switch to {normalized}"
            )
        return False

    original = transceiver_class.setCodecPreferences

    def patched(self: Any, codecs: Iterable[Any]) -> Any:
        return original(self, prefer_video_codec(codecs, normalized))

    transceiver_class.setCodecPreferences = patched
    transceiver_class._xiaoman_video_codec = normalized
    return True


def video_codecs_from_sdp(sdp: str) -> list[str]:
    """Return video RTP codec names in the negotiated payload order."""

    payloads: list[str] = []
    rtpmap: dict[str, str] = {}
    in_video = False
    for raw_line in str(sdp).replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith("m="):
            fields = line.split()
            in_video = bool(fields and fields[0] == "m=video")
            if in_video:
                payloads = fields[3:]
            continue
        if in_video and line.startswith("a=rtpmap:"):
            descriptor = line.removeprefix("a=rtpmap:")
            payload, _, encoding = descriptor.partition(" ")
            if payload and encoding:
                rtpmap[payload] = encoding.split("/", 1)[0].upper()
    return [rtpmap[payload] for payload in payloads if payload in rtpmap]


__all__ = [
    "SUPPORTED_VIDEO_CODECS",
    "install_video_codec_preference",
    "prefer_video_codec",
    "video_codecs_from_sdp",
]
