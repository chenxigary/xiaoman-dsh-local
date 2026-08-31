"""Safe, checkout-local character/avatar registry for the voice bridge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
XIAOMAN_ROOT = ROOT / "assets" / "xiaoman"
AVATAR_CONFIG = XIAOMAN_ROOT / "config" / "avatar.json"
CHARACTERS = frozenset({"default", "xiaoman"})
STATES = frozenset({"idle", "listening", "thinking", "speaking"})


def normalize_character(value: str | None) -> str:
    return "xiaoman" if (value or "").strip().lower() == "xiaoman" else "default"


def load_avatar_config() -> dict[str, Any]:
    if not AVATAR_CONFIG.is_file():
        return {"namespace": "xiaoman", "states": {}, "fallback": "idle"}
    with AVATAR_CONFIG.open(encoding="utf-8") as stream:
        value = json.load(stream)
    return value if isinstance(value, dict) else {"namespace": "xiaoman", "states": {}, "fallback": "idle"}


def _safe_state_dir(character: str, state: str) -> Path | None:
    if character != "xiaoman" or state not in STATES:
        return None
    config = load_avatar_config()
    states = config.get("states", {})
    relative = states.get(state) if isinstance(states, dict) else None
    if not isinstance(relative, str):
        return None
    candidate = (AVATAR_CONFIG.parent / relative).resolve()
    try:
        candidate.relative_to(XIAOMAN_ROOT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def state_media(character: str, state: str) -> list[dict[str, str]]:
    normalized_character = normalize_character(character)
    normalized_state = state if state in STATES else "idle"
    directory = _safe_state_dir(normalized_character, normalized_state)
    if directory is None and normalized_state != "idle":
        directory = _safe_state_dir(normalized_character, "idle")
        normalized_state = "idle"
    if directory is None:
        return []
    entries: list[dict[str, str]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in {".mp4", ".webm", ".ogg", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp"}:
            media_type = "video" if path.suffix.lower() in {".mp4", ".webm", ".ogg", ".mov", ".m4v"} else "image"
            entries.append({"name": path.name, "type": media_type, "state": normalized_state})
    if not entries and normalized_state != "idle":
        idle = _safe_state_dir(normalized_character, "idle")
        if idle is not None:
            for path in sorted(idle.iterdir()):
                if path.suffix.lower() in {".mp4", ".webm", ".ogg", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp"}:
                    media_type = "video" if path.suffix.lower() in {".mp4", ".webm", ".ogg", ".mov", ".m4v"} else "image"
                    entries.append({"name": path.name, "type": media_type, "state": "idle"})
    return entries


__all__ = ["AVATAR_CONFIG", "CHARACTERS", "STATES", "load_avatar_config", "normalize_character", "state_media"]
