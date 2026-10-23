"""Streamlit AppTest checks of the S12 dashboard workflow (offline only).

These drive real buttons and assert on authoritative metrics and visible states;
they do not freeze markup.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from shopfloor_pilot.ui.workbench import AgentWorkbench, FakeFixture, PlannerSource, RequestType

APP_PATH = Path(__file__).resolve().parents[2] / "src" / "shopfloor_pilot" / "ui" / "app.py"
LIVE_VARIABLES = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_FILE",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "SHOPFLOOR_PROVIDER_CURRENCY",
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path) -> None:
    for name in LIVE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPFLOOR_PILOT_READINESS_REPORT", str(tmp_path / "none.json"))
    monkeypatch.setenv("SHOPFLOOR_PILOT_UI_RUNTIME_DIR", str(tmp_path / "app-runtime"))


def _start() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    assert not app.exception
    return app


def _button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def _has_button(app: AppTest, label: str) -> bool:
    return any(item.label == label for item in app.button)


def _metric(app: AppTest, label: str) -> str:
    return next(item.value for item in app.metric if item.label == label)


def _texts(elements) -> str:
    return "\n".join(str(item.value) for item in elements)


def _click(app: AppTest, label: str) -> AppTest:
    _button(app, label).click()
    app.run()
    assert not app.exception
    return app


def _choose(app: AppTest, label: str, value) -> None:
    widget = next(item for item in [*app.radio, *app.selectbox] if item.label == label)
    widget.set_value(value)
    app.run()


def _wait_for_task(app: AppTest, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.run()
        if "Supervisor task running" not in _texts(app.markdown) and not any(
            "Supervisor working" in str(item.value) for item in app.info
        ):
            return
        time.sleep(0.2)
    raise AssertionError("supervisor task did not finish")


def _baseline_initial(app: AppTest) -> None:
    _click(app, "Submit request (one task per decision)")
    assert _metric(app, "Constraint validation") == "PASS"
    _click(app, "Approve & apply this proposal")
    assert _metric(app, "Plan revision") == "1"


def test_launch_initial_approval_rerun_and_second_session() -> None:
    app = _start()
    assert _metric(app, "State version") == "0"
    assert _metric(app, "Workflow status") == "Ready"
    assert "Synthetic demonstration data" in _texts(app.markdown)
    assert "Offline demo mode" in _texts(app.markdown)
    assert "SKIPPED" not in _texts(app.markdown)

    _click(app, "Submit request (one task per decision)")
    assert _metric(app, "State version") == "0"  # proposal only, nothing runs
    assert _metric(app, "Workflow status") == "Decision open"
    assert _has_button(app, "Approve & apply this proposal")
    assert _has_button(app, "Reject this proposal")
    assert _button(app, "Submit request (one task per decision)").disabled

    _click(app, "Approve & apply this proposal")
    assert _metric(app, "State version") == "1"
    assert _metric(app, "Plan revision") == "1"
    app.run()
    assert _metric(app, "State version") == "1"

    second = _start()
    assert _metric(second, "State version") == "1"
    assert _metric(second, "Plan revision") == "1"


def test_unsupported_typed_order_is_rejected_inline() -> None:
    app = _start()
    _choose(app, "Request type", RequestType.ORDER)
    product = next(item for item in app.text_input if item.label == "Product ID")
    product.set_value("UNSUPPORTED")
    _click(app, "Submit request (one task per decision)")
    assert _metric(app, "State version") == "0"
    assert "invalid_order" in _texts(app.error) and "product_id" in _texts(app.error)
    assert _metric(app, "Workflow status") == "Ready"


def test_scripted_fake_is_labeled_and_clarification_is_visible() -> None:
    app = _start()
    _choose(app, "Planner source", PlannerSource.SCRIPTED_FAKE)
    _choose(
        app,
        "Request fixture (the scripted fake replays its recorded interpretation)",
        "ambiguous-few-plates-soon",
    )
    _click(app, "Submit request (one task per decision)")
    _wait_for_task(app)
    warnings = _texts(app.warning)
    # Still states plainly that the model replies are recorded, not live DeepSeek.
    notes = _texts(app.info)
    assert "Recorded agent session" in notes and "not live DeepSeek output" in notes
    assert "Clarification needed" in warnings and "quantity" in warnings
    assert "clarification_required" in _texts(app.error)
    assert _metric(app, "State version") == "0"
    assert _metric(app, "Workflow status") == "Paused safely"


def test_live_source_without_settings_is_skipped_without_request() -> None:
    app = _start()
    _choose(app, "Planner source", PlannerSource.DEEPSEEK)
    assert "not configured" in _texts(app.warning).lower()
    _click(app, "Submit request (one task per decision)")
    assert "live_not_ready" in _texts(app.error)
    assert _metric(app, "Workflow status") == "Ready"


def test_outage_pauses_with_reason_and_offers_labeled_fallback() -> None:
    app = _start()
    _choose(app, "Scenario", "machine_breakdown")
    _click(app, "Start fresh run / reset")
    _baseline_initial(app)
    _click(app, "Inject named fixture disruption")
    version = _metric(app, "State version")
    _choose(app, "Planner source", PlannerSource.SCRIPTED_FAKE)
    _choose(app, "Request type", RequestType.ABNORMALITY)
    _choose(app, "Offline provider fixture", FakeFixture.OUTAGE)
    _click(app, "Submit request (one task per decision)")
    _wait_for_task(app)
    assert "provider_failure" in _texts(app.error)
    assert "unchanged" in _texts(app.error)
    assert _metric(app, "State version") == version
    assert _metric(app, "Workflow status") == "Paused safely"
    _click(app, "Run deterministic baseline fallback (explicit, labeled)")
    assert "Deterministic planner fallback" in _texts(app.info)
    assert "not counted as an LLM result" in _texts(app.info)
    assert _has_button(app, "Approve & apply this proposal")
    assert _metric(app, "State version") == version


def test_task_finishing_after_status_read_refreshes_header_and_controls(monkeypatch) -> None:
    """Finish a real outage between the header read and the decision-panel read."""

    original_turns = AgentWorkbench._fixture_turns
    original_status = AgentWorkbench.run_status
    observed = []

    def hold_outage(self, turns, fixture, record):
        result = original_turns(self, turns, fixture, record)
        if fixture is FakeFixture.OUTAGE:
            return [replace(result[0], before=lambda: record.release.wait(10)), *result[1:]]
        return result

    def finish_after_status_read(self):
        status = original_status(self)
        if status["active_tasks"] and not observed:
            observed.append(status["state"])
            task_id = status["active_tasks"][0]
            self.release_slow_response(task_id)
            assert not self.wait_for_task(task_id).running
            assert original_status(self)["state"] == "paused_blocked"
        return status

    monkeypatch.setattr(AgentWorkbench, "_fixture_turns", hold_outage)
    monkeypatch.setattr(AgentWorkbench, "run_status", finish_after_status_read)
    app = _start()
    _choose(app, "Scenario", "machine_breakdown")
    _click(app, "Start fresh run / reset")
    _baseline_initial(app)
    _click(app, "Inject named fixture disruption")
    version = _metric(app, "State version")
    _choose(app, "Planner source", PlannerSource.SCRIPTED_FAKE)
    _choose(app, "Request type", RequestType.ABNORMALITY)
    _choose(app, "Offline provider fixture", FakeFixture.OUTAGE)
    _click(app, "Submit request (one task per decision)")

    assert observed == ["paused_at_decision"]
    assert "provider_failure" in _texts(app.error)
    assert _metric(app, "Workflow status") == "Paused safely"
    assert _metric(app, "State version") == version
    assert not _button(app, "Submit request (one task per decision)").disabled
    assert _has_button(app, "Run deterministic baseline fallback (explicit, labeled)")


def test_recovery_review_evidence_and_separate_maintenance_control() -> None:
    app = _start()
    _choose(app, "Scenario", "machine_breakdown")
    _click(app, "Start fresh run / reset")
    _baseline_initial(app)
    _click(app, "Inject named fixture disruption")
    _choose(app, "Planner source", PlannerSource.SCRIPTED_FAKE)
    _choose(app, "Request type", RequestType.ABNORMALITY)
    _click(app, "Submit request (one task per decision)")
    _wait_for_task(app)
    assert _metric(app, "Constraint validation") == "PASS"
    markdown = _texts(app.markdown)
    # Plain-language headline; the raw source check lives in collapsed technical details.
    assert _metric(app, "Root cause") == "Not confirmed"
    assert _metric(app, "Maintenance") == "Not done"
    assert "resolved: **True**" in markdown
    # Readable tables: no long hex identifiers or hashes outside technical details.
    tables = [
        frame
        for frame in app.table
        if set(frame.value.columns) & {"Possible explanation", "Agent statement", "Signal"}
    ]
    assert len(tables) >= 3  # explanations, unknown signals, agent statements (+ review copy)
    for frame in tables:
        cells = " ".join(str(v) for v in frame.value.to_numpy().ravel())
        assert not re.search(r"[0-9a-f]{12,}", cells), cells
        assert "observation:event-" not in cells and "proposal-" not in cells
    assert not re.search(r"[0-9a-f]{32,}", _texts(app.metric))
    # Maintenance confirmation is disabled while the recovery decision is open.
    assert _button(app, "Confirm maintenance complete for CNC02").disabled
    _click(app, "Approve & apply this proposal")
    machines = next(item for item in app.dataframe if "Status" in item.value.columns)
    assert "down" in set(machines.value["Status"])
    _click(app, "Confirm maintenance complete for CNC02")
    assert "machine_restored" in _texts(app.success)


def test_plan_buttons_wait_for_an_approved_recovery_plan() -> None:
    app = _start()
    _choose(app, "Scenario", "machine_breakdown")
    _click(app, "Start fresh run / reset")
    _baseline_initial(app)
    _click(app, "Inject named fixture disruption")
    # Fault recorded, no recovery plan yet: the old plan cannot run.
    assert _button(app, "Run plan to completion and watch").disabled
    assert _button(app, "Continue production without CNC02 and watch").disabled
    assert "cannot run as it stands" in _texts(app.info)

    _choose(app, "Planner source", PlannerSource.SCRIPTED_FAKE)
    _choose(app, "Request type", RequestType.ABNORMALITY)
    _click(app, "Submit request (one task per decision)")
    _wait_for_task(app)
    assert _button(app, "Run plan to completion and watch").disabled  # decision open
    _click(app, "Approve & apply this proposal")
    assert not _button(app, "Run plan to completion and watch").disabled
    assert not _button(app, "Continue production without CNC02 and watch").disabled
    assert "cannot run as it stands" not in _texts(app.info)
