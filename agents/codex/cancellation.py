"""Cancellation primitives used by turn streams."""

from __future__ import annotations

import asyncio


class CancellationToken:
    """A small cooperative cancellation token with an observable reason."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    async def wait(self) -> str:
        await self._event.wait()
        return self._reason or "cancelled"
