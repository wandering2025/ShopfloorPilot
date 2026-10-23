"""Deterministic, explicitly advanced SimPy projection over authoritative SQLite state.

SimPy owns only callback timing.  SQLite events and checkpoints remain authoritative;
every callback rechecks persisted attempt status and its exact scheduled window before
it may emit a fact.  Live generator objects are therefore disposable and are rebuilt
from SCHEDULED/RUNNING attempts after a process restart.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import simpy
from pydantic import TypeAdapter

from shopfloor_pilot.domain.base import Identifier
from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.fixtures import (
    InterventionKind,
    ObservationTemplate,
    ScheduledIntervention,
)
from shopfloor_pilot.domain.models import (
    EVENT_TYPE_PRIORITY,
    AttemptStatus,
    CandidateProposal,
    DomainCatalog,
    DomainEvent,
    EventType,
    MachineStatus,
    Observation,
    OperationalSnapshot,
    OperationAttempt,
    OperationStatus,
    OrderStatus,
    TelemetryValue,
)
from shopfloor_pilot.events import (
    EventDraft,
    MachineDownPayload,
    MachineRestoredPayload,
    ObservationRecordedPayload,
    OperationCompletedPayload,
    OperationInterruptedPayload,
    OperationStartedPayload,
    OrderRegisteredPayload,
    PlanAppliedPayload,
    TimeAdvancedPayload,
)
from shopfloor_pilot.storage import (
    AuditRecord,
    IdempotencyConflictError,
    MutationResult,
    SQLiteStateRepository,
    VersionConflictError,
)
from shopfloor_pilot.validation import ValidationReport, validate_candidate

from .errors import (
    BoundaryClosedError,
    CommandConflictError,
    ConcurrentRuntimeError,
    InvalidAdvanceError,
    InvalidPlanError,
    PastInjectionError,
    RuntimeStateError,
    StalePlanError,
    UnsupportedPlanDeltaError,
    has_any_violation,
)
from .models import (
    ActivationResult,
    AdvanceResult,
    BlockedAttempt,
    BlockedReason,
    CommandStatus,
    PublicRuntimeEvent,
    RuntimeProjection,
)

_IDENTIFIER_ADAPTER = TypeAdapter(Identifier)
_STALE_CODES = (
    "run_id_mismatch",
    "base_state_version_mismatch",
    "base_snapshot_hash_mismatch",
    "plan_revision_mismatch",
)
_SIMULATION_AUDIT_CATEGORY = "simulation_runtime"
_ADVANCE_STARTED = "advance_started"
_ADVANCE_COMPLETED = "advance_completed"
_INTERVENTION_APPLIED = "intervention_applied"
_AUDIT_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# Explicit observations cross an evaluator-to-runtime trust boundary.  Keep the
# accepted signal vocabulary finite and unit-bound rather than persisting arbitrary
# evaluator dictionaries.  The short aliases make hand-built test observations easy.
_PUBLIC_SIGNAL_UNITS: dict[str, frozenset[str]] = {
    "temperature": frozenset({"mdegC"}),
    "temperature_millicelsius": frozenset({"mdegC"}),
    "vibration": frozenset({"um_per_s"}),
    "vibration_micrometers_per_second": frozenset({"um_per_s"}),
    "drive_current_milliamps": frozenset({"mA"}),
    "lubrication_pressure_kilopascals": frozenset({"kPa"}),
    "queue_depth_units": frozenset({"units"}),
}


@dataclass(frozen=True, order=True)
class _StartCallback:
    attempt_id: str
    operation_id: str
    machine_id: str
    start_seconds: int
    end_seconds: int


@dataclass(frozen=True, order=True)
class _CompletionCallback:
    attempt_id: str
    operation_id: str
    machine_id: str
    actual_start_seconds: int
    completion_seconds: int
    scheduled_start_seconds: int | None
    scheduled_end_seconds: int | None


@dataclass
class _AdvanceContext:
    advance_id: str
    target_seconds: int
    command_fingerprint: str
    mutations: list[MutationResult] = field(default_factory=list)
    start_audit_pending: bool = False
    completion_audit_pending: bool = True


def _model_update[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")  # type: ignore[attr-defined]
    data.update(updates)
    return type(model).model_validate(data)


def _draft(at_seconds: int, event_type: EventType, payload: Any) -> EventDraft:
    return EventDraft(
        simulated_time_seconds=at_seconds,
        event_type=event_type,
        payload=payload.model_dump(mode="json"),
    )


def _stable_identifier(prefix: str, value: Any, *, digest_length: int = 40) -> str:
    digest = hashlib.sha256(
        canonical_hash(value).encode("ascii"),
    ).hexdigest()[:digest_length]
    return f"{prefix}{digest}"


def _audit_timestamp(at_seconds: int) -> str:
    return (_AUDIT_EPOCH + timedelta(seconds=at_seconds)).isoformat(timespec="seconds")


def _public_intervention_data(intervention: ScheduledIntervention) -> dict[str, Any]:
    """Return only information known at the operational boundary.

    ``private_truth_ref`` and ``unavailable_until_seconds`` are intentionally not read
    into this structure, command hashes, audits, events, or projections.
    """

    return {
        "intervention_id": intervention.intervention_id,
        "at_seconds": intervention.at_seconds,
        "kind": intervention.kind.value,
        "target_machine_id": intervention.target_machine_id,
        "injected_status": (
            intervention.injected_status.value if intervention.injected_status is not None else None
        ),
        "order_to_add": (
            intervention.order_to_add.model_dump(mode="json")
            if intervention.order_to_add is not None
            else None
        ),
    }


def _event_observation(event: DomainEvent) -> Observation | None:
    if event.event_type is not EventType.OBSERVATION_RECORDED:
        return None
    return _json_payload(ObservationRecordedPayload, event.payload).observation


def _attempt_fits_latest_calendar(
    snapshot: OperationalSnapshot,
    attempt: OperationAttempt,
) -> bool:
    """Check the complete persisted reservation against the latest machine calendar."""

    window = attempt.scheduled_window
    if window is None:
        return False
    machine = next(
        (item for item in snapshot.machines if item.machine_id == attempt.machine_id),
        None,
    )
    if machine is None:
        return False
    return any(
        available.start_seconds <= window.start_seconds
        and window.end_seconds <= available.end_seconds
        for available in machine.availability_windows
    )


def _json_payload[ModelT](model_type: type[ModelT], payload: Any) -> ModelT:
    """Validate persisted JSON-shaped payloads under strict model semantics."""

    return model_type.model_validate_json(  # type: ignore[attr-defined,no-any-return]
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


class SimulationRuntime:
    """CPU-only deterministic execution runtime for one persisted manufacturing run."""

    def __init__(
        self,
        repository: SQLiteStateRepository,
        run_id: str,
        catalog: DomainCatalog,
        seed: int = 0,
    ) -> None:
        self._repository = repository
        self._run_id = _IDENTIFIER_ADAPTER.validate_python(run_id, strict=True)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
            raise ValueError("seed must be an integer between 0 and 2**32 - 1")
        self._seed = seed
        self._catalog = catalog
        self._active_advance: _AdvanceContext | None = None
        self._pending_interventions: dict[int, tuple[ScheduledIntervention, ...]] = {}
        self._pending_observations: dict[int, tuple[Observation, ...]] = {}
        self._checkpoint = repository.get_checkpoint(self._run_id)
        self._rebuild_environment()

    @classmethod
    def rebuild(
        cls,
        repository: SQLiteStateRepository,
        run_id: str,
        catalog: DomainCatalog,
        seed: int = 0,
    ) -> SimulationRuntime:
        """Recreate callbacks solely from the latest persisted checkpoint."""

        return cls(repository, run_id, catalog, seed)

    @classmethod
    def from_repository(
        cls,
        repository: SQLiteStateRepository,
        run_id: str,
        catalog: DomainCatalog,
        seed: int = 0,
    ) -> SimulationRuntime:
        return cls.rebuild(repository, run_id, catalog, seed)

    @property
    def repository(self) -> SQLiteStateRepository:
        return self._repository

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def snapshot(self) -> OperationalSnapshot:
        self._synchronize_read()
        return self._checkpoint.snapshot

    @property
    def now(self) -> int:
        return self.snapshot.simulated_time_seconds

    def pause(self) -> RuntimeProjection:
        """Return persisted public state; wall-clock waiting never runs SimPy."""

        return self.project_public_state()

    def read(self) -> RuntimeProjection:
        return self.project_public_state()

    def projection(self) -> RuntimeProjection:
        return self.project_public_state()

    def project_public_state(self) -> RuntimeProjection:
        self._synchronize_read()
        snapshot = self._checkpoint.snapshot
        envelopes = self._repository.list_events(self._run_id)
        observations: list[Observation] = []
        public_events: list[PublicRuntimeEvent] = []
        attempt_identity: dict[str, tuple[str, str]] = {
            item.attempt_id: (item.operation_id, item.machine_id) for item in snapshot.attempts
        }

        for envelope in envelopes:
            event = envelope.event
            machine_id: str | None = None
            attempt_id: str | None = None
            operation_id: str | None = None
            order_id: str | None = None
            observation_id: str | None = None
            plan_revision: int | None = None
            if event.event_type is EventType.PLAN_APPLIED:
                payload = _json_payload(PlanAppliedPayload, event.payload)
                plan_revision = payload.plan_revision
                for attempt in payload.attempts:
                    attempt_identity[attempt.attempt_id] = (
                        attempt.operation_id,
                        attempt.machine_id,
                    )
            elif event.event_type in (
                EventType.OPERATION_STARTED,
                EventType.OPERATION_COMPLETED,
                EventType.OPERATION_INTERRUPTED,
            ):
                attempt_id = str(event.payload["attempt_id"])
                identity = attempt_identity.get(attempt_id)
                if identity is not None:
                    operation_id, machine_id = identity
            elif event.event_type in (EventType.MACHINE_DOWN, EventType.MACHINE_RESTORED):
                machine_id = str(event.payload["machine_id"])
            elif event.event_type is EventType.ORDER_REGISTERED:
                payload = _json_payload(OrderRegisteredPayload, event.payload)
                order_id = payload.order.order_id
            elif event.event_type is EventType.OBSERVATION_RECORDED:
                payload = _json_payload(ObservationRecordedPayload, event.payload)
                observation = payload.observation
                observations.append(observation)
                observation_id = observation.observation_id
                machine_id = observation.machine_id
            public_events.append(
                PublicRuntimeEvent(
                    event_id=event.event_id,
                    sequence=event.sequence,
                    simulated_time_seconds=event.simulated_time_seconds,
                    event_type=event.event_type,
                    machine_id=machine_id,
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    order_id=order_id,
                    observation_id=observation_id,
                    plan_revision=plan_revision,
                )
            )

        return RuntimeProjection(
            run_id=snapshot.run_id,
            state_version=snapshot.state_version,
            plan_revision=snapshot.plan_revision,
            simulated_time_seconds=snapshot.simulated_time_seconds,
            hard_horizon_seconds=snapshot.hard_horizon_seconds,
            machines=snapshot.machines,
            orders=snapshot.orders,
            operations=snapshot.operations,
            attempts=snapshot.attempts,
            observations=tuple(observations),
            blocked_attempts=self._blocked_attempts(snapshot),
            events=tuple(public_events),
        )

    def activate_plan(
        self,
        candidate: CandidateProposal,
        idempotency_key: str,
        effective_time_seconds: int | None = None,
    ) -> ActivationResult:
        """Validate and atomically activate a zero-delta plan.

        The immediately following integer second may pre-stage only ``plan_applied``
        while the authoritative clock remains unchanged.  A later :meth:`advance_to`
        dispatches starts and the time advance at that second.  More distant pre-staging
        is rejected because it would close intervening event boundaries.  Pre-staging is
        safe only when no lower-priority completion would have to be inserted first.
        """

        key = _IDENTIFIER_ADAPTER.validate_python(idempotency_key, strict=True)
        if candidate.proposed_order_deltas or candidate.proposed_resource_deltas:
            raise UnsupportedPlanDeltaError

        existing_group = tuple(
            item for item in self._repository.list_events(self._run_id) if item.mutation_id == key
        )
        if existing_group:
            if not any(item.event.event_type is EventType.PLAN_APPLIED for item in existing_group):
                raise CommandConflictError(
                    f"idempotency key {key!r} already identifies another command"
                )
            base_version = existing_group[0].base_state_version
            base = self._repository.get_checkpoint(
                self._run_id,
                state_version=base_version,
            )
            effective = (
                base.snapshot.simulated_time_seconds
                if effective_time_seconds is None
                else self._require_seconds(effective_time_seconds, "effective_time_seconds")
            )
            try:
                report = self._validate_plan(base.snapshot, candidate)
                drafts = self._activation_drafts(base.snapshot, candidate, report, effective)
                self._repository.apply_mutation(
                    run_id=self._run_id,
                    expected_state_version=base.snapshot.state_version,
                    idempotency_key=key,
                    command_name="simulation_activate_plan",
                    command_payload=self._activation_command_payload(candidate, effective),
                    events=drafts,
                )
            except (InvalidPlanError, InvalidAdvanceError, IdempotencyConflictError) as exc:
                raise CommandConflictError(
                    f"idempotency key {key!r} was reused for different activation content"
                ) from exc
            self._reload_from_repository()
            projection = self.project_public_state()
            return ActivationResult(
                status=CommandStatus.IDEMPOTENT,
                run_id=self._run_id,
                effective_time_seconds=effective,
                previous_state_version=projection.state_version,
                resulting_state_version=projection.state_version,
                validation=report,
                snapshot=self._checkpoint.snapshot,
                mutation=None,
                projection=projection,
            )

        self._synchronize_read()
        # A peer runtime may have committed this activation between the initial
        # lookup and our authoritative refresh.  Route through the normal retry
        # path before validating against the now-newer plan revision.
        if any(item.mutation_id == key for item in self._repository.list_events(self._run_id)):
            return self.activate_plan(candidate, key, effective_time_seconds)
        snapshot = self._checkpoint.snapshot
        effective = (
            snapshot.simulated_time_seconds
            if effective_time_seconds is None
            else self._require_seconds(effective_time_seconds, "effective_time_seconds")
        )
        report = self._validate_plan(snapshot, candidate)
        drafts = self._activation_drafts(snapshot, candidate, report, effective)
        previous_version = snapshot.state_version
        try:
            mutation = self._repository.apply_mutation(
                run_id=self._run_id,
                expected_state_version=previous_version,
                idempotency_key=key,
                command_name="simulation_activate_plan",
                command_payload=self._activation_command_payload(candidate, effective),
                events=drafts,
            )
        except VersionConflictError as exc:
            self._reload_from_repository()
            raise ConcurrentRuntimeError(
                "authoritative state changed while activating the plan; runtime rebuilt"
            ) from exc
        except IdempotencyConflictError as exc:
            self._reload_from_repository()
            raise CommandConflictError(
                f"idempotency key {key!r} was reused for different activation content"
            ) from exc

        if mutation.replayed_idempotent:
            self._reload_from_repository()
            projection = self.project_public_state()
            return ActivationResult(
                status=CommandStatus.IDEMPOTENT,
                run_id=self._run_id,
                effective_time_seconds=effective,
                previous_state_version=projection.state_version,
                resulting_state_version=projection.state_version,
                validation=report,
                snapshot=self._checkpoint.snapshot,
                mutation=None,
                projection=projection,
            )

        self._checkpoint = mutation.checkpoint
        self._schedule_snapshot_callbacks(self._checkpoint.snapshot)
        projection = self.project_public_state()
        return ActivationResult(
            status=CommandStatus.APPLIED,
            run_id=self._run_id,
            effective_time_seconds=effective,
            previous_state_version=previous_version,
            resulting_state_version=mutation.resulting_state_version,
            validation=report,
            snapshot=mutation.checkpoint.snapshot,
            mutation=mutation,
            projection=projection,
        )

    def advance_to(
        self,
        target_seconds: int,
        advance_id: str,
        interventions: Sequence[ScheduledIntervention] = (),
        observations: Sequence[Observation | ObservationTemplate] = (),
    ) -> AdvanceResult:
        """Inclusively dispatch every known boundary through ``target_seconds``."""

        if self._active_advance is not None:
            raise RuntimeStateError("advance_to is not re-entrant")
        target = self._require_seconds(target_seconds, "target_seconds")
        command_id = _IDENTIFIER_ADAPTER.validate_python(advance_id, strict=True)
        self._synchronize_read()
        starting_snapshot = self._checkpoint.snapshot
        if target > starting_snapshot.hard_horizon_seconds:
            raise InvalidAdvanceError(
                f"target {target} exceeds hard horizon {starting_snapshot.hard_horizon_seconds}"
            )

        normalized_interventions = self._normalize_interventions(interventions, target)
        normalized_observations = self._normalize_observations(
            observations,
            target,
            starting_snapshot,
        )
        fingerprint = canonical_hash(
            {
                "schema_version": 1,
                "advance_id": command_id,
                "target_seconds": target,
                "interventions": [
                    _public_intervention_data(item) for item in normalized_interventions
                ],
                "observations": [item.model_dump(mode="json") for item in normalized_observations],
                "seed": self._seed,
            }
        )
        ledger = self._simulation_audits()
        started = self._find_advance_audit(ledger, _ADVANCE_STARTED, command_id)
        completed = self._find_advance_audit(ledger, _ADVANCE_COMPLETED, command_id)
        for audit in (started, completed):
            if audit is not None and audit.details.get("command_fingerprint") != fingerprint:
                raise CommandConflictError(
                    f"advance_id {command_id!r} was reused for different public content"
                )
        if completed is not None:
            projection = self.project_public_state()
            return AdvanceResult(
                status=CommandStatus.IDEMPOTENT,
                run_id=self._run_id,
                requested_target_seconds=target,
                previous_time_seconds=projection.simulated_time_seconds,
                resulting_time_seconds=projection.simulated_time_seconds,
                previous_state_version=projection.state_version,
                resulting_state_version=projection.state_version,
                snapshot=self._checkpoint.snapshot,
                projection=projection,
            )

        if target < starting_snapshot.simulated_time_seconds:
            raise InvalidAdvanceError(
                f"cannot advance backward from {starting_snapshot.simulated_time_seconds} "
                f"to {target}"
            )

        pending_interventions, duplicate_intervention = self._preflight_interventions(
            normalized_interventions,
            ledger,
            starting_snapshot.simulated_time_seconds,
        )
        pending_observations, duplicate_observation = self._preflight_observations(
            normalized_observations,
            starting_snapshot.simulated_time_seconds,
        )
        self._validate_pending_interventions(starting_snapshot, pending_interventions)
        self._pending_interventions = self._group_by_time(
            pending_interventions,
            lambda item: item.at_seconds,
        )
        self._pending_observations = self._group_by_time(
            pending_observations,
            lambda item: item.observed_at_seconds,
        )
        context = _AdvanceContext(
            advance_id=command_id,
            target_seconds=target,
            command_fingerprint=fingerprint,
            start_audit_pending=started is None,
        )
        self._active_advance = context
        for at_seconds in sorted(
            {*self._pending_interventions, *self._pending_observations, target}
        ):
            self._ensure_dispatcher(at_seconds)

        try:
            # SimPy ``run(until=T)`` excludes ordinary events scheduled exactly at T.
            # Explicit peek/step makes the public boundary inclusive.
            while self._env.peek() <= target:
                self._env.step()
        except VersionConflictError as exc:
            self._reload_from_repository()
            raise ConcurrentRuntimeError(
                "authoritative state changed during advance; runtime rebuilt"
            ) from exc
        except BaseException:
            self._reload_from_repository()
            raise
        finally:
            self._active_advance = None
            self._pending_interventions = {}
            self._pending_observations = {}

        self._synchronize_read()
        resulting = self._checkpoint.snapshot
        if resulting.simulated_time_seconds != target:
            raise RuntimeStateError(
                f"advance ended at {resulting.simulated_time_seconds}, expected {target}"
            )
        projection = self.project_public_state()
        if context.mutations:
            status = CommandStatus.APPLIED
        elif duplicate_intervention or duplicate_observation:
            status = CommandStatus.IDEMPOTENT
        else:
            status = CommandStatus.NO_OP
        return AdvanceResult(
            status=status,
            run_id=self._run_id,
            requested_target_seconds=target,
            previous_time_seconds=starting_snapshot.simulated_time_seconds,
            resulting_time_seconds=resulting.simulated_time_seconds,
            previous_state_version=starting_snapshot.state_version,
            resulting_state_version=resulting.state_version,
            snapshot=resulting,
            mutations=tuple(context.mutations),
            projection=projection,
        )

    def _synchronize_read(self) -> None:
        latest = self._repository.get_checkpoint(self._run_id)
        if (
            latest.snapshot.state_version != self._checkpoint.snapshot.state_version
            or latest.snapshot_hash != self._checkpoint.snapshot_hash
        ):
            self._checkpoint = latest
            self._rebuild_environment()

    def _reload_from_repository(self) -> None:
        self._checkpoint = self._repository.get_checkpoint(self._run_id)
        self._rebuild_environment()

    def _rebuild_environment(self) -> None:
        snapshot = self._checkpoint.snapshot
        self._env = simpy.Environment(initial_time=snapshot.simulated_time_seconds)
        self._start_callbacks: dict[int, set[_StartCallback]] = {}
        self._completion_callbacks: dict[int, set[_CompletionCallback]] = {}
        self._dispatcher_times: set[int] = set()
        self._processed_dispatcher_times: set[int] = set()
        self._schedule_snapshot_callbacks(snapshot)

    def _schedule_snapshot_callbacks(self, snapshot: OperationalSnapshot) -> None:
        now = int(self._env.now)
        for attempt in snapshot.attempts:
            if attempt.status is AttemptStatus.SCHEDULED and attempt.scheduled_window is not None:
                start = attempt.scheduled_window.start_seconds
                if start < now:
                    continue
                callback = _StartCallback(
                    attempt_id=attempt.attempt_id,
                    operation_id=attempt.operation_id,
                    machine_id=attempt.machine_id,
                    start_seconds=start,
                    end_seconds=attempt.scheduled_window.end_seconds,
                )
                self._start_callbacks.setdefault(start, set()).add(callback)
                self._ensure_dispatcher(start)
            elif (
                attempt.status is AttemptStatus.RUNNING and attempt.actual_start_seconds is not None
            ):
                completion = attempt.actual_start_seconds + attempt.planned_duration_seconds
                if completion < now:
                    raise RuntimeStateError(
                        f"running attempt {attempt.attempt_id} is overdue at persisted time {now}"
                    )
                window = attempt.scheduled_window
                callback = _CompletionCallback(
                    attempt_id=attempt.attempt_id,
                    operation_id=attempt.operation_id,
                    machine_id=attempt.machine_id,
                    actual_start_seconds=attempt.actual_start_seconds,
                    completion_seconds=completion,
                    scheduled_start_seconds=(window.start_seconds if window else None),
                    scheduled_end_seconds=(window.end_seconds if window else None),
                )
                self._completion_callbacks.setdefault(completion, set()).add(callback)
                self._ensure_dispatcher(completion)

    def _ensure_dispatcher(self, at_seconds: int) -> None:
        if (
            at_seconds < int(self._env.now)
            or at_seconds in self._dispatcher_times
            or at_seconds in self._processed_dispatcher_times
        ):
            return
        self._dispatcher_times.add(at_seconds)
        self._env.process(self._dispatcher(at_seconds))

    def _dispatcher(self, at_seconds: int) -> Iterable[simpy.events.Event]:
        yield self._env.timeout(at_seconds - int(self._env.now))
        self._dispatcher_times.discard(at_seconds)
        self._processed_dispatcher_times.add(at_seconds)
        try:
            self._dispatch_boundary(at_seconds)
        finally:
            # Keep the guard through callback rescheduling inside this dispatcher, but
            # let a later public command revisit T and receive an explicit closed-
            # boundary rejection instead of silently dropping its input.
            self._processed_dispatcher_times.discard(at_seconds)

    def _dispatch_boundary(self, at_seconds: int) -> None:
        context = self._active_advance
        if context is None:
            raise RuntimeStateError("a SimPy dispatcher ran outside explicit advance_to")
        latest = self._repository.get_checkpoint(self._run_id)
        if (
            latest.snapshot.state_version != self._checkpoint.snapshot.state_version
            or latest.snapshot_hash != self._checkpoint.snapshot_hash
        ):
            raise VersionConflictError(
                self._run_id,
                self._checkpoint.snapshot.state_version,
                latest.snapshot.state_version,
            )
        snapshot = latest.snapshot
        if at_seconds < snapshot.simulated_time_seconds:
            return

        completions = tuple(sorted(self._completion_callbacks.pop(at_seconds, set())))
        starts = tuple(sorted(self._start_callbacks.pop(at_seconds, set())))
        interventions = self._pending_interventions.pop(at_seconds, ())
        explicit_observations = self._pending_observations.pop(at_seconds, ())
        drafts, applied_interventions = self._boundary_drafts(
            snapshot,
            at_seconds,
            completions,
            starts,
            interventions,
            explicit_observations,
        )
        if not drafts:
            return
        self._require_open_boundary(latest, drafts)

        audits: list[AuditRecord] = []
        if context.start_audit_pending:
            audits.append(self._advance_audit(context, _ADVANCE_STARTED, at_seconds))
        audits.extend(self._intervention_audit(item) for item in applied_interventions)
        is_final = at_seconds == context.target_seconds
        if is_final and context.completion_audit_pending:
            audits.append(self._advance_audit(context, _ADVANCE_COMPLETED, at_seconds))

        mutation_key = _stable_identifier(
            "simadv-",
            {"advance_id": context.advance_id, "at_seconds": at_seconds},
        )

        def append_audits(transaction: Any) -> None:
            for audit in audits:
                transaction.append_audit(audit)

        mutation = self._repository.apply_mutation(
            run_id=self._run_id,
            expected_state_version=snapshot.state_version,
            idempotency_key=mutation_key,
            command_name="simulation_advance_boundary",
            command_payload={
                "schema_version": 1,
                "advance_id": context.advance_id,
                "command_fingerprint": context.command_fingerprint,
                "at_seconds": at_seconds,
                "is_final": is_final,
                "seed": self._seed,
            },
            events=drafts,
            participant=append_audits if audits else None,
        )
        context.mutations.append(mutation)
        context.start_audit_pending = False
        if is_final:
            context.completion_audit_pending = False
        self._checkpoint = mutation.checkpoint
        self._schedule_snapshot_callbacks(self._checkpoint.snapshot)

    def _boundary_drafts(
        self,
        snapshot: OperationalSnapshot,
        at_seconds: int,
        completion_callbacks: tuple[_CompletionCallback, ...],
        start_callbacks: tuple[_StartCallback, ...],
        interventions: tuple[ScheduledIntervention, ...],
        explicit_observations: tuple[Observation, ...],
    ) -> tuple[tuple[EventDraft, ...], tuple[ScheduledIntervention, ...]]:
        attempts = {item.attempt_id: item for item in snapshot.attempts}
        operations = {item.operation_id: item for item in snapshot.operations}
        machine_status = {item.machine_id: item.status for item in snapshot.machines}
        completed_operation_ids = {
            item.operation_id
            for item in snapshot.operations
            if item.status is OperationStatus.COMPLETED
        }
        drafts: list[EventDraft] = []
        touched_machines: set[str] = set()
        completing_attempt_ids: set[str] = set()

        for callback in completion_callbacks:
            attempt = attempts.get(callback.attempt_id)
            if attempt is None or not self._completion_callback_is_current(attempt, callback):
                continue
            if machine_status.get(attempt.machine_id) is not MachineStatus.RUNNING:
                raise RuntimeStateError(
                    f"running attempt {attempt.attempt_id} has non-running machine state"
                )
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.OPERATION_COMPLETED,
                    OperationCompletedPayload(attempt_id=attempt.attempt_id),
                )
            )
            completing_attempt_ids.add(attempt.attempt_id)
            completed_operation_ids.add(attempt.operation_id)
            machine_status[attempt.machine_id] = MachineStatus.AVAILABLE
            touched_machines.add(attempt.machine_id)

        faults = tuple(
            sorted(
                (item for item in interventions if item.kind is InterventionKind.MACHINE_FAULT),
                key=lambda item: (item.target_machine_id or "", item.intervention_id),
            )
        )
        restorations = tuple(
            sorted(
                (item for item in interventions if item.kind is InterventionKind.MACHINE_RESTORED),
                key=lambda item: (item.target_machine_id or "", item.intervention_id),
            )
        )
        additions = tuple(
            sorted(
                (item for item in interventions if item.kind is InterventionKind.ADD_ORDER),
                key=lambda item: item.intervention_id,
            )
        )
        checkpoints = tuple(
            sorted(
                (item for item in interventions if item.kind is InterventionKind.CHECKPOINT),
                key=lambda item: item.intervention_id,
            )
        )
        self._reject_ambiguous_machine_transitions(faults, restorations)
        faulted_this_boundary: set[str] = set()
        applied_interventions: list[ScheduledIntervention] = []

        for intervention in faults:
            machine_id = intervention.target_machine_id
            assert machine_id is not None
            status = machine_status.get(machine_id)
            if status is None:
                raise InvalidAdvanceError(f"fault targets unknown machine {machine_id}")
            if status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
                raise InvalidAdvanceError(
                    f"machine {machine_id} cannot fault from status {status.value}"
                )
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.MACHINE_DOWN,
                    MachineDownPayload(machine_id=machine_id, status="down"),
                )
            )
            machine_status[machine_id] = MachineStatus.DOWN
            faulted_this_boundary.add(machine_id)
            touched_machines.add(machine_id)
            applied_interventions.append(intervention)

            running = sorted(
                (
                    attempt
                    for attempt in snapshot.attempts
                    if attempt.machine_id == machine_id
                    and attempt.status is AttemptStatus.RUNNING
                    and attempt.attempt_id not in completing_attempt_ids
                ),
                key=lambda item: item.attempt_id,
            )
            if len(running) > 1:
                raise RuntimeStateError(f"machine {machine_id} has multiple running attempts")
            if running:
                attempt = running[0]
                assert attempt.actual_start_seconds is not None
                elapsed = at_seconds - attempt.actual_start_seconds
                if not 0 < elapsed < attempt.planned_duration_seconds:
                    raise BoundaryClosedError(
                        f"fault at {at_seconds} cannot partially interrupt {attempt.attempt_id}"
                    )
                drafts.append(
                    _draft(
                        at_seconds,
                        EventType.OPERATION_INTERRUPTED,
                        OperationInterruptedPayload(attempt_id=attempt.attempt_id),
                    )
                )

        for intervention in restorations:
            machine_id = intervention.target_machine_id
            assert machine_id is not None
            status = machine_status.get(machine_id)
            if status is None:
                raise InvalidAdvanceError(f"restoration targets unknown machine {machine_id}")
            if status not in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
                raise InvalidAdvanceError(
                    f"machine {machine_id} cannot be restored from status {status.value}"
                )
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.MACHINE_RESTORED,
                    MachineRestoredPayload(machine_id=machine_id, status="available"),
                )
            )
            machine_status[machine_id] = MachineStatus.AVAILABLE
            touched_machines.add(machine_id)
            applied_interventions.append(intervention)

        known_order_ids = {item.order_id for item in snapshot.orders}
        for intervention in additions:
            order = intervention.order_to_add
            assert order is not None
            if order.order_id in known_order_ids:
                raise InvalidAdvanceError(f"order {order.order_id} is already registered")
            if order.release_time_seconds > at_seconds:
                raise InvalidAdvanceError(
                    f"added order {order.order_id} releases after its injection boundary"
                )
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.ORDER_REGISTERED,
                    OrderRegisteredPayload(order=order),
                )
            )
            known_order_ids.add(order.order_id)
            applied_interventions.append(intervention)
        applied_interventions.extend(checkpoints)

        reserved_machines: set[str] = set()
        for callback in start_callbacks:
            attempt = attempts.get(callback.attempt_id)
            if attempt is None or not self._start_callback_is_current(attempt, callback):
                continue
            operation = operations.get(attempt.operation_id)
            if operation is None:
                raise RuntimeStateError(f"scheduled attempt {attempt.attempt_id} has no operation")
            if attempt.machine_id in faulted_this_boundary:
                continue
            if machine_status.get(attempt.machine_id) is not MachineStatus.AVAILABLE:
                continue
            if not _attempt_fits_latest_calendar(snapshot, attempt):
                continue
            if attempt.machine_id in reserved_machines:
                continue
            if not set(operation.predecessor_operation_ids) <= completed_operation_ids:
                continue
            if operation.status not in (
                OperationStatus.PENDING,
                OperationStatus.READY,
                OperationStatus.INTERRUPTED,
            ):
                continue
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.OPERATION_STARTED,
                    OperationStartedPayload(attempt_id=attempt.attempt_id),
                )
            )
            reserved_machines.add(attempt.machine_id)
            machine_status[attempt.machine_id] = MachineStatus.RUNNING
            touched_machines.add(attempt.machine_id)

        persisted_observations = self._persisted_observations()
        explicit_machine_ids: set[str] = set()
        for observation in sorted(explicit_observations, key=lambda item: item.observation_id):
            existing = persisted_observations.get(observation.observation_id)
            if existing is not None:
                if existing != observation:
                    raise CommandConflictError(
                        f"observation_id {observation.observation_id!r} has conflicting content"
                    )
                continue
            explicit_machine_ids.add(observation.machine_id)
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.OBSERVATION_RECORDED,
                    ObservationRecordedPayload(observation=observation),
                )
            )
        for machine_id in sorted(touched_machines - explicit_machine_ids):
            observation = self._generated_observation(
                machine_id,
                at_seconds,
                machine_status[machine_id],
            )
            existing = persisted_observations.get(observation.observation_id)
            if existing is not None:
                if existing != observation:
                    raise CommandConflictError(
                        f"generated observation {observation.observation_id!r} conflicts"
                    )
                continue
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.OBSERVATION_RECORDED,
                    ObservationRecordedPayload(observation=observation),
                )
            )
        if at_seconds > snapshot.simulated_time_seconds:
            drafts.append(
                _draft(
                    at_seconds,
                    EventType.TIME_ADVANCED,
                    TimeAdvancedPayload(target_time_seconds=at_seconds),
                )
            )
        return tuple(drafts), tuple(applied_interventions)

    def _validate_plan(
        self,
        snapshot: OperationalSnapshot,
        candidate: CandidateProposal,
    ) -> ValidationReport:
        report = validate_candidate(
            snapshot,
            self._catalog,
            candidate,
            projected_order_deltas=(),
            projected_resource_deltas=(),
        )
        if report.is_valid:
            return report
        if has_any_violation(report, _STALE_CODES):
            raise StalePlanError(report)
        raise InvalidPlanError(report)

    def _activation_drafts(
        self,
        snapshot: OperationalSnapshot,
        candidate: CandidateProposal,
        report: ValidationReport,
        effective: int,
    ) -> tuple[EventDraft, ...]:
        if effective < snapshot.simulated_time_seconds:
            raise InvalidAdvanceError("plan effective time cannot precede persisted time")
        if effective > snapshot.hard_horizon_seconds:
            raise InvalidAdvanceError("plan effective time exceeds hard horizon")
        if effective > snapshot.simulated_time_seconds:
            if effective != snapshot.simulated_time_seconds + 1:
                raise InvalidAdvanceError(
                    "future plan activation must use the next integer second; a farther "
                    "future plan event would close intermediate event boundaries"
                )
            for attempt in snapshot.attempts:
                if (
                    attempt.status is AttemptStatus.RUNNING
                    and attempt.actual_start_seconds is not None
                    and attempt.actual_start_seconds + attempt.planned_duration_seconds <= effective
                ):
                    raise InvalidAdvanceError(
                        f"running completion {attempt.attempt_id} cannot be skipped by pre-staging"
                    )
            earlier = sorted(
                item.attempt_id
                for item in candidate.assignments
                if item.window.start_seconds < effective
            )
            if earlier:
                raise InvalidAdvanceError(
                    "pre-staged plan contains assignments before its effective boundary: "
                    + ", ".join(earlier)
                )

        drafts: list[EventDraft] = [
            _draft(
                effective,
                EventType.PLAN_APPLIED,
                PlanAppliedPayload(
                    plan_revision=candidate.proposed_plan_revision,
                    operations=report.projected_operations,
                    attempts=report.projected_attempts,
                ),
            )
        ]
        planned = _model_update(
            snapshot,
            plan_revision=candidate.proposed_plan_revision,
            operations=report.projected_operations,
            attempts=report.projected_attempts,
        )
        # A plan applied at the current, still-open boundary starts due work in the
        # same atomic group.  A future plan is only pre-staged: starts remain callbacks
        # for the subsequent explicit advance through that boundary.
        if effective > snapshot.simulated_time_seconds:
            self._require_open_boundary(self._checkpoint_for_snapshot(snapshot), tuple(drafts))
            return tuple(drafts)

        due = tuple(
            sorted(
                (
                    _StartCallback(
                        attempt_id=item.attempt_id,
                        operation_id=item.operation_id,
                        machine_id=item.machine_id,
                        start_seconds=item.scheduled_window.start_seconds,
                        end_seconds=item.scheduled_window.end_seconds,
                    )
                    for item in planned.attempts
                    if item.status is AttemptStatus.SCHEDULED
                    and item.scheduled_window is not None
                    and item.scheduled_window.start_seconds == effective
                ),
            )
        )
        machine_status = {item.machine_id: item.status for item in planned.machines}
        operations = {item.operation_id: item for item in planned.operations}
        attempts = {item.attempt_id: item for item in planned.attempts}
        completed = {
            item.operation_id
            for item in planned.operations
            if item.status is OperationStatus.COMPLETED
        }
        touched: set[str] = set()
        for callback in due:
            attempt = attempts[callback.attempt_id]
            operation = operations[attempt.operation_id]
            if machine_status[attempt.machine_id] is not MachineStatus.AVAILABLE:
                continue
            if not _attempt_fits_latest_calendar(planned, attempt):
                continue
            if not set(operation.predecessor_operation_ids) <= completed:
                continue
            drafts.append(
                _draft(
                    effective,
                    EventType.OPERATION_STARTED,
                    OperationStartedPayload(attempt_id=attempt.attempt_id),
                )
            )
            machine_status[attempt.machine_id] = MachineStatus.RUNNING
            touched.add(attempt.machine_id)
        for machine_id in sorted(touched):
            drafts.append(
                _draft(
                    effective,
                    EventType.OBSERVATION_RECORDED,
                    ObservationRecordedPayload(
                        observation=self._generated_observation(
                            machine_id,
                            effective,
                            machine_status[machine_id],
                        )
                    ),
                )
            )
        self._require_open_boundary(self._checkpoint_for_snapshot(snapshot), tuple(drafts))
        return tuple(drafts)

    def _checkpoint_for_snapshot(self, snapshot: OperationalSnapshot) -> Any:
        if snapshot.state_version == self._checkpoint.snapshot.state_version:
            return self._checkpoint
        return self._repository.get_checkpoint(
            self._run_id,
            state_version=snapshot.state_version,
        )

    def _activation_command_payload(
        self,
        candidate: CandidateProposal,
        effective: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": candidate.proposal_id,
            "proposal_hash": candidate.proposal_hash,
            "effective_time_seconds": effective,
            "seed": self._seed,
        }

    @staticmethod
    def _completion_callback_is_current(
        attempt: OperationAttempt,
        callback: _CompletionCallback,
    ) -> bool:
        window = attempt.scheduled_window
        return (
            attempt.status is AttemptStatus.RUNNING
            and attempt.operation_id == callback.operation_id
            and attempt.machine_id == callback.machine_id
            and attempt.actual_start_seconds == callback.actual_start_seconds
            and attempt.actual_start_seconds + attempt.planned_duration_seconds
            == callback.completion_seconds
            and (window.start_seconds if window else None) == callback.scheduled_start_seconds
            and (window.end_seconds if window else None) == callback.scheduled_end_seconds
        )

    @staticmethod
    def _start_callback_is_current(
        attempt: OperationAttempt,
        callback: _StartCallback,
    ) -> bool:
        window = attempt.scheduled_window
        return (
            attempt.status is AttemptStatus.SCHEDULED
            and attempt.operation_id == callback.operation_id
            and attempt.machine_id == callback.machine_id
            and window is not None
            and window.start_seconds == callback.start_seconds
            and window.end_seconds == callback.end_seconds
        )

    @staticmethod
    def _require_seconds(value: int, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidAdvanceError(f"{field_name} must be a non-negative integer")
        return value

    def _normalize_interventions(
        self,
        interventions: Sequence[ScheduledIntervention],
        target: int,
    ) -> tuple[ScheduledIntervention, ...]:
        by_id: dict[str, ScheduledIntervention] = {}
        for raw in interventions:
            if not isinstance(raw, ScheduledIntervention):
                raise InvalidAdvanceError("interventions must be ScheduledIntervention values")
            if raw.at_seconds > target:
                raise InvalidAdvanceError(
                    f"intervention {raw.intervention_id} occurs after advance target"
                )
            previous = by_id.get(raw.intervention_id)
            if previous is not None:
                if _public_intervention_data(previous) != _public_intervention_data(raw):
                    raise CommandConflictError(
                        f"intervention_id {raw.intervention_id!r} has conflicting content"
                    )
                continue
            by_id[raw.intervention_id] = raw
        return tuple(
            sorted(
                by_id.values(),
                key=lambda item: (item.at_seconds, item.intervention_id),
            )
        )

    def _normalize_observations(
        self,
        observations: Sequence[Observation | ObservationTemplate],
        target: int,
        snapshot: OperationalSnapshot,
    ) -> tuple[Observation, ...]:
        by_id: dict[str, Observation] = {}
        # Both sides are explicit contracts, but only the current SQLite snapshot is
        # operationally authoritative.  Their intersection rejects a catalog-only
        # machine before any earlier SimPy boundary can commit.
        catalog_machine_ids = {item.machine_id for item in self._catalog.machines}
        snapshot_machine_ids = {item.machine_id for item in snapshot.machines}
        machine_ids = catalog_machine_ids & snapshot_machine_ids
        for raw in observations:
            if isinstance(raw, ObservationTemplate):
                observation = self._realize_template(raw)
            elif isinstance(raw, Observation):
                summary = raw.summary
                if "synthetic" not in summary.casefold():
                    summary = f"Synthetic observation: {summary}"
                observation = _model_update(raw, summary=summary[:300], recorded_at_utc=None)
            else:
                raise InvalidAdvanceError(
                    "observations must be Observation or ObservationTemplate values"
                )
            if observation.run_id != self._run_id:
                raise InvalidAdvanceError(
                    f"observation {observation.observation_id} belongs to another run"
                )
            if observation.machine_id not in machine_ids:
                raise InvalidAdvanceError(
                    f"observation {observation.observation_id} references unknown machine"
                )
            if observation.observed_at_seconds > target:
                raise InvalidAdvanceError(
                    f"observation {observation.observation_id} occurs after advance target"
                )
            for signal in observation.signals:
                allowed_units = _PUBLIC_SIGNAL_UNITS.get(signal.name)
                if allowed_units is None or signal.unit not in allowed_units:
                    raise InvalidAdvanceError(
                        f"observation signal {signal.name!r}/{signal.unit!r} is not allowlisted"
                    )
            previous = by_id.get(observation.observation_id)
            if previous is not None and previous != observation:
                raise CommandConflictError(
                    f"observation_id {observation.observation_id!r} has conflicting content"
                )
            by_id[observation.observation_id] = observation
        return tuple(
            sorted(by_id.values(), key=lambda item: (item.observed_at_seconds, item.observation_id))
        )

    def _realize_template(self, template: ObservationTemplate) -> Observation:
        values = []
        for spec in template.signals:
            span = spec.jitter_max - spec.jitter_min + 1
            offset = (
                self._deterministic_int(
                    "explicit-observation",
                    template.observation_id,
                    template.machine_id,
                    template.observed_at_seconds,
                    spec.name,
                )
                % span
            )
            values.append(
                TelemetryValue(
                    name=spec.name,
                    value=spec.base_value + spec.jitter_min + offset,
                    unit=spec.unit,
                )
            )
        summary = template.public_summary
        if "synthetic" not in summary.casefold():
            summary = f"Synthetic observation: {summary}"
        return Observation(
            schema_version=1,
            observation_id=template.observation_id,
            run_id=self._run_id,
            machine_id=template.machine_id,
            observed_at_seconds=template.observed_at_seconds,
            signals=tuple(values),
            summary=summary[:300],
        )

    def _generated_observation(
        self,
        machine_id: str,
        at_seconds: int,
        status: MachineStatus,
    ) -> Observation:
        down = status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
        temperature_base = 71000 if down else 48500
        vibration_base = 8800 if down else 2200
        temperature = temperature_base + (
            self._deterministic_int("temperature", machine_id, at_seconds) % 601 - 300
        )
        vibration = vibration_base + (
            self._deterministic_int("vibration", machine_id, at_seconds) % 201 - 100
        )
        observation_id = _stable_identifier(
            "obs-",
            {
                "run_id": self._run_id,
                "seed": self._seed,
                "machine_id": machine_id,
                "at_seconds": at_seconds,
            },
        )
        state_text = "unavailable" if down else status.value
        return Observation(
            schema_version=1,
            observation_id=observation_id,
            run_id=self._run_id,
            machine_id=machine_id,
            observed_at_seconds=at_seconds,
            signals=(
                TelemetryValue(
                    name="temperature_millicelsius",
                    value=temperature,
                    unit="mdegC",
                ),
                TelemetryValue(
                    name="vibration_micrometers_per_second",
                    value=vibration,
                    unit="um_per_s",
                ),
            ),
            summary=(
                f"Synthetic telemetry for {machine_id}; current operational status is {state_text}."
            ),
        )

    def _deterministic_int(self, *parts: object) -> int:
        digest = hashlib.sha256(
            canonical_hash(
                {
                    "namespace": "shopfloor-pilot-synthetic-telemetry-v1",
                    "seed": self._seed,
                    "parts": [str(item) for item in parts],
                }
            ).encode("ascii")
        ).digest()
        return int.from_bytes(digest[:8], "big")

    def _preflight_interventions(
        self,
        interventions: tuple[ScheduledIntervention, ...],
        audits: tuple[AuditRecord, ...],
        current_time: int,
    ) -> tuple[tuple[ScheduledIntervention, ...], bool]:
        existing: dict[str, AuditRecord] = {}
        for audit in audits:
            if audit.action != _INTERVENTION_APPLIED:
                continue
            intervention_id = audit.details.get("intervention_id")
            if isinstance(intervention_id, str):
                existing[intervention_id] = audit
        pending: list[ScheduledIntervention] = []
        duplicate = False
        for intervention in interventions:
            fingerprint = canonical_hash(_public_intervention_data(intervention))
            prior = existing.get(intervention.intervention_id)
            if prior is not None:
                if prior.details.get("public_fingerprint") != fingerprint:
                    raise CommandConflictError(
                        f"intervention_id {intervention.intervention_id!r} was reused"
                    )
                duplicate = True
                continue
            if intervention.at_seconds < current_time:
                raise PastInjectionError(
                    f"new intervention {intervention.intervention_id} targets elapsed time"
                )
            if (
                intervention.kind is InterventionKind.CHECKPOINT
                and intervention.at_seconds == current_time
            ):
                raise BoundaryClosedError(
                    "a checkpoint intervention requires a forward simulated-time boundary"
                )
            pending.append(intervention)
        return tuple(pending), duplicate

    def _preflight_observations(
        self,
        observations: tuple[Observation, ...],
        current_time: int,
    ) -> tuple[tuple[Observation, ...], bool]:
        existing = self._persisted_observations()
        pending: list[Observation] = []
        duplicate = False
        for observation in observations:
            prior = existing.get(observation.observation_id)
            if prior is not None:
                if prior != observation:
                    raise CommandConflictError(
                        f"observation_id {observation.observation_id!r} was reused"
                    )
                duplicate = True
                continue
            if observation.observed_at_seconds < current_time:
                raise PastInjectionError(
                    f"new observation {observation.observation_id} targets elapsed time"
                )
            pending.append(observation)
        return tuple(pending), duplicate

    def _validate_pending_interventions(
        self,
        snapshot: OperationalSnapshot,
        interventions: tuple[ScheduledIntervention, ...],
    ) -> None:
        """Reject the full intervention batch before any boundary can commit."""

        machine_status = {item.machine_id: item.status for item in snapshot.machines}
        catalog_products = {item.product_id for item in self._catalog.products}
        order_ids = {item.order_id for item in snapshot.orders}
        grouped = self._group_by_time(interventions, lambda item: item.at_seconds)
        for at_seconds in sorted(grouped):
            at_boundary = grouped[at_seconds]
            faults = sorted(
                (item for item in at_boundary if item.kind is InterventionKind.MACHINE_FAULT),
                key=lambda item: (item.target_machine_id or "", item.intervention_id),
            )
            restorations = sorted(
                (item for item in at_boundary if item.kind is InterventionKind.MACHINE_RESTORED),
                key=lambda item: (item.target_machine_id or "", item.intervention_id),
            )
            self._reject_ambiguous_machine_transitions(tuple(faults), tuple(restorations))
            for intervention in faults:
                machine_id = intervention.target_machine_id
                assert machine_id is not None
                status = machine_status.get(machine_id)
                if status is None:
                    raise InvalidAdvanceError(f"fault targets unknown machine {machine_id}")
                if status in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
                    raise InvalidAdvanceError(
                        f"machine {machine_id} cannot fault from status {status.value}"
                    )
                machine_status[machine_id] = MachineStatus.DOWN
            for intervention in restorations:
                machine_id = intervention.target_machine_id
                assert machine_id is not None
                status = machine_status.get(machine_id)
                if status is None:
                    raise InvalidAdvanceError(f"restoration targets unknown machine {machine_id}")
                if status not in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
                    raise InvalidAdvanceError(
                        f"machine {machine_id} cannot be restored from status {status.value}"
                    )
                machine_status[machine_id] = MachineStatus.AVAILABLE
            for intervention in sorted(
                (item for item in at_boundary if item.kind is InterventionKind.ADD_ORDER),
                key=lambda item: item.intervention_id,
            ):
                order = intervention.order_to_add
                assert order is not None
                if order.order_id in order_ids:
                    raise InvalidAdvanceError(f"order {order.order_id} is already registered")
                if order.product_id not in catalog_products:
                    raise InvalidAdvanceError(
                        f"order {order.order_id} uses unsupported product {order.product_id}"
                    )
                if order.status is not OrderStatus.PENDING:
                    raise InvalidAdvanceError(
                        f"added order {order.order_id} must have pending status"
                    )
                if order.release_time_seconds > at_seconds:
                    raise InvalidAdvanceError(
                        f"added order {order.order_id} releases after its injection boundary"
                    )
                order_ids.add(order.order_id)

    def _persisted_observations(self) -> dict[str, Observation]:
        result: dict[str, Observation] = {}
        for envelope in self._repository.list_events(self._run_id):
            observation = _event_observation(envelope.event)
            if observation is not None:
                result[observation.observation_id] = observation
        return result

    def _simulation_audits(self) -> tuple[AuditRecord, ...]:
        return tuple(
            item
            for item in self._repository.export_lifecycle(self._run_id).audits
            if item.category == _SIMULATION_AUDIT_CATEGORY
        )

    @staticmethod
    def _find_advance_audit(
        audits: tuple[AuditRecord, ...],
        action: str,
        advance_id: str,
    ) -> AuditRecord | None:
        matches = [
            item
            for item in audits
            if item.action == action and item.details.get("advance_id") == advance_id
        ]
        if len(matches) > 1:
            raise RuntimeStateError(f"advance ledger has duplicate {action} records")
        return matches[0] if matches else None

    def _advance_audit(
        self,
        context: _AdvanceContext,
        action: str,
        at_seconds: int,
    ) -> AuditRecord:
        return AuditRecord(
            audit_id=_stable_identifier(
                "sim-a-",
                {"action": action, "advance_id": context.advance_id},
            ),
            run_id=self._run_id,
            category=_SIMULATION_AUDIT_CATEGORY,
            action=action,
            actor="simulation-runtime",
            details={
                "advance_id": context.advance_id,
                "target_seconds": context.target_seconds,
                "command_fingerprint": context.command_fingerprint,
            },
            simulated_time_seconds=at_seconds,
            recorded_at_utc=_audit_timestamp(at_seconds),
        )

    def _intervention_audit(self, intervention: ScheduledIntervention) -> AuditRecord:
        public_data = _public_intervention_data(intervention)
        # Persist identifiers/fingerprints only.  In particular, no evaluator-only
        # cause reference, unavailable-until prediction, or future intervention list.
        return AuditRecord(
            audit_id=_stable_identifier(
                "sim-i-",
                {"intervention_id": intervention.intervention_id},
            ),
            run_id=self._run_id,
            category=_SIMULATION_AUDIT_CATEGORY,
            action=_INTERVENTION_APPLIED,
            actor="simulation-runtime",
            details={
                "intervention_id": intervention.intervention_id,
                "kind": intervention.kind.value,
                "at_seconds": intervention.at_seconds,
                "public_fingerprint": canonical_hash(public_data),
            },
            simulated_time_seconds=intervention.at_seconds,
            recorded_at_utc=_audit_timestamp(intervention.at_seconds),
        )

    @staticmethod
    def _group_by_time[ItemT](
        items: Sequence[ItemT],
        key: Any,
    ) -> dict[int, tuple[ItemT, ...]]:
        grouped: dict[int, list[ItemT]] = {}
        for item in items:
            grouped.setdefault(key(item), []).append(item)
        return {at: tuple(values) for at, values in grouped.items()}

    @staticmethod
    def _reject_ambiguous_machine_transitions(
        faults: tuple[ScheduledIntervention, ...],
        restorations: tuple[ScheduledIntervention, ...],
    ) -> None:
        fault_ids = [item.target_machine_id for item in faults]
        restoration_ids = [item.target_machine_id for item in restorations]
        if len(fault_ids) != len(set(fault_ids)):
            raise InvalidAdvanceError("one boundary cannot fault a machine more than once")
        if len(restoration_ids) != len(set(restoration_ids)):
            raise InvalidAdvanceError("one boundary cannot restore a machine more than once")

    @staticmethod
    def _require_open_boundary(checkpoint: Any, drafts: Sequence[EventDraft]) -> None:
        if not drafts or checkpoint.last_ordering_key is None:
            return
        first = min(drafts, key=lambda item: EVENT_TYPE_PRIORITY[item.event_type])
        first_key = (
            first.simulated_time_seconds,
            EVENT_TYPE_PRIORITY[first.event_type],
            checkpoint.last_sequence + 1,
        )
        if first_key <= checkpoint.last_ordering_key:
            raise BoundaryClosedError(
                f"simulated second {first.simulated_time_seconds} is already closed; "
                "use a later explicit boundary"
            )

    @staticmethod
    def _blocked_attempts(snapshot: OperationalSnapshot) -> tuple[BlockedAttempt, ...]:
        machine_by_id = {item.machine_id: item for item in snapshot.machines}
        operation_by_id = {item.operation_id: item for item in snapshot.operations}
        completed = {
            item.operation_id
            for item in snapshot.operations
            if item.status is OperationStatus.COMPLETED
        }
        blocked: list[BlockedAttempt] = []
        for attempt in sorted(snapshot.attempts, key=lambda item: item.attempt_id):
            if attempt.status is not AttemptStatus.SCHEDULED or attempt.scheduled_window is None:
                continue
            machine = machine_by_id[attempt.machine_id]
            operation = operation_by_id[attempt.operation_id]
            reason: BlockedReason | None = None
            if machine.status is MachineStatus.DOWN:
                reason = BlockedReason.MACHINE_DOWN
            elif machine.status is MachineStatus.MAINTENANCE:
                reason = BlockedReason.MACHINE_MAINTENANCE
            elif not _attempt_fits_latest_calendar(snapshot, attempt):
                reason = BlockedReason.OUTSIDE_AVAILABILITY_WINDOW
            elif not set(operation.predecessor_operation_ids) <= completed:
                if attempt.scheduled_window.start_seconds <= snapshot.simulated_time_seconds:
                    reason = BlockedReason.PREDECESSOR_INCOMPLETE
            elif machine.status is MachineStatus.RUNNING:
                if attempt.scheduled_window.start_seconds <= snapshot.simulated_time_seconds:
                    reason = BlockedReason.MACHINE_BUSY
            elif attempt.scheduled_window.start_seconds <= snapshot.simulated_time_seconds:
                reason = BlockedReason.MISSED_SCHEDULED_START
            if reason is not None:
                blocked.append(
                    BlockedAttempt(
                        attempt_id=attempt.attempt_id,
                        operation_id=attempt.operation_id,
                        machine_id=attempt.machine_id,
                        scheduled_start_seconds=attempt.scheduled_window.start_seconds,
                        reason=reason,
                    )
                )
        return tuple(blocked)
