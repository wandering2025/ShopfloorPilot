"""Bounded retry policy for one provider request."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry knobs; the ledger enforces the hard per-request and total caps."""

    max_retries_per_request: int = 2
    initial_backoff_seconds: float = 1.0
    multiplier: float = 2.0
    max_backoff_seconds: float = 4.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_retries_per_request, int) or isinstance(
            self.max_retries_per_request, bool
        ):
            raise ValueError("max_retries_per_request must be an integer")
        if not 0 <= self.max_retries_per_request <= 10:
            raise ValueError("max_retries_per_request must be between 0 and 10")
        for name, value in (
            ("initial_backoff_seconds", self.initial_backoff_seconds),
            ("multiplier", self.multiplier),
            ("max_backoff_seconds", self.max_backoff_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")

    def backoff(self, retry_index: int, requested_seconds: float | None = None) -> float:
        """Return the bounded delay before retry number ``retry_index`` (zero based)."""

        if retry_index < 0:
            raise ValueError("retry_index must be non-negative")
        exponential = self.initial_backoff_seconds * (self.multiplier**retry_index)
        delay = min(exponential, self.max_backoff_seconds)
        if requested_seconds is not None and math.isfinite(requested_seconds):
            delay = max(delay, requested_seconds)
        return min(max(delay, 0.0), self.max_backoff_seconds)
