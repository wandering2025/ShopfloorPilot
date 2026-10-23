from __future__ import annotations

import re

from shopfloor_pilot.domain import ApprovalDecision
from shopfloor_pilot.ui import DashboardController
from shopfloor_pilot.ui.presenters import (
    build_gantt_figure,
    machine_metric_rows,
    machine_rows,
    operation_rows,
    order_rows,
    proposal_change_rows,
)


def test_empty_and_pending_figures_label_simulated_units(
    controller: DashboardController,
) -> None:
    initial = controller.ensure_active()
    empty = build_gantt_figure(initial.snapshot)
    controller.propose_plan()
    pending = controller.view().pending
    assert pending is not None
    proposed = build_gantt_figure(initial.snapshot, pending.candidate)

    assert empty.layout.xaxis.title.text == "Simulated time (seconds)"
    assert empty.layout.yaxis.title.text == "Machine / basis"
    assert empty.layout.annotations[0].text.startswith("No applied plan")
    assert proposed.layout.xaxis.title.text == "Simulated time (seconds)"
    assert proposed.data
    assert proposal_change_rows(initial.snapshot, pending.candidate)


def test_partial_execution_tables_and_chart_distinguish_actual_from_plan(
    controller: DashboardController,
) -> None:
    controller.ensure_active()
    controller.propose_plan()
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
    controller.advance_by(900)
    view = controller.view()
    figure = build_gantt_figure(view.snapshot)

    lanes = {str(value) for trace in figure.data for value in trace.y}
    assert any(item.startswith("Plan ·") for item in lanes)
    assert any(item.startswith("Actual ·") for item in lanes)
    assert machine_rows(view.snapshot)
    assert order_rows(view.snapshot)
    assert all(
        item["Start (s)"] is None or isinstance(item["Start (s)"], int)
        for item in operation_rows(view.snapshot)
    )
    metric_rows = machine_metric_rows(view.metrics)
    assert metric_rows
    assert all("Busy numerator (s)" in item for item in metric_rows)
    assert all("Calendar denominator (s)" in item for item in metric_rows)


def test_evidence_labels_are_readable_and_never_expose_hashes() -> None:
    from shopfloor_pilot.ui.agent_presenters import (
        claim_cards,
        evidence_label,
        evidence_options,
        short_fingerprint,
        unknown_signals,
    )

    results = {
        "observation:event-3b7643626f0f53203493556dbafb9eb423b07488": {
            "machine_id": "CNC02",
            "observed_at_seconds": 2700,
            "signals": [{"name": "temperature_millicelsius"}, {"name": "vibration"}],
        },
        "guidance:chunk-temp-vibration-ambiguity": {"title": "Temperature and vibration triage"},
        "solver:proposal-a57066fb41ea4924e421be2c": {},
        "validation:proposal-a57066fb41ea4924e421be2c": {"is_valid": True},
        "snapshot:v15:d133579ae0b8": {"simulated_time_seconds": 2700},
    }
    labels = evidence_options(
        list(results) + ["hypothesis:CNC02:2", "diagnosis:v15:CNC02"], results
    )

    assert labels["observation:event-3b7643626f0f53203493556dbafb9eb423b07488"] == (
        "Telemetry · CNC02 at 2,700 s (Temperature, Vibration)"
    )
    assert labels["guidance:chunk-temp-vibration-ambiguity"] == (
        "Guidance note · Temperature and vibration triage"
    )
    assert labels["hypothesis:CNC02:2"] == "Hypothesis 2 · CNC02"
    assert labels["validation:proposal-a57066fb41ea4924e421be2c"].endswith("passed")
    assert labels["snapshot:v15:d133579ae0b8"] == "Plant state at 2,700 s"
    assert not any(re.search(r"[0-9a-f]{12,}", label) for label in labels.values())
    assert evidence_label("custom:" + "x" * 80, {}).endswith("…")

    claims = [
        {"kind": "plan", "text": "ok", "evidence_ids": ["solver:proposal-a57066fb41ea4924e421be2c"]}
    ]
    assert claim_cards(claims, results)[0]["Based on"] == "Recovery schedule (solver result)"
    assert short_fingerprint("a" * 64) == "aaaaaaaa"
    assert unknown_signals(
        {
            "missing_evidence": [
                {"signal_field": "drive_current_milliamps", "reason": "additional_signal_required"}
            ]
        }
    ) == [{"Signal": "Drive current", "Status": "More data needed"}]


def test_review_tables_use_readable_operation_names(controller) -> None:
    from shopfloor_pilot.ui.agent_presenters import change_rows

    controller.ensure_active()
    controller.propose_plan()
    pending = controller.view().pending
    assert pending is not None
    _, services = controller.services_for(controller.current_context())
    projected, _report = services.preview_application(
        pending.candidate.run_id, pending.candidate.proposal_id, pending.candidate.proposal_hash
    )
    rows = change_rows(controller.view().snapshot, pending.candidate, projected)
    # Initial plan: nothing existing to compare, so every row is "New" work.
    assert rows
    for row in rows:
        assert row["Operation"].startswith("Step ")
        assert not re.search(r"[0-9a-f]{12,}", " ".join(str(v) for v in row.values()))
