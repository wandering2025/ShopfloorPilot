"""S03-A07 through A10 provenance, deltas, independence, and purity acceptance."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import (
    CandidateProposal,
    MachineStatus,
    OrderDelta,
    OrderDeltaKind,
    ResourceDelta,
    ResourceDeltaKind,
)
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.validation import ViolationCode, validate_candidate, validate_schedule


def _copy[ModelT](model: ModelT, **updates: Any) -> ModelT:
    data = model.model_dump(mode="python")  # type: ignore[attr-defined]
    data.update(updates)
    return type(model).model_validate(data)


def _codes(report: Any) -> tuple[ViolationCode, ...]:
    return tuple(item.code for item in report.violations)


def _candidate(case: Any, **updates: Any) -> CandidateProposal:
    original = case.candidate
    values: dict[str, Any] = {
        "schema_version": original.schema_version,
        "proposal_id": original.proposal_id,
        "run_id": original.run_id,
        "workflow_id": original.workflow_id,
        "base_state_version": original.base_state_version,
        "base_snapshot_hash": original.base_snapshot_hash,
        "proposed_plan_revision": original.proposed_plan_revision,
        "assignments": original.assignments,
        "objective": original.objective,
        "proposed_order_deltas": original.proposed_order_deltas,
        "proposed_resource_deltas": original.proposed_resource_deltas,
        "created_at_utc": original.created_at_utc,
    }
    values.update(updates)
    return CandidateProposal.create(**values)


def test_a07_candidate_run_version_snapshot_revision_and_content_hash_are_bound(
    legal_case: Any,
) -> None:
    cases = (
        (
            _candidate(legal_case, run_id="run-s03-other"),
            ViolationCode.RUN_ID_MISMATCH,
        ),
        (
            _candidate(
                legal_case,
                base_state_version=legal_case.snapshot.state_version + 1,
            ),
            ViolationCode.BASE_STATE_VERSION_MISMATCH,
        ),
        (
            _candidate(legal_case, base_snapshot_hash="0" * 64),
            ViolationCode.BASE_SNAPSHOT_HASH_MISMATCH,
        ),
        (
            _candidate(
                legal_case,
                proposed_plan_revision=legal_case.snapshot.plan_revision + 2,
            ),
            ViolationCode.PLAN_REVISION_MISMATCH,
        ),
    )
    before = canonical_hash(legal_case.snapshot)
    for candidate, expected_code in cases:
        report = validate_candidate(
            snapshot=legal_case.snapshot,
            catalog=legal_case.catalog,
            candidate=candidate,
        )
        assert not report.is_valid
        assert expected_code in _codes(report)

    tampered = legal_case.candidate.model_copy(
        update={
            "objective": legal_case.candidate.objective.model_copy(
                update={"makespan_seconds": 2399}
            )
        }
    )
    tampered_report = validate_candidate(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        candidate=tampered,
    )
    assert ViolationCode.PROPOSAL_HASH_MISMATCH in _codes(tampered_report)
    assert canonical_hash(legal_case.snapshot) == before


def test_a08_exact_urgent_addition_is_projected_but_candidate_declaration_is_not_authority(
    urgent_case: Any,
) -> None:
    exact = validate_candidate(
        snapshot=urgent_case.snapshot,
        catalog=urgent_case.catalog,
        candidate=urgent_case.candidate,
        projected_order_deltas=urgent_case.order_deltas,
    )
    undeclared = validate_candidate(
        snapshot=urgent_case.snapshot,
        catalog=urgent_case.catalog,
        candidate=urgent_case.candidate,
    )

    assert exact.is_valid
    assert len(exact.projected_operations) == 3
    assert exact.total_tardiness_seconds == 3000
    assert ViolationCode.ORDER_DELTA_MISMATCH in _codes(undeclared)


def test_a08_unrelated_order_replacement_and_candidate_driven_restore_are_rejected(
    legal_case: Any,
    urgent_case: Any,
) -> None:
    existing = legal_case.snapshot.orders[0]
    changed = _copy(existing, priority_weight=existing.priority_weight + 1)
    replacement = OrderDelta(kind=OrderDeltaKind.REPLACE, order=changed)
    replace_candidate = _candidate(
        legal_case,
        proposed_order_deltas=(replacement,),
    )
    replace_report = validate_candidate(
        snapshot=legal_case.snapshot,
        catalog=legal_case.catalog,
        candidate=replace_candidate,
        projected_order_deltas=(replacement,),
    )

    down_machines = tuple(
        _copy(machine, status=MachineStatus.DOWN) if machine.machine_id == "CNC02" else machine
        for machine in urgent_case.snapshot.machines
    )
    down_snapshot = _copy(urgent_case.snapshot, machines=down_machines)
    restore = ResourceDelta(
        kind=ResourceDeltaKind.RESTORE,
        machine_id="CNC02",
        effective_time_seconds=down_snapshot.simulated_time_seconds,
        new_status=MachineStatus.AVAILABLE,
    )
    restore_candidate = CandidateProposal.create(
        schema_version=1,
        proposal_id="proposal-illegal-restore",
        run_id=down_snapshot.run_id,
        workflow_id="workflow-illegal-restore",
        base_state_version=down_snapshot.state_version,
        base_snapshot_hash=canonical_hash(down_snapshot),
        proposed_plan_revision=down_snapshot.plan_revision + 1,
        assignments=urgent_case.assignments,
        objective=urgent_case.candidate.objective,
        proposed_order_deltas=urgent_case.order_deltas,
        proposed_resource_deltas=(restore,),
    )
    restore_report = validate_candidate(
        snapshot=down_snapshot,
        catalog=urgent_case.catalog,
        candidate=restore_candidate,
        projected_order_deltas=urgent_case.order_deltas,
        projected_resource_deltas=(restore,),
    )

    assert ViolationCode.INVALID_ORDER_DELTA in _codes(replace_report)
    assert ViolationCode.RESOURCE_RESTORE_FORBIDDEN in _codes(restore_report)


def test_a08_declared_machine_down_is_supported_and_validation_never_mutates_sqlite(
    urgent_case: Any,
    tmp_path: Path,
) -> None:
    mark_down = ResourceDelta(
        kind=ResourceDeltaKind.MARK_DOWN,
        machine_id="CNC02",
        effective_time_seconds=urgent_case.snapshot.simulated_time_seconds,
        new_status=MachineStatus.DOWN,
    )
    candidate = _candidate(
        urgent_case,
        proposed_resource_deltas=(mark_down,),
    )
    repository = SQLiteStateRepository(tmp_path / "validator-purity.db")
    repository.initialize(applied_at_utc="2026-10-20T19:00:00+02:00")
    repository.create_run(
        urgent_case.snapshot,
        created_at_utc="2026-10-20T19:00:01+02:00",
    )
    before_checkpoint = repository.get_checkpoint(urgent_case.snapshot.run_id)
    before_events = repository.list_events(urgent_case.snapshot.run_id)

    report = validate_candidate(
        snapshot=before_checkpoint.snapshot,
        catalog=urgent_case.catalog,
        candidate=candidate,
        projected_order_deltas=urgent_case.order_deltas,
        projected_resource_deltas=(mark_down,),
    )
    reopened = SQLiteStateRepository(repository.database_path)
    after_checkpoint = reopened.get_checkpoint(urgent_case.snapshot.run_id)
    after_events = reopened.list_events(urgent_case.snapshot.run_id)

    assert report.is_valid
    assert after_checkpoint == before_checkpoint
    assert after_events == before_events == ()


def test_a08_running_machine_cannot_be_declared_down_without_interruption_history(
    history_case: Any,
) -> None:
    mark_down = ResourceDelta(
        kind=ResourceDeltaKind.MARK_DOWN,
        machine_id="CNC01",
        effective_time_seconds=history_case.snapshot.simulated_time_seconds,
        new_status=MachineStatus.DOWN,
    )
    report = validate_schedule(
        snapshot=history_case.snapshot,
        catalog=history_case.catalog,
        assignments=history_case.assignments,
        projected_resource_deltas=(mark_down,),
    )

    assert ViolationCode.INVALID_RESOURCE_DELTA in _codes(report)
    violation = next(
        item for item in report.violations if item.code is ViolationCode.INVALID_RESOURCE_DELTA
    )
    assert violation.machine_ids == ("CNC01",)
    assert violation.references["current_status"] == MachineStatus.RUNNING.value


def test_a09_validator_source_and_runtime_do_not_import_solver() -> None:
    validation_root = Path(__file__).resolve().parents[2] / "src" / "shopfloor_pilot" / "validation"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(validation_root.glob("*.py"))
    )
    forbidden = ("ortools", "cp_model", "shopfloor_pilot.scheduling")
    assert not any(token in source for token in forbidden)
    hand_built_test = Path(__file__).resolve().with_name("test_schedule_constraints.py")
    test_id = f"{hand_built_test}::test_a03_hand_calculated_adjacent_tardy_schedule_is_valid"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pytest; "
            f"status = pytest.main([{test_id!r}, '-q', '-p', 'no:cacheprovider']); "
            "assert status == pytest.ExitCode.OK; "
            "assert not any(n == 'ortools' or n.startswith('ortools.') "
            "for n in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_a10_repeated_validation_is_deterministic_and_inputs_are_unchanged(
    legal_case: Any,
) -> None:
    snapshot_before = canonical_hash(legal_case.snapshot)
    candidate_before = canonical_hash(legal_case.candidate)
    catalog_before = canonical_hash(legal_case.catalog)
    reports = tuple(
        validate_candidate(
            snapshot=legal_case.snapshot,
            catalog=legal_case.catalog,
            candidate=legal_case.candidate,
        )
        for _ in range(5)
    )
    invalid_reports = tuple(
        validate_schedule(
            snapshot=legal_case.snapshot,
            catalog=legal_case.catalog,
            assignments=legal_case.assignments[1:],
        )
        for _ in range(5)
    )

    assert all(report == reports[0] for report in reports)
    assert all(report == invalid_reports[0] for report in invalid_reports)
    assert tuple(item.code for item in invalid_reports[0].violations) == tuple(
        sorted(item.code for item in invalid_reports[0].violations)
    )
    assert canonical_hash(legal_case.snapshot) == snapshot_before
    assert canonical_hash(legal_case.candidate) == candidate_before
    assert canonical_hash(legal_case.catalog) == catalog_before


def test_a10_repeated_validation_leaves_persisted_state_unchanged(
    legal_case: Any,
    tmp_path: Path,
) -> None:
    repository = SQLiteStateRepository(tmp_path / "repeated-validator-purity.db")
    repository.initialize(applied_at_utc="2026-10-20T19:20:00+02:00")
    repository.create_run(
        legal_case.snapshot,
        created_at_utc="2026-10-20T19:20:01+02:00",
    )
    before_checkpoint = repository.get_checkpoint(legal_case.snapshot.run_id)
    before_events = repository.list_events(legal_case.snapshot.run_id)

    reports = tuple(
        validate_candidate(
            snapshot=before_checkpoint.snapshot,
            catalog=legal_case.catalog,
            candidate=legal_case.candidate,
        )
        for _ in range(5)
    )
    reopened = SQLiteStateRepository(repository.database_path)

    assert all(report.is_valid and report == reports[0] for report in reports)
    assert reopened.get_checkpoint(legal_case.snapshot.run_id) == before_checkpoint
    assert reopened.list_events(legal_case.snapshot.run_id) == before_events == ()
