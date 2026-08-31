"""Cooperative cancellation primitives shared by voice providers.

Model runtimes generally cannot be interrupted safely in the middle of one
native inference call.  The token therefore provides two guarantees:

* callers can invalidate a turn immediately, before a queued inference starts;
* streaming providers check between generated chunks and never publish stale
  audio after cancellation.

The in-flight native call is allowed to return, while its result is discarded
by the provider/gateway generation check.  This is intentional and avoids
concurrent access to MLX or legacy model state.
"""

from __future__ import annotations

from threading import Event
from typing import Protocol, TypeAlias


class CancellationRequested(RuntimeError):
    """Raised when a provider observes that its turn was cancelled."""


class CancellationLike(Protocol):
    """Minimal duck-typed cancellation contract accepted by adapters."""

    @property
    def is_cancelled(self) -> bool: ...


class CancellationToken:
    """Thread-safe, reusable cancellation token for one logical turn."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    # ``cancelled`` is convenient for callers that use ``threading.Event``
    # terminology, while ``is_cancelled`` is the canonical adapter property.
    @property
    def cancelled(self) -> bool:
        return self.is_cancelled

    def throw_if_cancelled(self) -> None:
        raise_if_cancelled(self)


CancelInput: TypeAlias = CancellationToken | Event | CancellationLike | None


def is_cancelled(token: CancelInput) -> bool:
    """Read cancellation state from the supported token/event shapes."""

    if token is None:
        return False
    if isinstance(token, Event):
        return token.is_set()
    value = getattr(token, "is_cancelled", None)
    if callable(value):
        return bool(value())
    if value is not None:
        return bool(value)
    value = getattr(token, "cancelled", False)
    if callable(value):
        return bool(value())
    return bool(value)


def raise_if_cancelled(token: CancelInput) -> None:
    """Raise :class:`CancellationRequested` when ``token`` is cancelled."""

    if is_cancelled(token):
        raise CancellationRequested("voice turn was cancelled")


__all__ = [
    "CancelInput",
    "CancellationLike",
    "CancellationRequested",
    "CancellationToken",
    "is_cancelled",
    "raise_if_cancelled",
]
