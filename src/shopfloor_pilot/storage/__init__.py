"""Public authoritative SQLite storage contracts and repository."""

from .errors import (
    CrossRunReferenceError,
    DatabaseBusyError,
    DatabaseNotInitializedError,
    IdempotencyConflictError,
    RecordConflictError,
    RunConflictError,
    RunNotFoundError,
    SchemaVersionError,
    StorageError,
    TransactionStateError,
    VersionConflictError,
)
from .models import (
    ApprovalConsumptionRecord,
    AuditRecord,
    LifecycleExport,
    MutationResult,
    RestoredRunState,
    RunRecord,
    WorkflowRecord,
    WorkflowStatus,
)
from .repository import OperationalTransaction, SQLiteStateRepository
from .schema import CURRENT_SCHEMA_VERSION

__all__ = [
    "ApprovalConsumptionRecord",
    "AuditRecord",
    "CrossRunReferenceError",
    "CURRENT_SCHEMA_VERSION",
    "DatabaseBusyError",
    "DatabaseNotInitializedError",
    "IdempotencyConflictError",
    "LifecycleExport",
    "MutationResult",
    "OperationalTransaction",
    "RecordConflictError",
    "RestoredRunState",
    "RunConflictError",
    "RunNotFoundError",
    "RunRecord",
    "SchemaVersionError",
    "SQLiteStateRepository",
    "StorageError",
    "TransactionStateError",
    "VersionConflictError",
    "WorkflowRecord",
    "WorkflowStatus",
]
