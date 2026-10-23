"""Typed failures for deterministic KPI calculation."""

from __future__ import annotations


class MetricsError(RuntimeError):
    """Base class for metric input and consistency failures."""


class MetricsInputError(MetricsError):
    """The supplied checkpoint/event history cannot yield trustworthy metrics."""
