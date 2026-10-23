"""Typed failures for deterministic event materialization and replay."""

from __future__ import annotations


class ReplayError(ValueError):
    """Base class for a replay stream that cannot be accepted completely."""

    code = "replay_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"{self.code}: {message}")


class UnsupportedSchemaError(ReplayError):
    """An envelope, event, payload, or checkpoint uses an unsupported schema."""

    code = "unsupported_schema"


class MalformedPayloadError(ReplayError):
    """An event payload does not match the schema for its event type."""

    code = "malformed_payload"


class MalformedEnvelopeError(ReplayError):
    """An event draft or persisted envelope is structurally malformed."""

    code = "malformed_envelope"


class SequenceConflictError(ReplayError):
    """A required event sequence is absent, duplicated, or non-contiguous."""

    code = "sequence_conflict"


class OrderingConflictError(ReplayError):
    """Events violate simulated-time, priority, and sequence ordering."""

    code = "ordering_conflict"


class EventConflictError(ReplayError):
    """An event identity or run binding conflicts with the accepted stream."""

    code = "event_conflict"


class GroupVersionConflictError(ReplayError):
    """An event group is incomplete or has an invalid version boundary."""

    code = "group_version_conflict"


class ReductionConflictError(ReplayError):
    """A validly shaped event cannot be applied to the current state."""

    code = "reduction_conflict"
