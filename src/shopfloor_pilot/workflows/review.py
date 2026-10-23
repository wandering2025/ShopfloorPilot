"""Prediction-only review content shown to a human before approval.

Soft due-date misses are surfaced as explicit warnings on the exact stored
proposal. They never make a candidate infeasible and are never silently dropped.
"""

from __future__ import annotations

from typing import Any

from shopfloor_pilot.metrics import MetricBasis, forecast_order_outcomes
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import AuditRecord

from .models import DecisionRecord, DecisionStatus


def due_date_warnings(
    services: ApplicationServices, run_id: str, proposal_id: str, proposal_hash: str
) -> list[dict[str, Any]]:
    """Orders whose predicted completion under the proposal misses the soft due date."""

    projected, _validation = services.preview_application(run_id, proposal_id, proposal_hash)
    warnings: list[dict[str, Any]] = []
    for order in forecast_order_outcomes(projected):
        completion = (
            order.actual_completion_seconds
            if order.actual_basis is MetricBasis.ACTUAL
            else order.predicted_completion_seconds
        )
        if completion is None or completion <= order.due_time_seconds:
            continue
        warnings.append(
            {
                "code": "soft_due_date_miss",
                "order_id": order.order_id,
                "due_time_seconds": order.due_time_seconds,
                "predicted_completion_seconds": completion,
                "priority_weight": order.priority_weight,
                "predicted_weighted_tardiness_seconds": order.priority_weight
                * (completion - order.due_time_seconds),
                "basis": "predicted",
                "hard_constraint": False,
            }
        )
    return warnings


def _latest_supervisor_audit(
    services: ApplicationServices, decision: DecisionRecord
) -> AuditRecord | None:
    audits = [
        item
        for item in services.export_lifecycle(decision.run_id).audits
        if item.action == "supervisor_outcome_recorded"
        and item.details.get("proposal_id") == decision.pending_proposal_id
    ]
    return audits[-1] if audits else None


def approval_request(services: ApplicationServices, decision: DecisionRecord) -> dict[str, Any]:
    """Rebuildable after restart: everything comes from SQLite records."""

    if decision.status is not DecisionStatus.AWAITING_APPROVAL:
        raise ValueError("only an awaiting decision has an approval request")
    assert decision.pending_proposal_id is not None and decision.pending_proposal_hash is not None
    proposal = next(
        item
        for item in services.export_lifecycle(decision.run_id).proposals
        if item.proposal_id == decision.pending_proposal_id
    )
    report = services.inspect_conflict(
        decision.run_id, proposal.proposal_id, decision.pending_proposal_hash
    )
    audit = _latest_supervisor_audit(services, decision)
    return {
        "schema_version": 1,
        "action_required": "explicit human approval outside the supervisor",
        "run_id": decision.run_id,
        "workflow_id": decision.workflow_id,
        "decision_kind": decision.kind.value,
        "source": decision.source.value,
        "provider": decision.provider,
        "evidence_label": None if audit is None else audit.details.get("evidence_label"),
        "decision_time_seconds": decision.decision_time_seconds,
        "decision_boundary_seconds": decision.decision_boundary_seconds,
        "proposal_id": proposal.proposal_id,
        "proposal_hash": proposal.proposal_hash,
        "base_state_version": proposal.base_state_version,
        "proposed_plan_revision": proposal.proposed_plan_revision,
        "objective": proposal.objective.model_dump(mode="json"),
        "order_deltas": [item.model_dump(mode="json") for item in proposal.proposed_order_deltas],
        "resource_deltas": [
            item.model_dump(mode="json") for item in proposal.proposed_resource_deltas
        ],
        "due_date_warnings": due_date_warnings(
            services, decision.run_id, proposal.proposal_id, proposal.proposal_hash
        ),
        "claims": [] if audit is None else audit.details.get("claims", []),
        "repair_recommendation": None
        if audit is None
        else audit.details.get("repair_recommendation"),
        "currently_applicable": report.can_apply,
        "conflict_reasons": [item.value for item in report.reasons],
    }


__all__ = ["approval_request", "due_date_warnings"]
