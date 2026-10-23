"""Read-only proposal-status projection of a lifecycle export.

Portable operational replay deliberately carries no lifecycle records. This view
is the separate display/audit export: it states each proposal's status and its
approval history and marks every record non-actionable. Nothing here can be fed
back into approval or application.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from shopfloor_pilot.storage import LifecycleExport, WorkflowStatus


def _proposal_status(workflow: Any, proposal_id: str, consumed: set[str]) -> str:
    if proposal_id in consumed:
        return "applied"
    if workflow is None:
        return "unbound"
    if workflow.status is WorkflowStatus.WAITING_APPROVAL:
        return "awaiting_approval"
    if workflow.status is WorkflowStatus.CANCELLED:
        outcome = workflow.payload.get("outcome")
        return str(outcome) if isinstance(outcome, str) else "cancelled"
    if workflow.status is WorkflowStatus.FAILED:
        return "blocked"
    return workflow.status.value


def lifecycle_view(lifecycle: LifecycleExport | Mapping[str, Any]) -> dict[str, Any]:
    export = (
        lifecycle
        if isinstance(lifecycle, LifecycleExport)
        else LifecycleExport.model_validate_json(json.dumps(dict(lifecycle)))
    )
    workflows = {item.workflow_id: item for item in export.workflows}
    consumed = {item.proposal_id for item in export.approval_consumptions}
    consumption_by_approval = {item.approval_id: item for item in export.approval_consumptions}
    proposals = []
    for proposal in export.proposals:
        workflow = workflows.get(proposal.workflow_id)
        proposals.append(
            {
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.proposal_hash,
                "workflow_id": proposal.workflow_id,
                "base_state_version": proposal.base_state_version,
                "proposed_plan_revision": proposal.proposed_plan_revision,
                "status": _proposal_status(workflow, proposal.proposal_id, consumed),
                "approvals": [
                    {
                        "approval_id": item.approval_id,
                        "decision": item.decision.value,
                        "actor": item.actor,
                        "consumed_by_mutation": (
                            consumption_by_approval[item.approval_id].mutation_id
                            if item.approval_id in consumption_by_approval
                            else None
                        ),
                    }
                    for item in export.approvals
                    if item.proposal_id == proposal.proposal_id
                ],
            }
        )
    return {
        "schema_version": 1,
        "view": "lifecycle_status",
        "run_id": export.run_id,
        "authority": "historical display only; no record here authorizes a live action",
        "actionable": False,
        "proposals": proposals,
        "workflows": [
            {
                "workflow_id": item.workflow_id,
                "status": item.status.value,
                "outcome": item.payload.get("outcome"),
                "reason_code": item.payload.get("reason_code"),
                "source": item.payload.get("source"),
            }
            for item in export.workflows
        ],
        "audit_actions": [
            {"action": item.action, "actor": item.actor, "category": item.category}
            for item in export.audits
        ],
    }


__all__ = ["lifecycle_view"]
