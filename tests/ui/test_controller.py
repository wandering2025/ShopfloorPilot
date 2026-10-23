from __future__ import annotations

from pathlib import Path

import pytest

from shopfloor_pilot.domain import ApprovalDecision, AttemptStatus, MachineStatus
from shopfloor_pilot.services import ApplicationServiceError, ServiceErrorCode
from shopfloor_pilot.storage import WorkflowStatus
from shopfloor_pilot.ui import DashboardController, DashboardInputError
from shopfloor_pilot.ui.presenters import future_attempt_options, proposal_change_rows


def _apply_pending(controller: DashboardController) -> None:
    pending = controller.view().pending
    assert pending is not None
    approval = controller.decide(
        pending.candidate.proposal_id,
        pending.candidate.proposal_hash,
        ApprovalDecision.APPROVED,
    )
    controller.apply_approved(
        pending.candidate.proposal_id,
        pending.candidate.proposal_hash,
        approval.approval_id,
    )


def test_open_refresh_and_reset_share_authority_without_implicit_actions(
    controller: DashboardController,
) -> None:
    first = controller.ensure_active()
    original = first.snapshot

    assert controller.ensure_active().snapshot == original
    assert controller.pause() == original

    second_session = DashboardController(controller.runtime_root)
    assert second_session.ensure_active().context == first.context
    assert second_session.view().snapshot == original

    reset = controller.start_or_reset("urgent_order")

    assert reset.context.run_id != first.context.run_id
    assert reset.context.generation == first.context.generation + 1
    assert reset.snapshot.state_version == 0
    assert reset.snapshot.plan_revision == 0
    assert second_session.view().context == reset.context
    assert controller.repository.get_run(reset.context.run_id).reset_from_run_id == (
        first.context.run_id
    )
    assert controller.repository.get_snapshot(first.context.run_id) == original

    proposal = controller.propose_plan()
    assert proposal.result.candidate is not None
    assert proposal.result.candidate.run_id == reset.context.run_id


def test_normal_plan_decision_apply_advance_and_idempotent_retry(
    controller: DashboardController,
) -> None:
    initial = controller.ensure_active()
    action = controller.propose_plan()
    candidate = action.result.candidate

    assert candidate is not None
    assert action.result.validation is not None and action.result.validation.is_valid
    assert controller.view().snapshot == initial.snapshot

    first_validation = controller.validate_pending(candidate.proposal_id, candidate.proposal_hash)
    second_validation = controller.validate_pending(candidate.proposal_id, candidate.proposal_hash)
    assert first_validation == second_validation
    assert first_validation.can_apply
    assert controller.view().snapshot.state_version == 0

    approval = controller.decide(
        candidate.proposal_id,
        candidate.proposal_hash,
        ApprovalDecision.APPROVED,
    )
    assert controller.view().snapshot == initial.snapshot

    applied = controller.apply_approved(
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
    )
    after_apply = controller.view()
    retry = controller.apply_approved(
        candidate.proposal_id,
        candidate.proposal_hash,
        approval.approval_id,
    )

    assert applied.status.value == "applied"
    assert retry.status.value == "idempotent"
    assert after_apply.snapshot.state_version == 1
    assert after_apply.snapshot.plan_revision == 1
    assert controller.view().snapshot == after_apply.snapshot

    advanced = controller.advance_by(900)
    after_advance = controller.view()

    assert advanced.resulting_time_seconds == 900
    assert after_advance.snapshot.simulated_time_seconds == 900
    assert after_advance.snapshot.state_version > after_apply.snapshot.state_version
    assert DashboardController(controller.runtime_root).view().snapshot == after_advance.snapshot


@pytest.mark.parametrize(
    "order_payload, expected_code",
    [
        (
            {
                "order_id": "bad id",
                "product_id": "PRODUCT-A",
                "quantity": 1,
                "release_time_seconds": 0,
                "due_time_seconds": 100,
                "priority_weight": 1,
            },
            "invalid_order",
        ),
        (
            {
                "order_id": "ORD-NEW",
                "product_id": "UNSUPPORTED",
                "quantity": 1,
                "release_time_seconds": 0,
                "due_time_seconds": 100,
                "priority_weight": 1,
            },
            "invalid_order",
        ),
    ],
)
def test_malformed_order_is_inline_safe_and_state_is_unchanged(
    controller: DashboardController,
    order_payload: dict[str, object],
    expected_code: str,
) -> None:
    before = controller.ensure_active()

    with pytest.raises(DashboardInputError) as raised:
        controller.propose_order(order_payload)

    after = controller.view()
    assert raised.value.code == expected_code
    assert after.snapshot == before.snapshot
    assert after.lifecycle == before.lifecycle


def test_urgent_order_fault_replan_and_rejection_preserve_authority(
    controller: DashboardController,
) -> None:
    controller.ensure_active()
    controller.propose_plan()
    _apply_pending(controller)
    before_urgent = controller.view()

    action = controller.propose_order(
        {
            "order_id": "ORD-URGENT-UI",
            "product_id": before_urgent.bundle.catalog.products[0].product_id,
            "quantity": 1,
            "release_time_seconds": before_urgent.snapshot.simulated_time_seconds,
            "due_time_seconds": 9000,
            "priority_weight": 50,
        }
    )
    assert action.result.candidate is not None
    assert all(item.order_id != "ORD-URGENT-UI" for item in controller.view().snapshot.orders)

    _apply_pending(controller)
    urgent_applied = controller.view()
    assert any(item.order_id == "ORD-URGENT-UI" for item in urgent_applied.snapshot.orders)
    assert urgent_applied.snapshot.plan_revision == 2

    option = future_attempt_options(urgent_applied.snapshot)[0]
    controller.inject_fault(
        str(option["machine_id"]),
        int(option["suggested_fault_seconds"]),
        unavailable_until_seconds=min(
            urgent_applied.snapshot.hard_horizon_seconds,
            int(option["suggested_fault_seconds"]) + 1200,
        ),
    )
    faulted = controller.view()
    assert any(item.status is MachineStatus.DOWN for item in faulted.snapshot.machines)
    assert any(item.status is AttemptStatus.INTERRUPTED for item in faulted.snapshot.attempts)

    recovery = controller.propose_plan(purpose="recovery")
    assert recovery.result.candidate is not None
    pending = controller.view().pending
    assert pending is not None
    assert any(
        row["Changed"] == "yes" for row in proposal_change_rows(faulted.snapshot, pending.candidate)
    )

    before_reject = controller.view().snapshot
    rejection = controller.decide(
        pending.candidate.proposal_id,
        pending.candidate.proposal_hash,
        ApprovalDecision.REJECTED,
    )
    assert controller.view().snapshot == before_reject
    with pytest.raises(ApplicationServiceError) as rejected:
        controller.apply_approved(
            pending.candidate.proposal_id,
            pending.candidate.proposal_hash,
            rejection.approval_id,
        )
    assert rejected.value.code is ServiceErrorCode.APPROVAL_REJECTED
    assert controller.view().snapshot == before_reject


def test_no_feasible_recovery_is_explicit_and_non_operational(
    controller: DashboardController,
) -> None:
    controller.ensure_active()
    controller.start_or_reset("no_feasible_recovery")
    controller.propose_plan()
    _apply_pending(controller)
    controller.inject_fixture_disruption()
    disrupted = controller.view()

    action = controller.propose_plan(purpose="recovery")
    after = controller.view()

    assert action.result.outcome.value == "infeasible"
    assert action.result.candidate is None
    assert after.snapshot == disrupted.snapshot
    assert after.pending is None
    assert after.lifecycle.workflows[-1].status is WorkflowStatus.FAILED
    assert after.lifecycle.workflows[-1].payload["reason_code"] == "scheduling_infeasible"


def test_stale_approved_proposal_is_rejected_without_replacement(
    controller: DashboardController,
) -> None:
    controller.ensure_active()
    controller.propose_plan()
    _apply_pending(controller)
    controller.propose_plan(purpose="replan")
    pending = controller.view().pending
    assert pending is not None
    approval = controller.decide(
        pending.candidate.proposal_id,
        pending.candidate.proposal_hash,
        ApprovalDecision.APPROVED,
    )

    other_session = DashboardController(controller.runtime_root)
    other_session.advance_by(1)
    authoritative = controller.view().snapshot

    with pytest.raises(ApplicationServiceError) as stale:
        controller.apply_approved(
            pending.candidate.proposal_id,
            pending.candidate.proposal_hash,
            approval.approval_id,
        )

    assert stale.value.code is ServiceErrorCode.STALE_STATE
    assert controller.view().snapshot == authoritative
    assert controller.view().pending is not None
    assert not controller.view().pending.conflict.can_apply


def test_exported_replay_isolated_from_live_state(controller: DashboardController) -> None:
    controller.ensure_active()
    controller.propose_plan()
    _apply_pending(controller)
    controller.advance_by(1200)
    before = controller.view().snapshot

    path = controller.export_replay()
    replay = controller.load_replay(path)
    after = controller.view().snapshot

    assert isinstance(path, Path) and path.is_file()
    assert replay.checkpoint.snapshot == before
    assert replay.actual_metrics == controller.view().metrics
    assert after == before
