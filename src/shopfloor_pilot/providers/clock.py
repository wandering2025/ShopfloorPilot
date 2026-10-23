"""Injectable monotonic clocks for provider deadlines and tests."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """The narrow clock surface the bounded provider needs."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production clock backed by the process monotonic timer and real sleep."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock; sleeping advances logical time without waiting."""

    def __init__(self, start: float = 0.0) -> None:
        if start < 0:
            raise ValueError("start must be non-negative")
        self._now = float(start)
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must be non-negative")
        self.sleep_calls.append(float(seconds))
        self._now += float(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("advance duration must be non-negative")
        self._now += float(seconds)
