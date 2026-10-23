"""Failures raised by portable reports and replay bundles."""

from __future__ import annotations


class ReportingError(RuntimeError):
    """Base class for report construction and portable replay failures."""


class ReportConflictError(ReportingError):
    """Report inputs disagree on run, snapshot, proposal, or validation content."""


class ReplayBundleError(ReportingError):
    """A portable replay bundle is malformed, incomplete, or inconsistent."""
