"""S05-A10 public telemetry and private scenario-data boundary acceptance."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from shopfloor_pilot.domain.fixtures import InterventionKind, ScheduledIntervention
from shopfloor_pilot.domain.models import (
    AttemptStatus,
    CandidateProposal,
    Capability,
    DomainCatalog,
    EventType,
    Machine,
    MachineStatus,
    Observation,
    ResourceKind,
    TelemetryValue,
    TimeWindow,
)
from shopfloor_pilot.simulation import InvalidAdvanceError, SimulationRuntime
from shopfloor_pilot.storage import SQLiteStateRepository


def test_a10_private_truth_and_future_schedule_never_enter_public_or_event_json(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    private_sentinel = "EVAL_ONLY_SENTINEL_SPINDLE_BEARING_DAMAGE"
    cell = tiny_cell_factory(
        quantity=1,
        milling_seconds=10,
        inspection_seconds=2,
        horizon=25,
    )
    repository = repository_factory(cell.snapshot, "a10")
    runtime = SimulationRuntime(repository, cell.snapshot.run_id, cell.catalog, seed=991)
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 0, 10),
            (cell.operation(1, 1), "qc", 10, 12),
        ),
        proposal_id="proposal-a10",
        makespan=12,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a10")
    fault = ScheduledIntervention(
        intervention_id="fault-a10-private",
        at_seconds=4,
        kind=InterventionKind.MACHINE_FAULT,
        target_machine_id="mill-a",
        injected_status=MachineStatus.DOWN,
        unavailable_until_seconds=19,
        private_truth_ref=private_sentinel,
    )
    result = runtime.advance_to(
        4,
        advance_id="advance-a10-fault",
        interventions=(fault,),
    )

    persisted_events = tuple(
        envelope.event.model_dump(mode="json")
        for envelope in repository.list_events(cell.snapshot.run_id)
    )
    public_projection = result.projection.model_dump(mode="json")
    serialized_events = json.dumps(persisted_events, sort_keys=True)
    serialized_public = json.dumps(public_projection, sort_keys=True)
    for serialized in (serialized_events, serialized_public):
        assert private_sentinel not in serialized
        assert "private_truth_ref" not in serialized
        assert "unavailable_until_seconds" not in serialized

    observation_event = next(
        envelope.event
        for envelope in repository.list_events(cell.snapshot.run_id)
        if envelope.event.event_type is EventType.OBSERVATION_RECORDED
        and envelope.event.simulated_time_seconds == 4
    )
    observation = observation_event.payload["observation"]
    assert observation["observation_id"]
    assert observation["machine_id"] == "mill-a"
    assert observation["observed_at_seconds"] == 4
    assert observation["signals"]
    assert all(
        set(signal) >= {"name", "value", "unit"} and signal["unit"]
        for signal in observation["signals"]
    )
    assert observation["observation_id"] in serialized_public
    assert "synthetic" in observation["summary"].lower()


def test_a10_invalid_late_observation_is_preflighted_before_any_callback_commits(
    tiny_cell_factory: Callable[..., Any],
    candidate_factory: Callable[..., CandidateProposal],
    repository_factory: Callable[..., SQLiteStateRepository],
) -> None:
    cell = tiny_cell_factory(quantity=1, horizon=20, run_id="run-s05-a10-preflight")
    catalog_only_machine = Machine(
        machine_id="catalog-only-machine",
        display_name="Catalog Only Machine",
        kind=ResourceKind.PROCESSING,
        capabilities=(Capability.MILLING,),
        availability_windows=(TimeWindow(start_seconds=0, end_seconds=20),),
    )
    expanded_catalog = DomainCatalog(
        schema_version=1,
        machines=(*cell.catalog.machines, catalog_only_machine),
        products=cell.catalog.products,
    )
    # SQLite is authoritative: the deliberately divergent snapshot does not contain
    # catalog-only-machine, even though the static catalog does.
    assert all(
        item.machine_id != catalog_only_machine.machine_id for item in cell.snapshot.machines
    )
    repository = repository_factory(cell.snapshot, "a10-preflight")
    runtime = SimulationRuntime(
        repository,
        cell.snapshot.run_id,
        expanded_catalog,
        seed=29,
    )
    candidate = candidate_factory(
        cell.snapshot,
        (
            (cell.operation(1, 0), "mill-a", 1, 4),
            (cell.operation(1, 1), "qc", 4, 6),
        ),
        proposal_id="proposal-a10-preflight",
        makespan=6,
    )
    runtime.activate_plan(candidate, idempotency_key="activate-a10-preflight")
    checkpoint_before = repository.get_checkpoint(cell.snapshot.run_id)
    events_before = repository.list_events(cell.snapshot.run_id)
    invalid_observation = Observation(
        schema_version=1,
        observation_id="observation-a10-catalog-only",
        run_id=cell.snapshot.run_id,
        machine_id=catalog_only_machine.machine_id,
        observed_at_seconds=5,
        signals=(TelemetryValue(name="temperature", value=49000, unit="mdegC"),),
        summary="Synthetic observation for an absent authoritative machine.",
    )

    # Without whole-command preflight, callbacks at t=1 and t=4 would commit before
    # the unknown-machine observation failed at t=5, leaving forbidden partial work.
    with pytest.raises(InvalidAdvanceError, match="unknown machine"):
        runtime.advance_to(
            5,
            advance_id="advance-a10-invalid-observation",
            observations=(invalid_observation,),
        )

    assert repository.get_checkpoint(cell.snapshot.run_id) == checkpoint_before
    assert repository.list_events(cell.snapshot.run_id) == events_before
    assert runtime.snapshot == checkpoint_before.snapshot
    scheduled = next(
        item
        for item in runtime.snapshot.attempts
        if item.operation_id == cell.operation(1, 0).operation_id
    )
    assert scheduled.status is AttemptStatus.SCHEDULED
    assert not any(
        envelope.event.event_type is EventType.OPERATION_STARTED
        for envelope in repository.list_events(cell.snapshot.run_id)
    )
