"""S15 factory-twin projection: read-only, event-derived and verifiable."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from shopfloor_pilot.domain.models import EventType
from shopfloor_pilot.ui import DashboardController
from shopfloor_pilot.ui.factory_twin import (
    TwinTimelineError,
    build_twin_timeline,
    build_twin_timeline_from_repository,
    compare_projections,
    project_snapshot,
)
from shopfloor_pilot.ui.twin_component import FRONTEND_DIRECTORY
from shopfloor_pilot.ui.workbench import AgentWorkbench, PlannerSource, RequestType

ACTOR = "human:twin-tester"
VENDORED = {
    "three.module.js": "9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea",
    "three.core.js": "9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6",
    "OrbitControls.js": "3d79d07ecb686b4e5d93232eedab255331c1beef711e13164eaa1f68655a5f2b",
    "RoomEnvironment.js": "55f466192cc84298755a424c5e040345006b2ee1455589b3b54126c2ea4123f4",
    "RoundedBoxGeometry.js": "c5feab96123858ed8823c889b238dc8734adab7e760e3cfd21e7f3f2f3e348e3",
    "BufferGeometryUtils.js": "9fb63427ce6641fa14fd0baff9cc4d1b5f9c3d85fd084bf2e90e803c44ec1797",
    "LICENSE": "8b378ebe60e2fe500158cb0ac71cb5e8b7d92953c2abcc63a0eb90499653b5bc",
}
OWN_FRONTEND_FILES = (
    "index.html",
    "twin.js",
    "layout.js",
    "scene3d.js",
    "scene2d.js",
    "models.js",
)


def _decide(workbench: AgentWorkbench, source: PlannerSource, request_type: RequestType) -> None:
    result = workbench.request_decision(source=source, request_type=request_type, actor=ACTOR)
    if result.status == "started":
        task = workbench.wait_for_task(result.decision.workflow_id, 60)
        assert not task.running and task.error is None
    decision = workbench.coordinator().decision(result.decision.run_id, result.decision.workflow_id)
    assert workbench.approve(
        decision.workflow_id,
        proposal_id=decision.pending_proposal_id,
        proposal_hash=decision.pending_proposal_hash,
        actor=ACTOR,
    ).applied


@pytest.fixture(scope="module")
def breakdown(tmp_path_factory) -> DashboardController:
    """The dashboard breakdown story: plan, fault, recovery, restoration, finish."""

    root = tmp_path_factory.mktemp("twin")
    controller = DashboardController(root, run_id_factory=lambda scenario: f"twin-{scenario}")
    workbench = AgentWorkbench(controller, environ={}, readiness_report=root / "none.json")
    controller.start_or_reset("machine_breakdown")
    _decide(workbench, PlannerSource.BASELINE, RequestType.REPLAN)
    controller.inject_fixture_disruption()
    _decide(workbench, PlannerSource.SCRIPTED_FAKE, RequestType.ABNORMALITY)
    workbench.confirm_maintenance("CNC02", actor="operator:twin-tester")
    controller.advance_by(controller.remaining_seconds(controller.view().snapshot))
    return controller


def _timeline(controller: DashboardController, **kwargs):
    view = controller.view()
    return build_twin_timeline_from_repository(
        controller.repository,
        view.context.run_id,
        view.bundle.catalog,
        scenario_id=view.context.scenario_id,
        **kwargs,
    )


def test_a01_timeline_is_read_only_and_reproduces_sqlite(breakdown: DashboardController) -> None:
    run_id = breakdown.current_context().run_id
    repository = breakdown.repository
    before = (
        repository.get_checkpoint(run_id),
        repository.list_events(run_id),
        repository.export_lifecycle(run_id),
    )

    timeline = _timeline(breakdown)

    after = (
        repository.get_checkpoint(run_id),
        repository.list_events(run_id),
        repository.export_lifecycle(run_id),
    )
    assert after == before
    stored = before[0]
    assert timeline.verification.passed
    assert timeline.verification.replayed_snapshot_hash == stored.snapshot_hash
    assert timeline.payload["authoritative"] == project_snapshot(stored.snapshot)
    final = timeline.frames[-1]
    assert final["state_version"] == stored.snapshot.state_version
    assert final["time"] == stored.snapshot.simulated_time_seconds
    assert json.loads(json.dumps(timeline.payload)) == timeline.payload


def test_a01_frames_follow_committed_groups(breakdown: DashboardController) -> None:
    run_id = breakdown.current_context().run_id
    timeline = _timeline(breakdown)
    frames = timeline.frames
    versions = [item["state_version"] for item in frames]
    times = [item["time"] for item in frames]

    assert versions[0] == 0 and versions == sorted(set(versions))
    assert times == sorted(times)
    assert [item["index"] for item in frames] == list(range(len(frames)))
    # Every group with a business fact is a keyframe; only clock/telemetry-only
    # groups are folded into the interpolation between keyframes.
    business_versions = {
        item.resulting_state_version
        for item in breakdown.repository.list_events(run_id)
        if item.event.event_type not in (EventType.TIME_ADVANCED, EventType.OBSERVATION_RECORDED)
    }
    assert business_versions <= set(versions)

    # A pinned historical version is a pure, smaller projection of the same stream.
    pinned = _timeline(breakdown, state_version=15)
    assert pinned.verification.passed
    assert pinned.frames[-1]["state_version"] == 15
    assert pinned.frames == [item for item in frames if item["state_version"] <= 15]


def test_a06_breakdown_states_and_reassignments_are_visible(
    breakdown: DashboardController,
) -> None:
    frames = _timeline(breakdown).frames
    statuses = {
        "restored" if machine["restored"] else machine["status"]
        for frame in frames
        for machine in frame["machines"].values()
    }
    unit_states = {unit["state"] for frame in frames for unit in frame["units"].values()}
    event_types = {event["type"] for frame in frames for event in frame["events"]}

    assert {"idle", "running", "failed", "restored"} <= statuses
    assert {"queued", "running", "interrupted", "queued_restart", "completed"} <= unit_states
    assert {"operation_completed", "operation_interrupted", "machine_down"} <= event_types
    assert "machine_restored" in event_types

    fault = next(f for f in frames if any(e["type"] == "machine_down" for e in f["events"]))
    interrupted = next(e for e in fault["events"] if e["type"] == "operation_interrupted")
    assert fault["key"] and fault["machines"]["CNC02"]["status"] == "failed"
    assert interrupted["wasted_seconds"] > 0
    held_unit = interrupted["unit"]
    assert fault["machines"]["CNC02"]["held"] == [held_unit]
    assert fault["units"][held_unit]["zone"] == "held"

    replan = next(
        f
        for f in frames
        if any(e["type"] == "plan_applied" and e["plan_revision"] == 2 for e in f["events"])
    )
    restart = next(item for item in replan["reassignments"] if item["unit"] == held_unit)
    assert restart["from"] == "CNC02" and restart["to"] != "CNC02" and restart["restart"]
    moved = replan["units"][held_unit]
    assert moved["zone"] == "queue" and moved["machine_id"] == restart["to"]
    assert moved["reassigned_from"] == "CNC02" and moved["state"] == "queued_restart"
    assert held_unit in replan["machines"][restart["to"]]["queue"]

    restored = next(f for f in frames if any(e["type"] == "machine_restored" for e in f["events"]))
    assert restored["machines"]["CNC02"]["status"] == "idle"
    assert restored["machines"]["CNC02"]["restored"] is True

    final = frames[-1]
    assert all(unit["zone"] == "finished" for unit in final["units"].values())


def test_running_progress_is_defined_by_recorded_attempts(breakdown: DashboardController) -> None:
    frames = _timeline(breakdown).frames
    for frame, following in zip(frames, frames[1:], strict=False):
        for machine in frame["machines"].values():
            running = machine["running"]
            if running is None:
                continue
            completion = running["start_seconds"] + running["duration_seconds"]
            # Interpolating (t - start) / duration is exact between keyframes:
            # no completion can fall strictly inside the interval.
            assert running["start_seconds"] <= frame["time"] < completion
            assert following["time"] <= completion


def test_a02_payload_has_no_evaluator_truth_or_telemetry(breakdown: DashboardController) -> None:
    view = breakdown.view()
    text = json.dumps(_timeline(breakdown).payload)
    for forbidden in (
        "private_truth_ref",
        "unavailable_until",
        "observation",
        "signals",
        "temperature",
        "vibration",
        "recorded_at_utc",
    ):
        assert forbidden not in text
    for intervention in view.bundle.evaluator.interventions:
        assert intervention.intervention_id not in text
        if intervention.private_truth_ref:
            assert intervention.private_truth_ref not in text


def test_a05_replay_mismatch_and_broken_streams_fail_closed(
    breakdown: DashboardController,
) -> None:
    run_id = breakdown.current_context().run_id
    repository = breakdown.repository
    initial = repository.get_checkpoint(run_id, state_version=0)
    stored = repository.get_checkpoint(run_id)
    envelopes = repository.list_events(run_id)
    catalog = breakdown.view().bundle.catalog

    missing_tail = tuple(
        item for item in envelopes if item.resulting_state_version < stored.snapshot.state_version
    )
    stale = build_twin_timeline(
        initial_checkpoint=initial,
        envelopes=missing_tail,
        stored_checkpoint=stored,
        catalog=catalog,
    )
    assert not stale.verification.passed
    assert not stale.verification.hash_matches
    assert stale.payload["verification"]["mismatches"]

    gap = tuple(item for item in envelopes if item.resulting_state_version != 3)
    with pytest.raises(TwinTimelineError):
        build_twin_timeline(
            initial_checkpoint=initial, envelopes=gap, stored_checkpoint=stored, catalog=catalog
        )
    with pytest.raises(TwinTimelineError):
        build_twin_timeline(
            initial_checkpoint=stored, envelopes=(), stored_checkpoint=stored, catalog=catalog
        )


def test_compare_projections_names_differing_fields() -> None:
    left = {
        "state_version": 3,
        "plan_revision": 1,
        "simulated_time_seconds": 10,
        "machines": {"M1": {"status": "idle"}},
        "units": {"o#1": {"zone": "queue"}},
    }
    right = json.loads(json.dumps(left))
    assert compare_projections(left, right) == []
    right["machines"]["M1"]["status"] = "failed"
    right["simulated_time_seconds"] = 11
    assert compare_projections(left, right) == [
        "simulated_time_seconds: 10 != 11",
        "machines.M1 differs",
    ]


def test_a09_vendored_three_is_unmodified_and_licensed() -> None:
    vendor = FRONTEND_DIRECTORY / "vendor" / "three"
    for name, expected in VENDORED.items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == expected, name
    assert (vendor / "LICENSE").read_text().startswith("The MIT License")
    provenance = (vendor / "PROVENANCE.md").read_text()
    assert all(digest in provenance for digest in VENDORED.values())


def test_a02_frontend_is_display_only_and_local() -> None:
    for name in OWN_FRONTEND_FILES:
        source = (FRONTEND_DIRECTORY / name).read_text()
        # The only Streamlit message that returns data to Python (and triggers a rerun).
        assert "streamlit:setComponentValue" not in source, name
        assert "fetch(" not in source and "XMLHttpRequest" not in source, name
        assert "WebSocket" not in source and "sendBeacon" not in source, name
        assert not re.search(r"https?://", source), name
    index = (FRONTEND_DIRECTORY / "index.html").read_text()
    assert '"three": "./vendor/three/three.module.js"' in index
    assert '<script type="module" src="./twin.js">' in index


def test_streamlit_tab_renders_with_the_component(monkeypatch, tmp_path) -> None:
    from streamlit.testing.v1 import AppTest

    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_FILE", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPFLOOR_PILOT_READINESS_REPORT", str(tmp_path / "none.json"))
    monkeypatch.setenv("SHOPFLOOR_PILOT_UI_RUNTIME_DIR", str(tmp_path / "app-runtime"))
    app_path = Path(__file__).resolve().parents[2] / "src" / "shopfloor_pilot" / "ui" / "app.py"
    app = AppTest.from_file(app_path, default_timeout=30).run()
    assert not app.exception
    assert "Factory twin" in [tab.label for tab in app.tabs]
    assert any("Visual digital-twin **prototype**" in str(item.value) for item in app.caption)
    advance = next(item for item in app.button if item.label == "Advance and watch")
    assert advance.disabled  # no plan applied yet
    assert any("No plan is applied yet" in str(item.value) for item in app.info)


@pytest.mark.parametrize("passed", [True, False])
def test_a05_dashboard_refuses_to_render_an_unverified_stream(
    monkeypatch, tmp_path, passed: bool
) -> None:
    """A failed server replay check must show an error and render no animation."""

    from streamlit.testing.v1 import AppTest

    import shopfloor_pilot.ui.factory_twin as twin_module
    import shopfloor_pilot.ui.twin_component as component_module

    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_FILE", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHOPFLOOR_PILOT_READINESS_REPORT", str(tmp_path / "none.json"))
    monkeypatch.setenv("SHOPFLOOR_PILOT_UI_RUNTIME_DIR", str(tmp_path / f"runtime-{passed}"))
    real_build = twin_module.build_twin_timeline_from_repository
    rendered: list[dict] = []

    def build(*args, **kwargs):
        timeline = real_build(*args, **kwargs)
        if not passed:
            timeline.payload["verification"].update(
                passed=False, hash_matches=False, mismatches=["injected test mismatch"]
            )
        return timeline

    monkeypatch.setattr(twin_module, "build_twin_timeline_from_repository", build)
    monkeypatch.setattr(
        component_module, "render_factory_twin", lambda payload, **_: rendered.append(payload)
    )
    app_path = Path(__file__).resolve().parents[2] / "src" / "shopfloor_pilot" / "ui" / "app.py"
    app = AppTest.from_file(app_path, default_timeout=30).run()

    assert not app.exception
    errors = "\n".join(str(item.value) for item in app.error)
    if passed:
        assert len(rendered) == 1 and rendered[0]["verification"]["passed"]
        assert "Factory view not rendered" not in errors
    else:
        assert rendered == []
        assert "Factory view not rendered" in errors and "injected test mismatch" in errors


@pytest.fixture(scope="module")
def degraded(tmp_path_factory) -> DashboardController:
    """Breakdown story with production continued without CNC02 before restoring it."""

    root = tmp_path_factory.mktemp("degraded")
    controller = DashboardController(root, run_id_factory=lambda scenario: f"deg-{scenario}")
    workbench = AgentWorkbench(controller, environ={}, readiness_report=root / "none.json")
    controller.start_or_reset("machine_breakdown")
    _decide(workbench, PlannerSource.BASELINE, RequestType.REPLAN)
    controller.inject_fixture_disruption()
    _decide(workbench, PlannerSource.SCRIPTED_FAKE, RequestType.ABNORMALITY)
    controller.advance_degraded_production()
    workbench.confirm_maintenance("CNC02", actor="operator:twin-tester")
    controller.advance_to_plan_completion()
    return controller


def _first(frames, event_type, **fields):
    return next(
        f["index"]
        for f in frames
        if any(
            e["type"] == event_type and all(e.get(k) == v for k, v in fields.items())
            for e in f["events"]
        )
    )


def test_a06_reassignment_arc_lifetime(degraded: DashboardController) -> None:
    """Arcs last until the operation completes or the machine that caused them is back."""

    frames = _timeline(degraded).frames
    replan = _first(frames, "plan_applied", plan_revision=2)
    restored = _first(frames, "machine_restored", machine_id="CNC02")
    changes = {item["operation_id"]: item for item in frames[replan]["reassignments"]}
    restart = next(item for item in changes.values() if item["restart"])
    completed = _first(frames, "operation_completed", operation_id=restart["operation_id"])
    assert replan < completed < restored  # the restart finishes while CNC02 is still down

    unit = restart["unit"]
    during = frames[replan:completed]
    assert {f["units"][unit]["zone"] for f in during} == {"queue", "machine"}
    for frame in during:  # queued, then the full restart run on the new machine
        assert frame["units"][unit]["reassigned_from"] == "CNC02"
        assert frame["units"][unit]["reassigned_restart"] is True
    assert all(f["units"][unit]["reassigned_from"] is None for f in frames[completed:])

    # Work still running on its new machine when CNC02 is restored loses its arc then.
    still_moved = [
        key
        for key, item in frames[restored - 1]["units"].items()
        if item["reassigned_from"] is not None
    ]
    assert still_moved, "expected reassigned work in progress at the restoration"
    for key in still_moved:
        assert frames[restored]["units"][key]["zone"] in ("queue", "machine")
        assert frames[restored]["units"][key]["reassigned_from"] is None
    for frame in frames[restored:]:
        assert all(u["reassigned_from"] is None for u in frame["units"].values())
    before = frames[:replan]
    assert all(not u["reassigned_from"] for f in before for u in f["units"].values())


def test_a06_no_arcs_after_an_early_restoration(breakdown: DashboardController) -> None:
    frames = _timeline(breakdown).frames
    restored = _first(frames, "machine_restored", machine_id="CNC02")
    replan = _first(frames, "plan_applied", plan_revision=2)
    assert any(u["reassigned_from"] for u in frames[replan]["units"].values())
    for frame in frames[restored:]:
        assert all(u["reassigned_from"] is None for u in frame["units"].values())


def _playback_wall_seconds(frames, start: int, end: int, speed: int = 120) -> float:
    """Wall time of playback between two keyframes, mirroring twin.js defaults.

    twin.js advances `speed` simulated seconds per wall second, 20x faster while no
    machine is running ("fast idle gaps"); key-event holds are excluded here.
    """

    seconds = 0.0
    for frame, following in zip(frames[start:end], frames[start + 1 : end + 1], strict=True):
        running = any(machine["running"] for machine in frame["machines"].values())
        seconds += (following["time"] - frame["time"]) / (speed * (1 if running else 20))
    return seconds


def test_recovered_plan_runs_to_completion_with_full_length_playback(tmp_path) -> None:
    controller = DashboardController(tmp_path / "rt", run_id_factory=lambda s: f"done-{s}")
    workbench = AgentWorkbench(controller, environ={}, readiness_report=tmp_path / "none.json")
    controller.start_or_reset("machine_breakdown")
    _decide(workbench, PlannerSource.BASELINE, RequestType.REPLAN)
    controller.inject_fixture_disruption()
    _decide(workbench, PlannerSource.SCRIPTED_FAKE, RequestType.ABNORMALITY)

    snapshot = controller.view().snapshot
    end = controller.plan_completion_seconds(snapshot)
    assert end is not None and end > snapshot.simulated_time_seconds
    controller.advance_to_plan_completion()
    finished = controller.view().snapshot

    assert finished.simulated_time_seconds == end
    assert all(order.status.value == "completed" for order in finished.orders)
    assert controller.plan_completion_seconds(finished) is None
    with pytest.raises(Exception, match="no remaining planned work"):
        controller.advance_to_plan_completion()

    frames = _timeline(controller).frames
    fault = next(
        f["index"] for f in frames if any(e["type"] == "machine_down" for e in f["events"])
    )
    replan = next(
        f["index"]
        for f in frames
        if any(e["type"] == "plan_applied" and e["plan_revision"] == 2 for e in f["events"])
    )
    normal = _playback_wall_seconds(frames, 1, fault)
    recovered = _playback_wall_seconds(frames, replan, len(frames) - 1)
    # One click now plays the whole recovered execution, at least as long as the
    # normal pre-fault segment (recorded fixture: about 30 s vs 22.5 s at 120x).
    assert recovered >= normal > 0


def test_degraded_production_segment_runs_as_long_as_normal_segment(tmp_path) -> None:
    controller = DashboardController(tmp_path / "rt", run_id_factory=lambda s: f"deg-{s}")
    workbench = AgentWorkbench(controller, environ={}, readiness_report=tmp_path / "none.json")
    controller.start_or_reset("machine_breakdown")
    _decide(workbench, PlannerSource.BASELINE, RequestType.REPLAN)
    assert controller.degraded_production_target() is None  # nothing is down yet
    controller.inject_fixture_disruption()
    # Before a recovery plan exists the old plan cannot run: both actions refuse.
    blockers = controller.plan_blockers(controller.view().snapshot)
    assert any("CNC02" in item for item in blockers)
    assert any("no restart scheduled" in item for item in blockers)
    before = controller.view().snapshot
    for action in (controller.advance_degraded_production, controller.advance_to_plan_completion):
        with pytest.raises(Exception, match="recovery plan first"):
            action()
    assert controller.view().snapshot == before  # refused without any state change
    _decide(workbench, PlannerSource.SCRIPTED_FAKE, RequestType.ABNORMALITY)
    assert controller.plan_blockers(controller.view().snapshot) == ()

    approved = controller.view().snapshot
    target = controller.degraded_production_target()
    assert target == approved.simulated_time_seconds + 2700  # fault at 2,700 s after plan at 0
    controller.advance_degraded_production()
    degraded = controller.view().snapshot
    assert degraded.simulated_time_seconds == target
    assert degraded.plan_revision == approved.plan_revision == 2
    assert {m.machine_id: m.status.value for m in degraded.machines}["CNC02"] == "down"
    assert not all(order.status.value == "completed" for order in degraded.orders)

    workbench.confirm_maintenance("CNC02", actor="operator:twin-tester")
    assert controller.degraded_production_target() is None  # machine is back
    controller.advance_to_plan_completion()
    finished = controller.view().snapshot
    assert all(order.status.value == "completed" for order in finished.orders)

    timeline = _timeline(controller)
    assert timeline.verification.passed
    frames = timeline.frames
    fault = next(
        f["index"] for f in frames if any(e["type"] == "machine_down" for e in f["events"])
    )
    replan = next(
        f["index"]
        for f in frames
        if any(e["type"] == "plan_applied" and e["plan_revision"] == 2 for e in f["events"])
    )
    reached = next(f["index"] for f in frames if f["time"] >= target)
    for frame in frames[replan:reached]:
        assert frame["machines"]["CNC02"]["status"] == "failed"
    normal = _playback_wall_seconds(frames, 1, fault)
    without_cnc02 = _playback_wall_seconds(frames, replan, reached)
    assert 0.75 * normal <= without_cnc02 <= 1.25 * normal, (normal, without_cnc02)
