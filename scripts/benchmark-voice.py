#!/usr/bin/env python3
"""Summarize captured voice latency events without inventing measurements.

The script is intentionally model- and hardware-agnostic.  It consumes JSONL
captured from bridge logs (or a manually exported browser console stream),
prints the planned 20/10/10/10 batch template, and computes percentiles only
from records actually supplied with ``--log``.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

BATCHES = {
    "short_question": 20,
    "ordinary_qa": 10,
    "coding": 10,
    "interruption_race": 10,
}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        # Bridge logger lines may prefix a JSON object with timestamp/level.
        start = text.find("{")
        if start < 0:
            continue
        try:
            value = json.loads(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "voice.latency":
            events.append(value)
    return events


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_stage: dict[str, list[float]] = {}
    for event in events:
        stage = str(event.get("stage") or event.get("operation") or "unknown")
        duration = event.get("duration_ms")
        if isinstance(duration, (int, float)):
            by_stage.setdefault(stage, []).append(float(duration))
    return {
        stage: {
            "samples": len(values),
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "min_ms": round(min(values), 3),
            "max_ms": round(max(values), 3),
        }
        for stage, values in sorted(by_stage.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, help="captured JSONL bridge/browser latency events")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    args = parser.parse_args()

    events = read_events(args.log) if args.log is not None else []
    report = {
        "status": "measured" if events else "template-only",
        "batches": {name: {"planned_runs": count, "completed_runs": 0, "notes": "fill only from an executed run"} for name, count in BATCHES.items()},
        "metrics": {
            "timeline": ["t0_speech_start", "t1_vad_endpoint", "t2_stt_start", "t3_stt_ready", "t4_queue_accepted", "t5_backend_started", "t6_first_speakable_sentence", "t7_tts_start", "t8_audio_ready", "t9_audio_played"],
            "codex": ["thread_ensure_ms", "turn_start_ms", "first_event_ms", "first_final_delta_ms", "tool_activity_ms", "interrupt_ack_ms", "terminal_ms"],
            "barge_in": ["speech_start_to_local_audible_stop_ms", "speech_start_to_interrupt_ack_ms", "speech_start_to_terminal_ms"],
        },
        "summary": summarize(events),
        "source_log": str(args.log) if args.log is not None else None,
        "hardware_results": "not collected",
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out is not None:
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
