"""Semantic claim audit against the pinned snapshot (S11 probe / S13-A12 support)."""

from __future__ import annotations

from pathlib import Path

from recovery_support import harness

from shopfloor_pilot.agent.workflow import SupervisorConfig
from shopfloor_pilot.workflows.claim_audit import audit_claim_text, audit_claims

LIVE_CLAIM = "State v15 at t=2700s: CNC01, CNC02, CNC03 and QC01 are unavailable; 8 orders present."


def _verdicts(findings):
    return {(item["type"], item.get("machine_id")): item["verdict"] for item in findings}


def test_live_attempt_claim_is_ambiguous_not_contradictory(tmp_path: Path) -> None:
    snapshot = harness(tmp_path, "fault").services.get_snapshot("run-breakdown-fixture")
    statuses = {item.machine_id: item.status.value for item in snapshot.machines}
    assert statuses == {"CNC01": "running", "CNC02": "down", "CNC03": "running", "QC01": "running"}
    verdicts = _verdicts(audit_claim_text(LIVE_CLAIM, snapshot))
    assert verdicts == {
        ("machine_status", "CNC01"): "ambiguous",
        ("machine_status", "CNC02"): "consistent",
        ("machine_status", "CNC03"): "ambiguous",
        ("machine_status", "QC01"): "ambiguous",
        ("order_count", None): "consistent",
        ("simulated_time", None): "consistent",
        ("state_version", None): "consistent",
    }


def test_clear_contradictions_are_detected_and_negations_skipped(tmp_path: Path) -> None:
    snapshot = harness(tmp_path, "fault").services.get_snapshot("run-breakdown-fixture")
    cases = {
        "CNC01 is down after the alarm.": {("machine_status", "CNC01"): "contradiction"},
        "CNC02 is running normally.": {("machine_status", "CNC02"): "contradiction"},
        "CNC02 is idle and free.": {("machine_status", "CNC02"): "contradiction"},
        "CNC03 is busy; CNC02 stopped.": {
            ("machine_status", "CNC03"): "consistent",
            ("machine_status", "CNC02"): "consistent",
        },
        # Probe false positive: earlier readings are history, not the current time.
        "CNC02 ran near 48.3 degC until t=2520s, then spiked at t=2700s.": {
            ("simulated_time", None): "consistent",
        },
        "There are 9 orders at t=2800s in state v14.": {
            ("order_count", None): "contradiction",
            ("simulated_time", None): "contradiction",
            ("state_version", None): "contradiction",
        },
        "CNC02 is not running.": {},
        "CNC02 temperature peaked at 48.3 degC.": {},
    }
    for text, expected in cases.items():
        assert _verdicts(audit_claim_text(text, snapshot)) == expected, text
    summary = audit_claims(
        [{"kind": "observation", "text": "CNC01 is down."}, {"kind": "plan", "text": LIVE_CLAIM}],
        snapshot,
    )
    assert summary["has_contradiction"] is True
    assert summary["counts"] == {"consistent": 4, "contradiction": 1, "ambiguous": 3}


def test_outcome_filter_accepts_clause_initial_negation_only() -> None:
    from shopfloor_pilot.tools.supervisor_tools import forbidden_claim_phrase

    accepted = [
        # The exact hedge rejected twice in the S11 live probe (machine_breakdown seed 0).
        "Diagnostic status for CNC02 is ambiguous with three limited, uncalibrated "
        "possibilities (thermal-load/rotating-element/tool-interface type conditions); "
        "no cause is confirmed.",
        "Nothing is approved yet.",
        "None of the hypotheses is confirmed.",
        "The plan is not approved and awaits human approval.",
        "CNC02 remains down and is not restored.",
        # S11 probe (machine_breakdown seed 8): both claims were rejected by the old filter.
        "Uncalibrated hypothesis only: diagnosis remains ambiguous and nothing is confirmed.",
        "Validated recovery proposal; it awaits human approval and has not been applied.",
        "Uncalibrated, limited: another possible explanation; neither cause is confirmed.",
    ]
    rejected = {
        "Root cause confirmed and machine repaired.": "root cause",
        "CNC02, with no alarms, is restored.": "is restored",
        "The machine has been repaired; no action needed.": "has been repaired",
        "The plan is approved.": "is approved",
        "No alarms remain and CNC02 is now fixed.": "is now fixed",
        "No alarm and CNC02 is fixed.": "is fixed",
        "Nothing changed but the plan is applied.": "is applied",
        "The plan is checked and is now applied.": "is now applied",
    }
    for text in accepted:
        assert forbidden_claim_phrase(text) is None, text
    for text, phrase in rejected.items():
        assert forbidden_claim_phrase(text) == phrase, text


def test_truncated_tool_arguments_get_an_actionable_notice(tmp_path: Path) -> None:
    import json

    from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn

    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    turns = h.fault_turns()
    cut = '{"run_id": "run-breakdown-fixture", "claims": [{"kind": "observation", "text": "CNC0'
    truncated = ScriptedTurn(calls=(ScriptedCall("persist_proposal", raw_arguments=cut),))
    step = h.run_fault_supervisor(decision, [*turns[:-1], truncated, turns[-1]])
    assert step.outcome.terminal.value == "awaiting_approval"
    last_request = h.sessions[-1].workflow._provider.requests[-1]  # noqa: SLF001
    digest = json.loads(last_request.messages[1].content)
    assert any("truncated by the output limit" in item for item in digest["notices"])
    assert "at most 160 characters" in digest["next_step"]


def test_repeated_call_notice_reaches_the_model(tmp_path: Path) -> None:
    import json

    from recovery_support import SNAPSHOT_CALL

    from shopfloor_pilot.agent.fake import ScriptedTurn

    h = harness(tmp_path, "fault")
    decision = h.open_fault()
    turns = h.fault_turns()
    repeat = ScriptedTurn(calls=(SNAPSHOT_CALL,))
    step = h.run_fault_supervisor(
        decision,
        [turns[0], repeat, *turns[1:]],
        config=SupervisorConfig(narrow_next_step_tools=False),
    )
    assert step.outcome.terminal.value == "awaiting_approval"
    requests = h.sessions[-1].workflow._provider.requests  # noqa: SLF001
    digest = json.loads(requests[2].messages[1].content)
    assert any("repeated with identical arguments" in item for item in digest["notices"])


def test_order_hint_and_kind_mismatch_feedback_are_explicit(tmp_path: Path) -> None:
    import json

    from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn

    h = harness(tmp_path, "urgent")
    decision = h.open_order()
    task, turns = h.order_task_and_turns()
    good = turns[-1]
    bad_args = json.loads(json.dumps(dict(good.calls[0].arguments)))
    bad_args["claims"][0]["evidence_ids"] = ["$evidence_first:interpretation"]
    bad = ScriptedTurn(calls=(ScriptedCall("persist_proposal", bad_args),))
    step = h.coordinator.run_supervisor(
        decision, h.factory(decision, task, [*turns[:-1], bad, good])
    )
    assert step.outcome.terminal.value == "awaiting_approval"
    requests = h.sessions[-1].workflow._provider.requests  # noqa: SLF001
    gather = [
        json.loads(item.messages[1].content)
        for item in requests
        if json.loads(item.messages[1].content)["workflow"]["phase"] == "gather_evidence"
    ]
    assert gather and "do not re-read them" in gather[0]["next_step"]
    rejection = json.loads(requests[-1].messages[-1].content)
    issue = rejection["details"]["issues"][0]
    assert issue["code"] == "evidence_kind_mismatch"
    assert issue["claim_kind"] == "observation"
    assert issue["allowed_for_this_claim"] == ["event_log", "observation", "snapshot"]


def test_future_boundary_time_claim_is_rejected_before_persistence(tmp_path: Path) -> None:
    import json

    from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn

    h = harness(tmp_path, "urgent")
    decision = h.open_order()
    task, turns = h.order_task_and_turns()
    good = turns[-1]
    bad_args = json.loads(json.dumps(dict(good.calls[0].arguments)))
    bad_args["claims"][0]["text"] = "The pinned event log reaches the decision boundary at t=3600s."
    bad = ScriptedTurn(calls=(ScriptedCall("persist_proposal", bad_args),))
    step = h.coordinator.run_supervisor(
        decision, h.factory(decision, task, [*turns[:-1], bad, good])
    )
    assert step.outcome.terminal.value == "awaiting_approval"
    requests = h.sessions[-1].workflow._provider.requests  # noqa: SLF001
    rejection = json.loads(requests[-1].messages[-1].content)
    issue = rejection["details"]["issues"][0]
    assert issue == {
        "actual": 3599,
        "claim": 0,
        "claimed": 3600,
        "code": "contradicts_pinned_snapshot",
        "finding_type": "simulated_time",
        "hint": (
            "rewrite the observation with the exact pinned snapshot value; "
            "decision_boundary_seconds is a future application boundary, not the "
            "observed simulated time"
        ),
    }
