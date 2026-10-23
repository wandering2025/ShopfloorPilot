"""S05-A07 persisted-runtime rebuild and post-replay continuation acceptance."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    Capability,
    DomainCatalog,
    EventType,
    Machine,
    OperationalSnapshot,
    OperationTemplate,
    Order,
    ProductTemplate,
    ResourceKind,
    ScheduleAssignment,
    ScheduleObjective,
    TimeWindow,
)
from shopfloor_pilot.events import SnapshotCheckpoint, replay_events
from shopfloor_pilot.planning import expand_process_route
from shopfloor_pilot.simulation import SimulationRuntime
from shopfloor_pilot.storage import SQLiteStateRepository


def _case(run_id: str) -> tuple[DomainCatalog, OperationalSnapshot, tuple[Any, ...]]:
    window = (TimeWindow(start_seconds=0, end_seconds=20),)
    machines = (
        Machine(
            machine_id="mill",
            display_name="Mill",
            kind=ResourceKind.PROCESSING,
            capabilities=(Capability.MILLING,),
            availability_windows=window,
        ),
        Machine(
            machine_id="qc",
            display_name="QC",
            kind=ResourceKind.INSPECTION,
            capabilities=(Capability.INSPECTION,),
            availability_windows=window,
        ),
    )
    product = ProductTemplate(
        product_id="restart-part",
        canonical_name="Restart Test Part",
        route_id="route-restart-part",
        operations=(
            OperationTemplate(
                operation_template_id="restart-mill",
                sequence_index=0,
                name="Mill",
                required_capability=Capability.MILLING,
                duration_seconds=10,
            ),
            OperationTemplate(
                operation_template_id="restart-qc",
                sequence_index=1,
                name="Inspect",
                required_capability=Capability.INSPECTION,
                duration_seconds=2,
            ),
        ),
    )
    catalog = DomainCatalog(schema_version=1, machines=machines, products=(product,))
    order = Order(
        order_id="order-restart",
        product_id=product.product_id,
        quantity=1,
        release_time_seconds=0,
        due_time_seconds=20,
        priority_weight=1,
    )
    expansion = expand_process_route(order, catalog)
    assert len(expansion.operations) == 2
    snapshot = OperationalSnapshot(
        schema_version=1,
        run_id=run_id,
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=0,
        hard_horizon_seconds=20,
        machines=machines,
        orders=(order,),
        operations=(),
        attempts=(),
    )
    return catalog, snapshot, expansion.operations


def _candidate(
    snapshot: OperationalSnapshot,
    operations: tuple[Any, ...],
    suffix: str,
) -> CandidateProposal:
    assignments = (
        ScheduleAssignment(
            operation_id=operations[0].operation_id,
            attempt_id="attempt-restart-mill",
            machine_id="mill",
            window=TimeWindow(start_seconds=0, end_seconds=10),
        ),
        ScheduleAssignment(
            operation_id=operations[1].operation_id,
            attempt_id="attempt-restart-qc",
            machine_id="qc",
            window=TimeWindow(start_seconds=10, end_seconds=12),
        ),
    )
    return CandidateProposal.create(
        schema_version=1,
        proposal_id=f"proposal-{suffix}",
        run_id=snapshot.run_id,
        workflow_id=f"workflow-{suffix}",
        base_state_version=0,
        base_snapshot_hash=canonical_hash(snapshot),
        proposed_plan_revision=1,
        assignments=assignments,
        objective=ScheduleObjective(weighted_total_tardiness_seconds=0, makespan_seconds=12),
    )


def _repository(path: Path, snapshot: OperationalSnapshot) -> SQLiteStateRepository:
    repository = SQLiteStateRepository(path)
    repository.initialize(applied_at_utc="2026-10-20T20:00:00+02:00")
    repository.create_run(snapshot, created_at_utc="2026-10-20T20:00:00+02:00")
    return repository


def _semantic_snapshot(snapshot: OperationalSnapshot) -> dict[str, Any]:
    value = snapshot.model_dump(mode="json", exclude={"state_version", "recorded_at_utc"})
    value["run_id"] = "<run>"
    return value


def _business_events(repository: SQLiteStateRepository, run_id: str) -> tuple[Any, ...]:
    projected = []
    for envelope in repository.list_events(run_id):
        event = envelope.event
        if event.event_type is EventType.TIME_ADVANCED:
            continue
        payload = deepcopy(event.payload)
        if isinstance(payload.get("observation"), dict):
            payload["observation"]["run_id"] = "<run>"
            payload["observation"]["observation_id"] = "<observation>"
        projected.append((event.simulated_time_seconds, event.event_type.value, payload))
    return tuple(projected)


def test_a07_rebuild_at_four_completes_at_absolute_ten_and_can_continue_after_replay(
    tmp_path: Path,
) -> None:
    direct_catalog, direct_initial, direct_operations = _case("run-s05-a07-direct")
    restored_catalog, restored_initial, restored_operations = _case("run-s05-a07-restored")
    direct_repo = _repository(tmp_path / "direct.sqlite3", direct_initial)
    restored_repo = _repository(tmp_path / "restored.sqlite3", restored_initial)

    direct = SimulationRuntime(
        direct_repo,
        direct_initial.run_id,
        direct_catalog,
        seed=211,
    )
    restored = SimulationRuntime(
        restored_repo,
        restored_initial.run_id,
        restored_catalog,
        seed=211,
    )
    direct.activate_plan(
        _candidate(direct_initial, direct_operations, "direct"),
        idempotency_key="activate-a07",
    )
    restored.activate_plan(
        _candidate(restored_initial, restored_operations, "restored"),
        idempotency_key="activate-a07",
    )

    direct_at_ten = direct.advance_to(10, advance_id="advance-a07-10").snapshot
    restored.advance_to(4, advance_id="advance-a07-4")
    # No SimPy Environment, Process, callback, or generator object is serialized.
    del restored
    restored = SimulationRuntime.rebuild(
        restored_repo,
        restored_initial.run_id,
        restored_catalog,
        seed=211,
    )
    restored_at_ten = restored.advance_to(10, advance_id="advance-a07-10").snapshot

    direct_mill = next(
        item for item in direct_at_ten.attempts if item.attempt_id == "attempt-restart-mill"
    )
    restored_mill = next(
        item for item in restored_at_ten.attempts if item.attempt_id == "attempt-restart-mill"
    )
    assert direct_mill.status is restored_mill.status is AttemptStatus.COMPLETED
    assert (direct_mill.actual_start_seconds, direct_mill.actual_end_seconds) == (0, 10)
    assert (restored_mill.actual_start_seconds, restored_mill.actual_end_seconds) == (0, 10)
    assert _semantic_snapshot(direct_at_ten) == _semantic_snapshot(restored_at_ten)
    assert _business_events(direct_repo, direct_initial.run_id) == _business_events(
        restored_repo, restored_initial.run_id
    )

    exact_replay = replay_events(
        SnapshotCheckpoint.create(restored_initial),
        restored_repo.list_events(restored_initial.run_id),
    )
    assert exact_replay == restored_repo.get_checkpoint(restored_initial.run_id)
    assert exact_replay.snapshot == restored_at_ten

    direct_final = direct.advance_to(12, advance_id="advance-a07-12").snapshot
    restored_final = restored.advance_to(12, advance_id="advance-a07-12").snapshot
    assert _semantic_snapshot(direct_final) == _semantic_snapshot(restored_final)
    assert _business_events(direct_repo, direct_initial.run_id) == _business_events(
        restored_repo, restored_initial.run_id
    )
    assert all(item.status is AttemptStatus.COMPLETED for item in restored_final.attempts)

    continued_replay = replay_events(
        SnapshotCheckpoint.create(restored_initial),
        restored_repo.list_events(restored_initial.run_id),
    )
    assert continued_replay == restored_repo.get_checkpoint(restored_initial.run_id)
    assert continued_replay.snapshot == restored_final
