"""Monotonic clock used for backwash step deadlines.

Step *advancement* is always decided from position feedback. Time is only used
for the action watchdog (did the device answer within its deadline?) and for the
fixed wash/rinse hold durations. Tests inject a deterministic clock so timeout
behaviour never sleeps.
"""

from __future__ import annotations

import time
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Current monotonic time in seconds."""


class SystemClock:
    """Wall clock backed by :func:`time.monotonic`."""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """Deterministic clock advanced explicitly by the tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self._now += seconds
        return self._now


def manual(start: float = 0.0) -> ManualClock:
    return ManualClock(start)


def system() -> SystemClock:
    return SystemClock()


def factory(supplier: Callable[[], float]) -> Clock:
    """Build a clock from any zero-argument time supplier."""

    class _Clock:
        def now(self) -> float:
            return float(supplier())

    return _Clock()
