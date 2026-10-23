"""Deterministic application services shared by CLI, UI, and later agent tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import ObservationTemplate, ScheduledIntervention
from shopfloor_pilot.domain.models import (
    EVENT_TYPE_PRIORITY,
    ApprovalDecision,
    ApprovalRecord,
    AttemptStatus,
    CandidateProposal,
    DomainCatalog,
    EventType,
    MachineStatus,
    Observation,
    OperationalSnapshot,
    OrderDelta,
    OrderDeltaKind,
    ResourceDelta,
    ResourceDeltaKind,
)
from shopfloor_pilot.events import (
    EventDraft,
    MachineDownPayload,
    OrderingConflictError,
    OrderRegisteredPayload,
    PlanAppliedPayload,
    ReplayError,
)
from shopfloor_pilot.scheduling import SchedulingResult, SolverConfig, solve_schedule
from shopfloor_pilot.simulation import AdvanceResult, SimulationRuntime
from shopfloor_pilot.storage import (
    AuditRecord,
    CrossRunReferenceError,
    DatabaseBusyError,
    IdempotencyConflictError,
    RecordConflictError,
    RunConflictError,
    RunNotFoundError,
    RunRecord,
    SQLiteStateRepository,
    VersionConflictError,
    WorkflowRecord,
    WorkflowStatus,
)
from shopfloor_pilot.validation import ValidationReport, ViolationCode, validate_candidate

from .errors import ApplicationServiceError, ServiceErrorCode
from .models import (
    PlanApplicationResult,
    PlanApplicationStatus,
    ProposalConflictReport,
    ServiceCapabilities,
)

if TYPE_CHECKING:
    from shopfloor_pilot.events import EventEnvelope, SnapshotCheckpoint
    from shopfloor_pilot.metrics import DecisionLatency, RunMetrics
    from shopfloor_pilot.reporting import (
        ProposalComparisonReport,
        ProposalReportPaths,
        ReplayBundle,
        ReplayBundleHeader,
        ReplayResult,
    )
    from shopfloor_pilot.storage import LifecycleExport

FailureInjector = Callable[[str], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validated_update[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")  # type: ignore[attr-defined]
    data.update(updates)
    return type(model).model_validate(data)


class ModelReadableApplicationServices:
    """Restricted facade suitable for later model-readable typed tools.

    It deliberately has no decision, application, reset, or intervention method.
    """

    __slots__ = ("_services",)

    def __init__(self, services: ApplicationServices) -> None:
        self._services = services

    def ensure_workflow(
        self,
        run_id: str,
        workflow_id: str,
        *,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Ensure RUNNING without accepting model-controlled status or payload."""

        return self._services.ensure_workflow(
            run_id,
            workflow_id,
            updated_at_utc=updated_at_utc,
        )

    def generate_proposal(
        self,
        run_id: str,
        workflow_id: str,
        proposal_id: str,
        *,
        proposed_order_deltas: Sequence[OrderDelta] = (),
        proposed_resource_deltas: Sequence[ResourceDelta] = (),
        not_before_seconds: int | None = None,
        config: SolverConfig | None = None,
        created_at_utc: str | None = None,
    ) -> SchedulingResult:
        return self._services.generate_proposal(
            run_id,
            workflow_id,
            proposal_id,
            proposed_order_deltas=proposed_order_deltas,
            proposed_resource_deltas=proposed_resource_deltas,
            not_before_seconds=not_before_seconds,
            config=config,
            created_at_utc=created_at_utc,
        )

    def save_validated_proposal(
        self,
        candidate: CandidateProposal,
        *,
        created_at_utc: str | None = None,
    ) -> CandidateProposal:
        return self._services.save_validated_proposal(
            candidate,
            created_at_utc=created_at_utc,
        )

    def inspect_conflict(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str | None = None,
    ) -> ProposalConflictReport:
        return self._services.inspect_conflict(
            run_id,
            proposal_id,
            expected_proposal_hash=expected_proposal_hash,
        )

    def get_snapshot(self, run_id: str) -> OperationalSnapshot:
        return self._services.get_snapshot(run_id)

    def get_checkpoint(
        self,
        run_id: str,
        state_version: int | None = None,
    ) -> SnapshotCheckpoint:
        return self._services.get_checkpoint(run_id, state_version=state_version)

    def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[EventEnvelope, ...]:
        return self._services.list_events(run_id, after_sequence=after_sequence)

    def export_lifecycle(self, run_id: str) -> LifecycleExport:
        return self._services.export_lifecycle(run_id)

    def calculate_run_metrics(self, run_id: str) -> RunMetrics:
        return self._services.calculate_run_metrics(run_id)

    def calculate_decision_latency(
        self,
        *,
        start_simulated_seconds: int,
        end_simulated_seconds: int,
        started_at_utc: str,
        completed_at_utc: str,
    ) -> DecisionLatency:
        return self._services.calculate_decision_latency(
            start_simulated_seconds=start_simulated_seconds,
            end_simulated_seconds=end_simulated_seconds,
            started_at_utc=started_at_utc,
            completed_at_utc=completed_at_utc,
        )

    def build_proposal_comparison(
        self,
        run_id: str,
        proposal_id: str,
        *,
        expected_proposal_hash: str | None = None,
        decision_latency: DecisionLatency | None = None,
    ) -> ProposalComparisonReport:
        return self._services.build_proposal_comparison(
            run_id,
            proposal_id,
            expected_proposal_hash=expected_proposal_hash,
            decision_latency=decision_latency,
        )


class ApplicationServices:
    """One deterministic service boundary over authoritative SQLite state."""

    _CAPABILITIES = ServiceCapabilities(
        model_readable_actions=(
            "ensure_workflow",
            "generate_proposal",
            "save_validated_proposal",
            "inspect_conflict",
            "get_snapshot",
            "get_checkpoint",
            "list_events",
            "export_lifecycle",
            "calculate_run_metrics",
            "calculate_decision_latency",
            "build_proposal_comparison",
        ),
        operator_actions=(
            "create_run",
            "reset_run",
            "record_human_decision",
            "apply_approved_plan",
            "advance_simulation",
            "mark_workflow_blocked",
            "cancel_workflow",
            "append_audit",
        ),
        batch_policy_actions=("record_deterministic_policy_decision",),
    )

    def __init__(
        self,
        repository: SQLiteStateRepository,
        catalog: DomainCatalog,
        seed: int = 0,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be an integer between 0 and 2**32 - 1")
        self.repository = repository
        self.catalog = catalog
        self.seed = seed

    @property
    def capabilities(self) -> ServiceCapabilities:
        return self._CAPABILITIES

    def for_model(self) -> ModelReadableApplicationServices:
        """Return the facade allowed for future model-readable typed tools."""

        return ModelReadableApplicationServices(self)

    def create_run(
        self,
        initial_snapshot: OperationalSnapshot,
        *,
        created_at_utc: str | None = None,
    ) -> RunRecord:
        return self.repository.create_run(initial_snapshot, created_at_utc=created_at_utc)

    def reset_run(
        self,
        source_run_id: str,
        new_initial_snapshot: OperationalSnapshot,
        *,
        created_at_utc: str | None = None,
    ) -> RunRecord:
        """Create a new run identity linked to the prior run without mutating it."""

        try:
            return self.repository.reset_run(
                source_run_id,
                new_initial_snapshot,
                created_at_utc=created_at_utc,
            )
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={
                    "source_run_id": source_run_id,
                    "new_run_id": new_initial_snapshot.run_id,
                },
            ) from exc
        except (RunNotFoundError, RunConflictError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def get_snapshot(self, run_id: str) -> OperationalSnapshot:
        """Read the current authoritative snapshot through the shared boundary."""

        try:
            return self.repository.get_snapshot(run_id)
        except (RunNotFoundError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def get_checkpoint(
        self,
        run_id: str,
        state_version: int | None = None,
    ) -> SnapshotCheckpoint:
        """Read a current or historical checkpoint without changing authority."""

        try:
            return self.repository.get_checkpoint(run_id, state_version=state_version)
        except (RunNotFoundError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[EventEnvelope, ...]:
        """Read the exact operational event suffix for one run."""

        try:
            return self.repository.list_events(run_id, after_sequence=after_sequence)
        except (RunNotFoundError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def export_lifecycle(self, run_id: str) -> LifecycleExport:
        """Read immutable proposal, approval, workflow, and audit evidence."""

        try:
            return self.repository.export_lifecycle(run_id)
        except (RunNotFoundError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def calculate_run_metrics(self, run_id: str) -> RunMetrics:
        """Replay one stored run into explicit actual/predicted/censored KPIs."""

        from shopfloor_pilot.metrics import calculate_run_metrics

        initial = self.get_checkpoint(run_id, state_version=0)
        return calculate_run_metrics(initial, self.list_events(run_id))

    @staticmethod
    def calculate_decision_latency(
        *,
        start_simulated_seconds: int,
        end_simulated_seconds: int,
        started_at_utc: str,
        completed_at_utc: str,
    ) -> DecisionLatency:
        """Keep wall-clock decision waiting separate from simulated progress."""

        from shopfloor_pilot.metrics import calculate_decision_latency

        return calculate_decision_latency(
            start_simulated_seconds=start_simulated_seconds,
            end_simulated_seconds=end_simulated_seconds,
            started_at_utc=started_at_utc,
            completed_at_utc=completed_at_utc,
        )

    def build_proposal_comparison(
        self,
        run_id: str,
        proposal_id: str,
        *,
        expected_proposal_hash: str | None = None,
        decision_latency: DecisionLatency | None = None,
    ) -> ProposalComparisonReport:
        """Build a prediction-only comparison for an exact stored proposal."""

        from shopfloor_pilot.reporting import build_proposal_comparison

        try:
            proposal = self.repository.get_proposal(run_id, proposal_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        if expected_proposal_hash is not None and proposal.proposal_hash != expected_proposal_hash:
            raise ApplicationServiceError(
                ServiceErrorCode.PROPOSAL_HASH_MISMATCH,
                "expected proposal hash does not match stored immutable content",
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "expected_proposal_hash": expected_proposal_hash,
                    "actual_proposal_hash": proposal.proposal_hash,
                },
            )
        current = self.get_checkpoint(run_id)
        initial = self.get_checkpoint(run_id, state_version=0)
        events = self.list_events(run_id)
        validation = validate_candidate(
            current.snapshot,
            self.catalog,
            proposal,
            projected_order_deltas=proposal.proposed_order_deltas,
            projected_resource_deltas=proposal.proposed_resource_deltas,
        )
        return build_proposal_comparison(
            initial,
            current,
            events,
            proposal,
            validation,
            decision_latency=decision_latency,
        )

    def export_proposal_reports(
        self,
        run_id: str,
        proposal_id: str,
        output_directory: str | Path,
        *,
        expected_proposal_hash: str | None = None,
        decision_latency: DecisionLatency | None = None,
    ) -> ProposalReportPaths:
        """Export before/after/comparison artifacts from one stored proposal."""

        from shopfloor_pilot.reporting import export_proposal_reports

        report = self.build_proposal_comparison(
            run_id,
            proposal_id,
            expected_proposal_hash=expected_proposal_hash,
            decision_latency=decision_latency,
        )
        return export_proposal_reports(output_directory, report)

    def export_replay_bundle(
        self,
        run_id: str,
        path: str | Path,
    ) -> ReplayBundleHeader:
        """Export a self-contained, hash-bound event replay bundle."""

        from shopfloor_pilot.reporting import export_replay_bundle

        initial = self.get_checkpoint(run_id, state_version=0)
        return export_replay_bundle(path, initial, self.list_events(run_id))

    @staticmethod
    def load_replay_bundle(path: str | Path) -> ReplayBundle:
        """Load and authenticate a portable bundle without repository state."""

        from shopfloor_pilot.reporting import load_replay_bundle

        return load_replay_bundle(path)

    @staticmethod
    def replay_bundle(bundle_or_path: ReplayBundle | str | Path) -> ReplayResult:
        """Purely replay a portable bundle without solving or random sampling."""

        from shopfloor_pilot.reporting import replay_bundle

        return replay_bundle(bundle_or_path)

    def advance_simulation(
        self,
        run_id: str,
        target_seconds: int,
        advance_id: str,
        *,
        interventions: Sequence[ScheduledIntervention] = (),
        observations: Sequence[Observation | ObservationTemplate] = (),
    ) -> AdvanceResult:
        """Execute explicit operator/harness inputs through the persisted runtime."""

        runtime = SimulationRuntime.rebuild(
            self.repository,
            run_id,
            self.catalog,
            seed=self.seed,
        )
        return runtime.advance_to(
            target_seconds,
            advance_id,
            interventions=interventions,
            observations=observations,
        )

    def ensure_workflow(
        self,
        run_id: str,
        workflow_id: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Atomically create RUNNING or return existing RUNNING unchanged."""

        try:
            return self.repository.ensure_running_workflow(
                run_id,
                workflow_id,
                payload=payload,
                updated_at_utc=updated_at_utc,
            )
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={"run_id": run_id, "workflow_id": workflow_id},
            ) from exc
        except (RunNotFoundError, CrossRunReferenceError) as exc:
            raise self._storage_error(exc) from exc
        except RecordConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.WORKFLOW_CONFLICT,
                str(exc),
                details={"run_id": run_id, "workflow_id": workflow_id},
            ) from exc

    def mark_workflow_blocked(
        self,
        run_id: str,
        workflow_id: str,
        *,
        reason_code: str,
        details: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Persist an explicit safe-blocked outcome without operational mutation.

        Schema-v1 lifecycle uses ``FAILED`` as the terminal status and carries the
        more precise ``outcome=blocked`` plus an actionable reason in the payload.
        Exact repeats are idempotent; conflicting terminal content is rejected.
        """

        normalized_reason = reason_code.strip()
        if not normalized_reason or normalized_reason != reason_code:
            raise ValueError("reason_code must be a non-empty trimmed string")
        payload_updates: dict[str, JsonValue] = {
            "outcome": "blocked",
            "reason_code": normalized_reason,
            "details": dict(details or {}),
        }
        try:
            return self.repository.transition_workflow(
                run_id,
                workflow_id,
                expected_status=WorkflowStatus.RUNNING,
                resulting_status=WorkflowStatus.FAILED,
                payload_updates=payload_updates,
                updated_at_utc=updated_at_utc,
            )
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={"run_id": run_id, "workflow_id": workflow_id},
            ) from exc
        except (RunNotFoundError, CrossRunReferenceError) as exc:
            raise self._storage_error(exc) from exc
        except RecordConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.WORKFLOW_CONFLICT,
                str(exc),
                details={
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "reason_code": normalized_reason,
                },
            ) from exc

    def cancel_workflow(
        self,
        run_id: str,
        workflow_id: str,
        *,
        reason_code: str,
        outcome: str = "cancelled",
        details: Mapping[str, JsonValue] | None = None,
        updated_at_utc: str | None = None,
    ) -> WorkflowRecord:
        """Close an open workflow as ``cancelled`` without operational mutation.

        ``running`` or ``waiting_approval`` becomes ``cancelled`` with ``outcome``,
        ``reason_code``, and ``details`` in the payload. Approved application
        compare-and-sets ``waiting_approval`` inside its own transaction, and proposal
        persistence refuses closed workflows, so a cancelled workflow can neither be
        applied nor revived. Exact repeats are idempotent; other terminals conflict.
        """

        for label, value in (("reason_code", reason_code), ("outcome", outcome)):
            if not isinstance(value, str) or not value.strip() or value.strip() != value:
                raise ValueError(f"{label} must be a non-empty trimmed string")
        payload_updates: dict[str, JsonValue] = {
            "outcome": outcome,
            "reason_code": reason_code,
            "details": dict(details or {}),
        }
        last_error: RecordConflictError | None = None
        # One retry covers a concurrent running -> waiting_approval binding between
        # the status read and the compare-and-set.
        for _attempt in range(2):
            try:
                current = self.repository.get_workflow(run_id, workflow_id)
                expected = (
                    current.status
                    if current.status in (WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL)
                    else WorkflowStatus.RUNNING
                )
                return self.repository.transition_workflow(
                    run_id,
                    workflow_id,
                    expected_status=expected,
                    resulting_status=WorkflowStatus.CANCELLED,
                    payload_updates=payload_updates,
                    updated_at_utc=updated_at_utc,
                )
            except DatabaseBusyError as exc:
                raise ApplicationServiceError(
                    ServiceErrorCode.DATABASE_BUSY,
                    str(exc),
                    details={"run_id": run_id, "workflow_id": workflow_id},
                ) from exc
            except (RunNotFoundError, CrossRunReferenceError) as exc:
                raise self._storage_error(exc) from exc
            except RecordConflictError as exc:
                last_error = exc
        assert last_error is not None
        raise ApplicationServiceError(
            ServiceErrorCode.WORKFLOW_CONFLICT,
            str(last_error),
            details={"run_id": run_id, "workflow_id": workflow_id, "reason_code": reason_code},
        ) from last_error

    def append_audit(self, record: AuditRecord, *, workflow_id: str | None = None) -> int:
        """Append run-scoped, non-operational audit evidence.

        The state version, checkpoint, and event stream are unchanged. An exact
        repeat is idempotent; reusing an audit ID for other content conflicts.
        """

        try:
            return self.repository.append_audit(record, workflow_id=workflow_id)
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={"run_id": record.run_id, "audit_id": record.audit_id},
            ) from exc
        except (RunNotFoundError, CrossRunReferenceError) as exc:
            raise self._storage_error(exc) from exc
        except RecordConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.STORAGE_CONFLICT,
                str(exc),
                details={"run_id": record.run_id, "audit_id": record.audit_id},
            ) from exc

    def list_reset_successors(self, run_id: str) -> tuple[str, ...]:
        """Run IDs created by ``reset_run`` from ``run_id`` (read-only lineage)."""

        try:
            return self.repository.list_reset_successors(run_id)
        except RunNotFoundError as exc:
            raise self._storage_error(exc) from exc

    def generate_proposal(
        self,
        run_id: str,
        workflow_id: str,
        proposal_id: str,
        *,
        proposed_order_deltas: Sequence[OrderDelta] = (),
        proposed_resource_deltas: Sequence[ResourceDelta] = (),
        not_before_seconds: int | None = None,
        config: SolverConfig | None = None,
        created_at_utc: str | None = None,
    ) -> SchedulingResult:
        try:
            snapshot = self.repository.get_snapshot(run_id)
            workflow = self.repository.get_workflow(run_id, workflow_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        self._require_workflow_open_for_proposal(workflow)

        result = solve_schedule(
            snapshot,
            self.catalog,
            proposal_id=proposal_id,
            workflow_id=workflow_id,
            proposed_order_deltas=tuple(proposed_order_deltas),
            proposed_resource_deltas=tuple(proposed_resource_deltas),
            not_before_seconds=not_before_seconds,
            config=config,
        )
        if result.candidate is None:
            return result

        candidate = result.candidate
        if created_at_utc is not None:
            candidate = _validated_update(candidate, created_at_utc=created_at_utc)
            result = result.model_copy(update={"candidate": candidate})
        persisted = self._save_proposal_for_approval(
            workflow,
            candidate,
            updated_at_utc=created_at_utc,
        )
        if persisted != candidate:
            result = result.model_copy(update={"candidate": persisted})
        return result

    def save_validated_proposal(
        self,
        candidate: CandidateProposal,
        *,
        created_at_utc: str | None = None,
    ) -> CandidateProposal:
        """Persist externally assembled deterministic output only after full validation."""

        try:
            current = self.repository.get_snapshot(candidate.run_id)
            workflow = self.repository.get_workflow(candidate.run_id, candidate.workflow_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        self._require_workflow_open_for_proposal(workflow)
        validation = validate_candidate(
            current,
            self.catalog,
            candidate,
            projected_order_deltas=candidate.proposed_order_deltas,
            projected_resource_deltas=candidate.proposed_resource_deltas,
        )
        if not validation.is_valid:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_CANDIDATE,
                "candidate failed independent validation and was not persisted",
                details={
                    "run_id": candidate.run_id,
                    "proposal_id": candidate.proposal_id,
                    "violation_codes": [item.code.value for item in validation.violations],
                },
                validation=validation,
            )
        stored = (
            _validated_update(candidate, created_at_utc=created_at_utc)
            if created_at_utc is not None
            else candidate
        )
        return self._save_proposal_for_approval(
            workflow,
            stored,
            updated_at_utc=created_at_utc,
        )

    @staticmethod
    def _require_workflow_open_for_proposal(workflow: WorkflowRecord) -> None:
        if workflow.status in (WorkflowStatus.RUNNING, WorkflowStatus.WAITING_APPROVAL):
            return
        raise ApplicationServiceError(
            ServiceErrorCode.WORKFLOW_CONFLICT,
            "proposal generation cannot revive a closed workflow",
            details={
                "run_id": workflow.run_id,
                "workflow_id": workflow.workflow_id,
                "current_status": workflow.status.value,
            },
        )

    def _save_proposal_for_approval(
        self,
        workflow: WorkflowRecord,
        candidate: CandidateProposal,
        *,
        updated_at_utc: str | None,
    ) -> CandidateProposal:
        binding: dict[str, JsonValue] = {
            "pending_proposal_id": candidate.proposal_id,
            "pending_proposal_hash": candidate.proposal_hash,
        }
        if workflow.status is WorkflowStatus.WAITING_APPROVAL and not all(
            workflow.payload.get(key) == value for key, value in binding.items()
        ):
            raise ApplicationServiceError(
                ServiceErrorCode.WORKFLOW_CONFLICT,
                "waiting workflow is already bound to a different proposal",
                details={
                    "run_id": candidate.run_id,
                    "workflow_id": candidate.workflow_id,
                    "proposal_id": candidate.proposal_id,
                    "current_status": workflow.status.value,
                },
            )
        try:
            return self.repository.save_proposal_and_transition_workflow(
                candidate,
                expected_workflow_status=WorkflowStatus.RUNNING,
                resulting_workflow_status=WorkflowStatus.WAITING_APPROVAL,
                workflow_payload_updates=binding,
                updated_at_utc=updated_at_utc,
            )
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={
                    "run_id": candidate.run_id,
                    "workflow_id": candidate.workflow_id,
                    "proposal_id": candidate.proposal_id,
                },
            ) from exc
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    def record_human_decision(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
        *,
        approval_id: str,
        decision: ApprovalDecision,
        actor: str,
        reason: str | None = None,
        decided_at_utc: str | None = None,
    ) -> ApprovalRecord:
        actor_name = actor.strip()
        if not actor_name or actor_name.casefold().startswith(("policy:", "model:")):
            raise ValueError("human decision actor must identify a human operator")
        if not actor_name.startswith("human:"):
            actor_name = f"human:{actor_name}"
        return self._record_decision(
            run_id,
            proposal_id,
            expected_proposal_hash,
            approval_id=approval_id,
            decision=decision,
            actor=actor_name,
            reason=reason,
            decided_at_utc=decided_at_utc,
        )

    def record_deterministic_policy_decision(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
        *,
        approval_id: str,
        policy_name: str,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        reason: str | None = None,
        decided_at_utc: str | None = None,
    ) -> ApprovalRecord:
        name = policy_name.strip()
        if not name:
            raise ValueError("deterministic approval policy_name must be non-empty")
        return self._record_decision(
            run_id,
            proposal_id,
            expected_proposal_hash,
            approval_id=approval_id,
            decision=decision,
            actor=f"policy:{name}",
            reason=reason,
            decided_at_utc=decided_at_utc,
        )

    def inspect_conflict(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str | None = None,
    ) -> ProposalConflictReport:
        try:
            proposal = self.repository.get_proposal(run_id, proposal_id)
            current = self.repository.get_checkpoint(run_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

        validation = validate_candidate(
            current.snapshot,
            self.catalog,
            proposal,
            projected_order_deltas=proposal.proposed_order_deltas,
            projected_resource_deltas=proposal.proposed_resource_deltas,
        )
        reasons: list[ServiceErrorCode] = []
        if expected_proposal_hash is not None and expected_proposal_hash != proposal.proposal_hash:
            reasons.append(ServiceErrorCode.PROPOSAL_HASH_MISMATCH)
        violation_codes = {item.code for item in validation.violations}
        if ViolationCode.RUN_ID_MISMATCH in violation_codes:
            reasons.append(ServiceErrorCode.WRONG_RUN)
        if violation_codes & {
            ViolationCode.BASE_STATE_VERSION_MISMATCH,
            ViolationCode.BASE_SNAPSHOT_HASH_MISMATCH,
            ViolationCode.PLAN_REVISION_MISMATCH,
        }:
            reasons.append(ServiceErrorCode.STALE_STATE)
        remaining = violation_codes - {
            ViolationCode.RUN_ID_MISMATCH,
            ViolationCode.BASE_STATE_VERSION_MISMATCH,
            ViolationCode.BASE_SNAPSHOT_HASH_MISMATCH,
            ViolationCode.PLAN_REVISION_MISMATCH,
        }
        if remaining:
            reasons.append(ServiceErrorCode.INVALID_CANDIDATE)
        ordered_reasons = tuple(dict.fromkeys(reasons))
        return ProposalConflictReport(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            expected_proposal_hash=expected_proposal_hash,
            base_state_version=proposal.base_state_version,
            current_state_version=current.snapshot.state_version,
            base_snapshot_hash=proposal.base_snapshot_hash,
            current_snapshot_hash=current.snapshot_hash,
            can_apply=not ordered_reasons and validation.is_valid,
            reasons=ordered_reasons,
            validation=validation,
        )

    def apply_approved_plan(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
        approval_id: str,
        idempotency_key: str,
        effective_time_seconds: int | None = None,
        _failure_injector: FailureInjector | None = None,
    ) -> PlanApplicationResult:
        """Revalidate and atomically apply one exact stored approved proposal.

        The proposal's historical checkpoint is intentionally used to rebuild an
        identical request on retry.  A new key still performs the repository's live
        compare-and-swap and therefore cannot apply against a newer state.
        """

        proposal = self._proposal(run_id, proposal_id, expected_proposal_hash)
        approval = self._approval(run_id, approval_id)
        self._require_approval_binding(proposal, approval)

        try:
            base = self.repository.get_checkpoint(
                run_id,
                state_version=proposal.base_state_version,
            )
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        validation = validate_candidate(
            base.snapshot,
            self.catalog,
            proposal,
            projected_order_deltas=proposal.proposed_order_deltas,
            projected_resource_deltas=proposal.proposed_resource_deltas,
        )
        if not validation.is_valid:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_CANDIDATE,
                "stored proposal failed independent validation",
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "violation_codes": [item.code.value for item in validation.violations],
                },
                validation=validation,
            )

        effective = self._effective_time(base, proposal, effective_time_seconds)
        events = self._application_events(proposal, validation, effective)
        expected_snapshot = self._expected_result_snapshot(base.snapshot, proposal, validation)
        expected_result_hash = canonical_hash(expected_snapshot)
        recorded_at = _utc_now()
        audit = AuditRecord(
            audit_id=self._application_audit_id(run_id, idempotency_key),
            run_id=run_id,
            category="approval",
            action="approved_plan_applied",
            actor=approval.actor,
            details={
                "approval_id": approval.approval_id,
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.proposal_hash,
                "base_snapshot_hash": proposal.base_snapshot_hash,
                "order_delta_hash": canonical_hash(proposal.proposed_order_deltas),
                "resource_delta_hash": canonical_hash(proposal.proposed_resource_deltas),
                "result_snapshot_hash": expected_result_hash,
            },
            simulated_time_seconds=effective,
            recorded_at_utc=recorded_at,
        )
        command_payload: dict[str, JsonValue] = {
            "schema_version": 1,
            "approval_id": approval.approval_id,
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
            "workflow_id": proposal.workflow_id,
            "base_state_version": proposal.base_state_version,
            "base_snapshot_hash": proposal.base_snapshot_hash,
            "effective_time_seconds": effective,
            "expected_result_snapshot_hash": expected_result_hash,
            "audit": audit.model_dump(mode="json"),
            "catalog_hash": canonical_hash(self.catalog),
            "seed": self.seed,
        }

        def consume_and_audit(transaction: Any) -> None:
            transaction.consume_approved_proposal(
                approval.approval_id,
                expected_proposal_id=proposal.proposal_id,
                expected_proposal_hash=proposal.proposal_hash,
                expected_result_snapshot_hash=expected_result_hash,
                consumed_at_utc=recorded_at,
            )
            transaction.append_audit(audit, workflow_id=proposal.workflow_id)
            transaction.transition_workflow(
                proposal.workflow_id,
                expected_status=WorkflowStatus.WAITING_APPROVAL,
                resulting_status=WorkflowStatus.COMPLETED,
                payload_updates={
                    "outcome": "applied",
                    "applied_proposal_id": proposal.proposal_id,
                    "approval_id": approval.approval_id,
                },
                updated_at_utc=recorded_at,
            )

        try:
            mutation = self.repository.apply_mutation(
                run_id=run_id,
                expected_state_version=proposal.base_state_version,
                idempotency_key=idempotency_key,
                command_name="apply_approved_plan",
                command_payload=command_payload,
                events=events,
                participant=consume_and_audit,
                _failure_injector=_failure_injector,
                created_at_utc=recorded_at,
            )
        except VersionConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.STALE_STATE,
                str(exc),
                details={
                    "run_id": run_id,
                    "expected_state_version": exc.expected,
                    "actual_state_version": exc.actual,
                },
            ) from exc
        except IdempotencyConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.IDEMPOTENCY_CONFLICT,
                str(exc),
                details={"run_id": run_id, "idempotency_key": idempotency_key},
            ) from exc
        except DatabaseBusyError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.DATABASE_BUSY,
                str(exc),
                details={"run_id": run_id},
            ) from exc
        except (OrderingConflictError, ReplayError) as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.BOUNDARY_CLOSED,
                str(exc),
                details={"run_id": run_id, "effective_time_seconds": effective},
            ) from exc
        except (CrossRunReferenceError, RunNotFoundError) as exc:
            raise self._storage_error(exc) from exc
        except RecordConflictError as exc:
            code = (
                ServiceErrorCode.WORKFLOW_CONFLICT
                if exc.record_type == "workflow"
                else ServiceErrorCode.APPROVAL_BINDING_MISMATCH
            )
            raise ApplicationServiceError(
                code,
                str(exc),
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "approval_id": approval_id,
                },
            ) from exc

        return PlanApplicationResult(
            status=(
                PlanApplicationStatus.IDEMPOTENT
                if mutation.replayed_idempotent
                else PlanApplicationStatus.APPLIED
            ),
            run_id=run_id,
            workflow_id=proposal.workflow_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            approval_id=approval.approval_id,
            effective_time_seconds=effective,
            validation=validation,
            mutation=mutation,
        )

    def preview_application(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
    ) -> tuple[OperationalSnapshot, ValidationReport]:
        """Read-only projection of the snapshot one exact stored proposal would produce.

        The projection is computed from the proposal's historical base checkpoint,
        exactly as approved application would compute it. Nothing is written, and
        the result is a prediction for review, not authority.
        """

        proposal = self._proposal(run_id, proposal_id, expected_proposal_hash)
        try:
            base = self.repository.get_checkpoint(run_id, state_version=proposal.base_state_version)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        validation = validate_candidate(
            base.snapshot,
            self.catalog,
            proposal,
            projected_order_deltas=proposal.proposed_order_deltas,
            projected_resource_deltas=proposal.proposed_resource_deltas,
        )
        if not validation.is_valid:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_CANDIDATE,
                "stored proposal failed independent validation",
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "violation_codes": [item.code.value for item in validation.violations],
                },
                validation=validation,
            )
        return self._expected_result_snapshot(base.snapshot, proposal, validation), validation

    def _record_decision(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
        *,
        approval_id: str,
        decision: ApprovalDecision,
        actor: str,
        reason: str | None,
        decided_at_utc: str | None,
    ) -> ApprovalRecord:
        proposal = self._proposal(run_id, proposal_id, expected_proposal_hash)
        requested_time = decided_at_utc
        if requested_time is None:
            try:
                existing = self.repository.get_approval(run_id, approval_id)
            except RecordConflictError:
                existing = None
            except (RunNotFoundError, CrossRunReferenceError) as exc:
                raise self._storage_error(exc) from exc
            if existing is not None:
                expected = {
                    "proposal_id": proposal.proposal_id,
                    "proposal_hash": proposal.proposal_hash,
                    "workflow_id": proposal.workflow_id,
                    "base_state_version": proposal.base_state_version,
                    "decision": decision,
                    "actor": actor,
                    "reason": reason,
                }
                actual = existing.model_dump(mode="python", include=set(expected))
                if actual == expected:
                    return existing
                raise ApplicationServiceError(
                    ServiceErrorCode.APPROVAL_BINDING_MISMATCH,
                    "approval_id is already bound to different decision content",
                    details={"run_id": run_id, "approval_id": approval_id},
                )
        approval = ApprovalRecord(
            schema_version=1,
            approval_id=approval_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            run_id=run_id,
            workflow_id=proposal.workflow_id,
            base_state_version=proposal.base_state_version,
            decision=decision,
            actor=actor,
            decided_at_utc=requested_time or _utc_now(),
            reason=reason,
        )
        try:
            return self.repository.save_approval(approval)
        except (RunNotFoundError, CrossRunReferenceError) as exc:
            raise self._storage_error(exc) from exc
        except RecordConflictError as exc:
            raise ApplicationServiceError(
                ServiceErrorCode.APPROVAL_BINDING_MISMATCH,
                str(exc),
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "approval_id": approval_id,
                },
            ) from exc

    def _proposal(
        self,
        run_id: str,
        proposal_id: str,
        expected_proposal_hash: str,
    ) -> CandidateProposal:
        try:
            proposal = self.repository.get_proposal(run_id, proposal_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc
        if proposal.proposal_hash != expected_proposal_hash:
            raise ApplicationServiceError(
                ServiceErrorCode.PROPOSAL_HASH_MISMATCH,
                "expected proposal hash does not match stored immutable content",
                details={
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "expected_proposal_hash": expected_proposal_hash,
                    "actual_proposal_hash": proposal.proposal_hash,
                },
            )
        return proposal

    def _approval(self, run_id: str, approval_id: str) -> ApprovalRecord:
        try:
            return self.repository.get_approval(run_id, approval_id)
        except (RunNotFoundError, CrossRunReferenceError, RecordConflictError) as exc:
            raise self._storage_error(exc) from exc

    @staticmethod
    def _require_approval_binding(
        proposal: CandidateProposal,
        approval: ApprovalRecord,
    ) -> None:
        if approval.decision is not ApprovalDecision.APPROVED:
            raise ApplicationServiceError(
                ServiceErrorCode.APPROVAL_REJECTED,
                "proposal does not have an approving decision",
                details={
                    "proposal_id": proposal.proposal_id,
                    "approval_id": approval.approval_id,
                    "decision": approval.decision.value,
                },
            )
        expected = (
            proposal.run_id,
            proposal.workflow_id,
            proposal.proposal_id,
            proposal.proposal_hash,
            proposal.base_state_version,
        )
        actual = (
            approval.run_id,
            approval.workflow_id,
            approval.proposal_id,
            approval.proposal_hash,
            approval.base_state_version,
        )
        if actual != expected:
            raise ApplicationServiceError(
                ServiceErrorCode.APPROVAL_BINDING_MISMATCH,
                "approval does not bind the selected stored proposal exactly",
                details={
                    "proposal_id": proposal.proposal_id,
                    "proposal_hash": proposal.proposal_hash,
                    "approval_id": approval.approval_id,
                    "approved_proposal_id": approval.proposal_id,
                    "approved_proposal_hash": approval.proposal_hash,
                },
            )

    @staticmethod
    def _effective_time(
        base: Any,
        proposal: CandidateProposal,
        requested: int | None,
    ) -> int:
        snapshot = base.snapshot
        now = snapshot.simulated_time_seconds
        event_types = [EventType.PLAN_APPLIED]
        if proposal.proposed_order_deltas:
            event_types.append(EventType.ORDER_REGISTERED)
        if proposal.proposed_resource_deltas:
            event_types.append(EventType.MACHINE_DOWN)
        first_priority = min(EVENT_TYPE_PRIORITY[item] for item in event_types)
        last_key = base.last_ordering_key

        if last_key is not None and last_key[0] > now:
            raise ApplicationServiceError(
                ServiceErrorCode.BOUNDARY_CLOSED,
                "a future boundary is already staged; advance through it before replanning",
                details={"current_time_seconds": now, "last_event_time_seconds": last_key[0]},
            )
        current_is_open = (
            last_key is None
            or (
                now,
                first_priority,
                base.last_sequence + 1,
            )
            > last_key
        )
        effective = now if requested is None and current_is_open else now + 1
        if requested is not None:
            if isinstance(requested, bool) or not isinstance(requested, int):
                raise ApplicationServiceError(
                    ServiceErrorCode.INVALID_EFFECTIVE_TIME,
                    "effective_time_seconds must be a strict integer",
                )
            effective = requested
        if effective not in (now, now + 1) or effective > snapshot.hard_horizon_seconds:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_EFFECTIVE_TIME,
                "plan application must use the current or immediately following boundary",
                details={
                    "current_time_seconds": now,
                    "effective_time_seconds": effective,
                    "hard_horizon_seconds": snapshot.hard_horizon_seconds,
                },
            )
        first_key = (effective, first_priority, base.last_sequence + 1)
        if last_key is not None and first_key <= last_key:
            raise ApplicationServiceError(
                ServiceErrorCode.BOUNDARY_CLOSED,
                "the requested event priority is already closed at this boundary",
                details={
                    "effective_time_seconds": effective,
                    "first_event_priority": first_priority,
                    "last_event_priority": last_key[1],
                },
            )
        if effective > now and proposal.proposed_resource_deltas:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_EFFECTIVE_TIME,
                "resource mark-down deltas must apply at their current snapshot boundary",
                details={"current_time_seconds": now, "effective_time_seconds": effective},
            )
        earlier_assignments = tuple(
            sorted(
                item.attempt_id
                for item in proposal.assignments
                if item.window.start_seconds < effective
            )
        )
        if earlier_assignments:
            raise ApplicationServiceError(
                ServiceErrorCode.INVALID_EFFECTIVE_TIME,
                "proposal contains assignments before its application boundary",
                details={"attempt_ids": list(earlier_assignments)},
            )
        if effective > now:
            due_completions = tuple(
                sorted(
                    attempt.attempt_id
                    for attempt in snapshot.attempts
                    if attempt.status is AttemptStatus.RUNNING
                    and attempt.actual_start_seconds is not None
                    and attempt.actual_start_seconds + attempt.planned_duration_seconds <= effective
                )
            )
            if due_completions:
                raise ApplicationServiceError(
                    ServiceErrorCode.BOUNDARY_CLOSED,
                    "future application would skip a running completion boundary",
                    details={"attempt_ids": list(due_completions)},
                )
        return effective

    @staticmethod
    def _application_events(
        proposal: CandidateProposal,
        validation: ValidationReport,
        effective: int,
    ) -> tuple[EventDraft, ...]:
        drafts: list[EventDraft] = []
        for delta in sorted(
            proposal.proposed_resource_deltas,
            key=lambda item: (item.machine_id, item.kind.value),
        ):
            if delta.kind is not ResourceDeltaKind.MARK_DOWN:
                raise AssertionError("validated application contains unsupported resource delta")
            drafts.append(
                EventDraft(
                    simulated_time_seconds=effective,
                    event_type=EventType.MACHINE_DOWN,
                    payload=MachineDownPayload(
                        machine_id=delta.machine_id,
                        status="down",
                    ).model_dump(mode="json"),
                )
            )
        for delta in sorted(
            proposal.proposed_order_deltas,
            key=lambda item: (item.order.order_id, item.kind.value),
        ):
            if delta.kind is not OrderDeltaKind.ADD:
                raise AssertionError("validated application contains unsupported order delta")
            drafts.append(
                EventDraft(
                    simulated_time_seconds=effective,
                    event_type=EventType.ORDER_REGISTERED,
                    payload=OrderRegisteredPayload(order=delta.order).model_dump(mode="json"),
                )
            )
        drafts.append(
            EventDraft(
                simulated_time_seconds=effective,
                event_type=EventType.PLAN_APPLIED,
                payload=PlanAppliedPayload(
                    plan_revision=proposal.proposed_plan_revision,
                    operations=validation.projected_operations,
                    attempts=validation.projected_attempts,
                ).model_dump(mode="json"),
            )
        )
        return tuple(drafts)

    @staticmethod
    def _expected_result_snapshot(
        base: OperationalSnapshot,
        proposal: CandidateProposal,
        validation: ValidationReport,
    ) -> OperationalSnapshot:
        machines = list(base.machines)
        machine_indexes = {item.machine_id: index for index, item in enumerate(machines)}
        for delta in sorted(
            proposal.proposed_resource_deltas,
            key=lambda item: (item.machine_id, item.kind.value),
        ):
            index = machine_indexes[delta.machine_id]
            machines[index] = _validated_update(machines[index], status=MachineStatus.DOWN)
        orders = [*base.orders]
        orders.extend(
            delta.order
            for delta in sorted(
                proposal.proposed_order_deltas,
                key=lambda item: (item.order.order_id, item.kind.value),
            )
        )
        return _validated_update(
            base,
            state_version=base.state_version + 1,
            plan_revision=proposal.proposed_plan_revision,
            machines=tuple(machines),
            orders=tuple(orders),
            operations=validation.projected_operations,
            attempts=validation.projected_attempts,
        )

    @staticmethod
    def _application_audit_id(run_id: str, idempotency_key: str) -> str:
        digest = canonical_hash(
            {
                "namespace": "shopfloor-pilot-approved-apply-audit-v1",
                "run_id": run_id,
                "idempotency_key": idempotency_key,
            }
        )
        return f"audit-approved-apply-{digest[:40]}"

    @staticmethod
    def _storage_error(error: Exception) -> ApplicationServiceError:
        if isinstance(error, CrossRunReferenceError):
            return ApplicationServiceError(
                ServiceErrorCode.WRONG_RUN,
                str(error),
                details={
                    "record_type": error.record_type,
                    "record_id": error.record_id,
                    "expected_run_id": error.expected_run_id,
                    "actual_run_id": error.actual_run_id,
                },
            )
        if isinstance(error, RunNotFoundError):
            return ApplicationServiceError(
                ServiceErrorCode.RUN_NOT_FOUND,
                str(error),
                details={"run_id": error.run_id},
            )
        if isinstance(error, RecordConflictError):
            code = {
                "proposal": ServiceErrorCode.PROPOSAL_NOT_FOUND,
                "approval": ServiceErrorCode.APPROVAL_NOT_FOUND,
                "workflow": ServiceErrorCode.WORKFLOW_CONFLICT,
            }.get(error.record_type, ServiceErrorCode.STORAGE_CONFLICT)
            return ApplicationServiceError(
                code,
                str(error),
                details={
                    "record_type": error.record_type,
                    "record_id": error.record_id,
                    "detail": error.detail,
                },
            )
        return ApplicationServiceError(ServiceErrorCode.STORAGE_CONFLICT, str(error))
