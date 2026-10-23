"""S10-A11 scripted determinism and the opt-in S10-A10 live DeepSeek gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from agent_support import fixtures_by_id, fresh_run, run_supervisor

from shopfloor_pilot.agent.harness import (
    ABNORMALITY_TASKS,
    run_agent_scenario,
    scripted_abnormality_turns,
    scripted_order_turns,
)
from shopfloor_pilot.agent.trace import normalize_for_comparison
from shopfloor_pilot.agent.workflow import SupervisorTask
from shopfloor_pilot.tools import WorkflowKind, WorkflowPhase


def _scripted(tmp: Path, scenario: str):
    handle = fresh_run(tmp, scenario)
    if scenario == "machine_breakdown":
        text, machines, query = ABNORMALITY_TASKS[scenario]
        task = SupervisorTask(WorkflowKind.ABNORMALITY, text, machines)
        turns = scripted_abnormality_turns(query)
    else:
        fixture = fixtures_by_id()["urgent-zh-plate-90min"]
        task = SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text)
        turns = scripted_order_turns(fixture.scripted_draft)
    return handle, run_supervisor(handle, task, turns)


@pytest.mark.parametrize("scenario", ["urgent_order", "machine_breakdown"])
def test_a11_identical_scripted_runs_match_after_normalization(
    tmp_path: Path, scenario: str
) -> None:
    first_handle, first = _scripted(tmp_path / "a", scenario)
    second_handle, second = _scripted(tmp_path / "b", scenario)
    assert first.outcome.terminal is WorkflowPhase.AWAITING_APPROVAL
    assert normalize_for_comparison(first.trace_view) == normalize_for_comparison(second.trace_view)
    assert first.outcome.proposal == second.outcome.proposal
    stored = [
        handle.services.repository.get_proposal(
            handle.run_id, run.outcome.proposal["proposal_id"]
        ).content_payload()
        for handle, run in ((first_handle, first), (second_handle, second))
    ]
    assert stored[0] == stored[1]
    # Wall-clock fields exist but are excluded by the documented normalization only.
    raw = [json.dumps([item.public_dict() for item in run.records]) for run in (first, second)]
    assert "recorded_at_utc" in raw[0]


@pytest.mark.live
def test_a10_live_deepseek_normal_order_workflow(tmp_path: Path) -> None:
    if not (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY_FILE")):
        pytest.skip("live gate requires a DeepSeek credential and explicit paid settings")
    result = run_agent_scenario(
        provider_kind="deepseek",
        scenario_id="normal",
        seed=0,
        output=tmp_path / "live",
    )
    summary = result.summary
    assert result.status != "SKIPPED", summary
    assert summary["live_provider"] is True
    assert summary["terminal"] == "awaiting_approval", summary
    assert summary["operational_state_unchanged_by_supervisor"] is True
    assert summary["budget"]["provider_attempts"] >= 2
    assert summary["budget"]["tool_executions"] >= 1
    assert summary["budget"]["used_tokens"] > 0
    assert summary["budget"]["actual_cost_micros"] > 0
