"""S10-A01/A02: fixed request fixtures, scenario clock, and typed clarification."""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path

import pytest
from agent_support import fixtures_by_id, fresh_run, identity, run_supervisor

from shopfloor_pilot.agent import interpretation as interpretation_module
from shopfloor_pilot.agent.harness import load_request_fixtures, scripted_order_turns
from shopfloor_pilot.agent.interpretation import (
    OrderInterpretationDraft,
    interpret_order_draft,
)
from shopfloor_pilot.agent.workflow import SupervisorTask
from shopfloor_pilot.domain.fixtures import ScenarioClock, load_scenario
from shopfloor_pilot.domain.models import ResultStatus
from shopfloor_pilot.tools import WorkflowKind, WorkflowPhase

EXPECTED_FIELDS = (
    "product_id",
    "route_id",
    "quantity",
    "due_time_seconds",
    "due_basis",
    "release_time_seconds",
    "priority",
    "priority_weight",
)


def _draft(payload: dict[str, object], run_id: str = "run-test") -> OrderInterpretationDraft:
    return OrderInterpretationDraft.model_validate_json(json.dumps({**payload, "run_id": run_id}))


def field_results(fixture_id: str, tmp: Path) -> dict[str, object]:
    """Run one fixture through the scripted supervisor and compare typed fields."""

    fixture = fixtures_by_id()[fixture_id]
    handle = fresh_run(tmp, fixture.scenario)
    before = identity(handle.services, handle.run_id)
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, fixture.request_text),
        scripted_order_turns(fixture.scripted_draft),
        workflow_id=f"workflow-{fixture_id}",
    )
    actual = run.outcome.interpretation or {}
    expected = fixture.expected
    rows = []
    if expected["status"] == "success":
        for name in EXPECTED_FIELDS:
            rows.append(
                {
                    "field": name,
                    "expected": expected[name],
                    "actual": actual.get(name),
                    "match": expected[name] == actual.get(name),
                }
            )
    else:
        rows.append(
            {
                "field": "issue_codes",
                "expected": sorted(expected["issue_codes"]),
                "actual": sorted(item["code"] for item in actual.get("issues", [])),
                "match": sorted(expected["issue_codes"])
                == sorted(item["code"] for item in actual.get("issues", [])),
            }
        )
        rows.append(
            {
                "field": "unresolved_fields",
                "expected": sorted(expected["unresolved_fields"]),
                "actual": sorted(actual.get("unresolved_fields", [])),
                "match": sorted(expected["unresolved_fields"])
                == sorted(actual.get("unresolved_fields", [])),
            }
        )
    rows.insert(
        0,
        {
            "field": "status",
            "expected": expected["status"],
            "actual": actual.get("status"),
            "match": expected["status"] == actual.get("status"),
        },
    )
    return {
        "fixture_id": fixture_id,
        "category": fixture.category,
        "scenario": fixture.scenario,
        "request_text": fixture.request_text,
        "terminal": run.outcome.terminal.value,
        "proposal_id": None
        if run.outcome.proposal is None
        else run.outcome.proposal["proposal_id"],
        "fields": rows,
        "all_match": all(item["match"] for item in rows),
        "operational_state_unchanged": before == identity(handle.services, handle.run_id),
    }


def test_fixture_set_has_required_coverage() -> None:
    fixtures = load_request_fixtures().fixtures
    categories = [item.category for item in fixtures]
    assert len(fixtures) >= 8
    assert categories.count("normal") >= 3
    assert categories.count("urgent") >= 3
    assert categories.count("ambiguous") + categories.count("unsupported") >= 2
    texts = " ".join(item.request_text for item in fixtures)
    assert "铝" in texts and "Aluminum Mounting Plate" in texts  # S01 bilingual plate example
    assert "today" in texts and "今天" in texts and "hours" in texts and "分钟" in texts


@pytest.mark.parametrize(
    "fixture_id", [item.fixture_id for item in load_request_fixtures().fixtures]
)
def test_a01_fixture_fields_match_through_supervisor(fixture_id: str, tmp_path: Path) -> None:
    result = field_results(fixture_id, tmp_path)
    assert result["all_match"], result["fields"]
    assert result["operational_state_unchanged"]
    expected_terminal = {
        "success": WorkflowPhase.AWAITING_APPROVAL.value,
        "clarification_required": WorkflowPhase.CLARIFICATION_REQUIRED.value,
        "rejected": WorkflowPhase.REJECTED.value,
    }[fixtures_by_id()[fixture_id].expected["status"]]
    assert result["terminal"] == expected_terminal


def test_a01_scenario_clock_not_host_date(monkeypatch: pytest.MonkeyPatch) -> None:
    # The interpreter imports no wall-clock facility at all.
    tree = ast.parse(Path(interpretation_module.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"datetime", "time", "calendar", "zoneinfo"}

    catalog = load_scenario("normal").catalog
    draft = _draft(fixtures_by_id()["normal-en-plate-today"].scripted_draft)
    text = fixtures_by_id()["normal-en-plate-today"].request_text

    def resolve(clock: ScenarioClock | None) -> object:
        return interpret_order_draft(
            draft,
            request_text=text,
            catalog=catalog,
            scenario_clock=clock,
            decision_boundary_seconds=0,
            hard_horizon_seconds=28_800,
            order_id="order-req-test",
        )

    shift = ScenarioClock(origin_label="shift", start_seconds=0, shift_boundary_seconds=28_800)
    results = []
    for fake_epoch, zone in ((0.0, "UTC"), (4_102_444_800.0, "Asia/Shanghai")):
        monkeypatch.setattr(time, "time", lambda value=fake_epoch: value)
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        results.append(resolve(shift).model_dump(mode="json"))
    monkeypatch.delenv("TZ")
    time.tzset()
    assert results[0] == results[1]
    assert results[0]["due_time_seconds"] == 28_800

    shorter = ScenarioClock(origin_label="shift", start_seconds=0, shift_boundary_seconds=14_400)
    assert resolve(shorter).due_time_seconds == 14_400
    missing = resolve(None)
    assert missing.status is ResultStatus.CLARIFICATION_REQUIRED
    assert [item.code.value for item in missing.issues] == ["today_without_clock"]
    assert os.environ.get("TZ") is None


A02_CASES = (
    (
        "ambiguous_quantity",
        "几件铝安装板，今天要。",
        {
            "request_type": "new_order",
            "product_text": "铝安装板",
            "quantity_text": "几件",
            "due_texts": ["今天"],
            "priority": "normal",
        },
        "clarification_required",
        {"ambiguous_quantity"},
        {"quantity"},
    ),
    (
        "impossible_capability",
        "2 aluminum mounting plates, anodized, due today.",
        {
            "request_type": "new_order",
            "product_text": "aluminum mounting plate",
            "quantity_text": "2",
            "due_texts": ["today"],
            "priority": "normal",
            "requested_capabilities": ["slotting"],
        },
        "rejected",
        {"unsupported_process", "capability_not_on_route"},
        {"capabilities"},
    ),
    (
        "conflicting_due_units",
        "1 steel bracket within 90 minutes, i.e. within 2 hours.",
        {
            "request_type": "new_order",
            "product_text": "steel bracket",
            "quantity_text": "1",
            "due_texts": ["within 90 minutes"],
            "priority": "normal",
        },
        "clarification_required",
        {"conflicting_due_time"},
        {"due_time"},
    ),
    (
        "fabricated_quantity",
        "Add aluminum mounting plates due today.",
        {
            "request_type": "new_order",
            "product_text": "aluminum mounting plate",
            "quantity_text": "3",
            "due_texts": ["today"],
            "priority": "normal",
        },
        "rejected",
        {"span_not_in_request"},
        {"quantity"},
    ),
    (
        "machine_pin",
        "2 steel brackets on CNC03 today.",
        {
            "request_type": "new_order",
            "product_text": "steel bracket",
            "quantity_text": "2",
            "due_texts": ["today"],
            "priority": "normal",
        },
        "rejected",
        {"machine_pinning_unsupported"},
        {"machine"},
    ),
)


@pytest.mark.parametrize(
    ("case_id", "text", "draft", "status", "codes", "fields"),
    A02_CASES,
    ids=[item[0] for item in A02_CASES],
)
def test_a02_unresolved_requests_never_mutate_or_fabricate(
    tmp_path: Path,
    case_id: str,
    text: str,
    draft: dict[str, object],
    status: str,
    codes: set[str],
    fields: set[str],
) -> None:
    handle = fresh_run(tmp_path, "normal")
    before = identity(handle.services, handle.run_id)
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, text),
        scripted_order_turns(draft),
        workflow_id=f"workflow-a02-{case_id.replace('_', '-')}",
    )
    interpretation = run.outcome.interpretation
    assert interpretation is not None
    assert interpretation["status"] == status
    assert codes <= {item["code"] for item in interpretation["issues"]}
    assert fields <= set(interpretation["unresolved_fields"])
    # No invented values: an unresolved interpretation carries no order fields.
    for name in ("order_id", "product_id", "quantity", "due_time_seconds", "priority_weight"):
        assert interpretation[name] is None
    assert run.outcome.terminal.value == status
    assert run.outcome.details["unresolved_fields"]
    assert run.outcome.proposal is None
    # Only one provider turn: the workflow stops instead of guessing further.
    assert run.provider.calls == 1
    assert identity(handle.services, handle.run_id) == before
    lifecycle = handle.services.export_lifecycle(handle.run_id)
    assert not lifecycle.proposals and not lifecycle.approvals


def test_a01_verbatim_plural_span_resolves_to_contained_alias() -> None:
    catalog = load_scenario("normal").catalog
    text = "Please add 2 Aluminum Mounting Plates, due today."
    shift = ScenarioClock(origin_label="shift", start_seconds=0, shift_boundary_seconds=28_800)
    for span, status in (
        ("Aluminum Mounting Plates", ResultStatus.SUCCESS),
        ("2 Aluminum Mounting Plates", ResultStatus.SUCCESS),
        ("Plates", ResultStatus.REJECTED),
    ):
        result = interpret_order_draft(
            _draft(
                {
                    "request_type": "new_order",
                    "product_text": span,
                    "quantity_text": "2",
                    "due_texts": ["today"],
                    "priority": "normal",
                }
            ),
            request_text=text,
            catalog=catalog,
            scenario_clock=shift,
            decision_boundary_seconds=0,
            hard_horizon_seconds=28_800,
            order_id="order-req-test",
        )
        assert result.status is status, (span, result.issues)
        if status is ResultStatus.SUCCESS:
            assert result.product_id == "prod_aluminum_mounting_plate"
