"""Typed storage failures for authoritative SQLite state operations."""

from __future__ import annotations

from pathlib import Path


class StorageError(Exception):
    """Base class for expected storage-boundary failures."""


class DatabaseNotInitializedError(StorageError):
    """Raised when an operation requires a database schema that is not initialized."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = str(database_path) if database_path is not None else None
        location = f" at {self.database_path!r}" if self.database_path is not None else ""
        super().__init__(f"SQLite database{location} is not initialized")


class SchemaVersionError(StorageError):
    """Raised when a database schema cannot be handled by this application version."""

    def __init__(self, found: int, supported: int) -> None:
        self.found = found
        self.supported = supported
        super().__init__(
            f"unsupported database schema version {found}; supported version is {supported}"
        )


class RunNotFoundError(StorageError):
    """Raised when a requested manufacturing run does not exist."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} was not found")


class RunConflictError(StorageError):
    """Raised when creating or selecting a run conflicts with existing run state."""

    def __init__(self, run_id: str, detail: str = "run already exists") -> None:
        self.run_id = run_id
        self.detail = detail
        super().__init__(f"run conflict for {run_id!r}: {detail}")


class VersionConflictError(StorageError):
    """Raised when compare-and-swap observes a stale operational state version."""

    def __init__(self, run_id: str, expected: int, actual: int) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        # Descriptive aliases make exception handling self-documenting at API boundaries.
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(
            f"stale state for run {run_id!r}: expected version {expected}, actual version {actual}"
        )


class IdempotencyConflictError(StorageError):
    """Raised when an idempotency key is reused for different command content."""

    def __init__(self, run_id: str, key: str) -> None:
        self.run_id = run_id
        self.key = key
        self.idempotency_key = key
        super().__init__(
            f"idempotency key {key!r} for run {run_id!r} is already bound to another command"
        )


class RecordConflictError(StorageError):
    """Raised when an immutable lifecycle record conflicts with stored content."""

    def __init__(
        self,
        record_type: str,
        record_id: str,
        detail: str = "stored content differs",
    ) -> None:
        self.record_type = record_type
        self.record_id = record_id
        self.detail = detail
        super().__init__(f"{record_type} {record_id!r} conflicts: {detail}")


class CrossRunReferenceError(StorageError):
    """Raised when a record attempts to reference data owned by another run."""

    def __init__(
        self,
        record_type: str,
        record_id: str,
        expected_run_id: str,
        actual_run_id: str,
    ) -> None:
        self.record_type = record_type
        self.record_id = record_id
        self.expected_run_id = expected_run_id
        self.actual_run_id = actual_run_id
        super().__init__(
            f"{record_type} {record_id!r} belongs to run {actual_run_id!r}, "
            f"not run {expected_run_id!r}"
        )


class DatabaseBusyError(StorageError):
    """Raised when SQLite remains busy beyond the configured bounded wait."""

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        if timeout_seconds is None:
            message = "SQLite database is busy"
        else:
            message = f"SQLite database remained busy for {timeout_seconds:g} seconds"
        super().__init__(message)


class TransactionStateError(StorageError):
    """Raised for invalid transaction lifecycle or ownership operations."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"invalid transaction state: {detail}")
