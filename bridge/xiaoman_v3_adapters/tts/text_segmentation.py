"""Deterministic Chinese text segmentation for low-drift TTS."""

from __future__ import annotations

from collections.abc import Iterable


STRONG_BOUNDARIES = frozenset("。！？；\n.!?;")
SOFT_BOUNDARIES = frozenset("，、,：:")


def _split_long_clause(clause: str, *, target_chars: int, max_chars: int) -> list[str]:
    pieces: list[str] = []
    remaining = clause
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        cut = -1
        for index, char in enumerate(window):
            if char in SOFT_BOUNDARIES and index + 1 >= target_chars:
                cut = index + 1
        if cut < 1:
            for index, char in enumerate(window):
                if char in SOFT_BOUNDARIES:
                    cut = index + 1
        if cut < 1:
            cut = max_chars
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        pieces.append(remaining)
    return pieces


def _strong_clauses(text: str) -> Iterable[str]:
    start = 0
    for index, char in enumerate(text):
        if char in STRONG_BOUNDARIES:
            yield text[start : index + 1]
            start = index + 1
    if start < len(text):
        yield text[start:]


def split_for_tts(
    text: str,
    *,
    min_chars: int = 8,
    target_chars: int = 24,
    max_chars: int = 28,
) -> list[str]:
    """Split text into natural, bounded chunks suitable for Qwen3-TTS.

    Strong punctuation is respected first. Long clauses prefer commas near the
    target length and hard-split only when no punctuation is available.
    Adjacent short fragments are merged, so ``你好。`` is not generated as an
    unnatural one-packet utterance. Returned chunks concatenate to the stripped
    input exactly.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if min_chars < 1 or target_chars < min_chars or max_chars < target_chars:
        raise ValueError("expected 1 <= min_chars <= target_chars <= max_chars")

    source = text.strip()
    if not source:
        return []

    raw: list[str] = []
    for clause in _strong_clauses(source):
        raw.extend(
            _split_long_clause(
                clause, target_chars=target_chars, max_chars=max_chars
            )
        )

    merged: list[str] = []
    pending = ""
    for piece in raw:
        if not piece:
            continue
        if not pending:
            pending = piece
            continue
        combined = pending + piece
        short_merge_cap = max_chars + min_chars - 1
        if len(combined) <= max_chars or (
            len(pending) < min_chars and len(combined) <= short_merge_cap
        ):
            pending = combined
        else:
            merged.append(pending)
            pending = piece
    if pending:
        merged.append(pending)
    return merged


class StreamingTextSegmenter:
    """Merge tiny LLM sentence fragments before sending them to TTS.

    ``LLMClient.chat_stream`` emits complete sentences, but conversational
    replies often contain tiny fragments such as ``嗯。``. Starting a separate
    model generation for each fragment creates audible joins. This adapter
    holds only a short trailing fragment while allowing normal/long segments
    to reach TTS immediately.
    """

    def __init__(
        self,
        *,
        min_chars: int = 8,
        target_chars: int = 24,
        max_chars: int = 28,
    ) -> None:
        if min_chars < 1 or target_chars < min_chars or max_chars < target_chars:
            raise ValueError("expected 1 <= min_chars <= target_chars <= max_chars")
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self._pending = ""

    def push(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        self._pending += text
        pieces = split_for_tts(
            self._pending,
            min_chars=self.min_chars,
            target_chars=self.target_chars,
            max_chars=self.max_chars,
        )
        if not pieces:
            return []
        if len(pieces[-1]) < self.min_chars:
            self._pending = pieces.pop()
        else:
            self._pending = ""
        return pieces

    def finish(self) -> list[str]:
        pieces = split_for_tts(
            self._pending,
            min_chars=self.min_chars,
            target_chars=self.target_chars,
            max_chars=self.max_chars,
        )
        self._pending = ""
        return pieces


__all__ = ["StreamingTextSegmenter", "split_for_tts"]
