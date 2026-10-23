"""S10-A04: closed schemas, allowlist, and pre-execution rejection."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from agent_support import FIXED_UTC, fresh_run, identity, run_supervisor

from shopfloor_pilot.agent.fake import ScriptedCall, ScriptedTurn
from shopfloor_pilot.agent.workflow import SupervisorTask, SupervisorWorkflow
from shopfloor_pilot.diagnostics import DiagnosticService
from shopfloor_pilot.retrieval import LexicalRetriever, load_corpus
from shopfloor_pilot.tools import (
    PROHIBITED_TOOL_NAMES,
    ObservedState,
    RejectionReason,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    WorkflowKind,
    WorkflowPhase,
    WorkflowState,
    build_supervisor_registry,
)
from shopfloor_pilot.tools.registry import ToolCallStatus, ToolClassification, ToolOutcome


def _walk(schema: Mapping[str, Any], path: str = "$") -> list[tuple[str, Mapping[str, Any]]]:
    nodes = [(path, schema)]
    for name, child in schema.get("properties", {}).items():
        nodes.extend(_walk(child, f"{path}.{name}"))
    if isinstance(schema.get("items"), Mapping):
        nodes.extend(_walk(schema["items"], f"{path}[]"))
    return nodes


def test_a04_every_schema_is_closed_bounded_and_run_scoped() -> None:
    registry = build_supervisor_registry()
    assert len(registry.names) == 10
    for spec in registry.specs():
        schema = spec.parameters
        assert "run_id" in schema["required"], spec.name
        assert spec.classification in set(ToolClassification)
        assert spec.max_result_bytes <= 8_000
        assert spec.phases and all(not phase.is_terminal for phase in spec.phases)
        for path, node in _walk(schema):
            types = node.get("type")
            types = types if isinstance(types, list) else [types]
            if "object" in types:
                assert node.get("additionalProperties") is False, (spec.name, path)
            if "string" in types and "enum" not in node:
                assert "maxLength" in node, (spec.name, path)
            if "array" in types:
                assert "maxItems" in node, (spec.name, path)
            if "integer" in types and path.endswith(("limit", "top_k", "state_version")):
                assert "maximum" in node, (spec.name, path)
        if spec.requires_state_version:
            assert "state_version" in schema["required"]
    proposal_writers = [
        item.name
        for item in registry.specs()
        if item.classification is ToolClassification.PROPOSAL_WRITE
    ]
    assert proposal_writers == ["persist_proposal"]
    assert not set(registry.names) & PROHIBITED_TOOL_NAMES


@pytest.mark.parametrize("name", ["apply_plan", "approve_plan", "shell", "sql", "restore_machine"])
def test_a04_prohibited_names_cannot_be_registered(name: str) -> None:
    with pytest.raises(ValueError, match="prohibited"):
        ToolSpec(
            name=name,
            description="attempted privileged tool",
            parameters={
                "type": "object",
                "properties": {"run_id": {"type": "string", "maxLength": 64}},
                "required": ["run_id"],
                "additionalProperties": False,
            },
            classification=ToolClassification.READ_ONLY,
            phases=frozenset({WorkflowPhase.OBSERVE}),
            handler=lambda _args, _context: ToolOutcome(ToolCallStatus.OK, {}),
        )


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    handle = fresh_run(tmp_path, "normal")
    facade = handle.services.for_model()
    snapshot = facade.get_snapshot(handle.run_id)
    checkpoint = facade.get_checkpoint(handle.run_id)
    state = WorkflowState(
        run_id=handle.run_id,
        workflow_id="workflow-a04",
        kind=WorkflowKind.ABNORMALITY,
        request_text="inspect",
        phase=WorkflowPhase.GATHER_EVIDENCE,
        observed=ObservedState(
            state_version=snapshot.state_version,
            plan_revision=snapshot.plan_revision,
            snapshot_hash=checkpoint.snapshot_hash,
            simulated_time_seconds=snapshot.simulated_time_seconds,
            hard_horizon_seconds=snapshot.hard_horizon_seconds,
            last_sequence=checkpoint.last_sequence,
            decision_boundary_seconds=0,
            machine_ids=frozenset(item.machine_id for item in snapshot.machines),
            order_ids=frozenset(item.order_id for item in snapshot.orders),
        ),
    )
    return ToolContext(
        run_id=handle.run_id,
        workflow_id="workflow-a04",
        facade=facade,
        catalog=handle.bundle.catalog,
        diagnostics=DiagnosticService(
            run_id=handle.run_id,
            get_checkpoint=facade.get_checkpoint,
            list_events=facade.list_events,
            retriever=LexicalRetriever(load_corpus()),
        ),
        scenario_clock=handle.bundle.scenario.clock,
        seed=0,
        state=state,
        wall_clock=lambda: FIXED_UTC,
    )


def _spy_registry() -> tuple[ToolRegistry, list[str]]:
    executed: list[str] = []
    wrapped = []
    for spec in build_supervisor_registry().specs():
        original = spec.handler

        def handler(args: Mapping[str, Any], ctx: ToolContext, *, _n=spec.name, _h=original):
            executed.append(_n)
            return _h(args, ctx)

        wrapped.append(
            ToolSpec(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                classification=spec.classification,
                phases=spec.phases,
                handler=handler,
                requires_state_version=spec.requires_state_version,
                max_result_bytes=spec.max_result_bytes,
                machine_reference_fields=spec.machine_reference_fields,
                kinds=spec.kinds,
            )
        )
    return ToolRegistry(tuple(wrapped)), executed


REJECTIONS = (
    ("apply_plan", {"run_id": "run-normal-fixture"}, RejectionReason.PROHIBITED_TOOL),
    ("approve", {"run_id": "run-normal-fixture"}, RejectionReason.PROHIBITED_TOOL),
    ("shell", {"command": "rm -rf /"}, RejectionReason.PROHIBITED_TOOL),
    ("sql", {"query": "DELETE FROM events"}, RejectionReason.PROHIBITED_TOOL),
    ("get_weather", {"run_id": "run-normal-fixture"}, RejectionReason.UNKNOWN_TOOL),
    (
        "summarize_event_log",
        {"run_id": "run-normal-fixture", "state_version": 0, "drop_table": True},
        RejectionReason.INVALID_ARGUMENTS,
    ),
    (
        "summarize_event_log",
        {"run_id": "run-other-fixture", "state_version": 0},
        RejectionReason.WRONG_RUN,
    ),
    (
        "summarize_event_log",
        {"run_id": "run-normal-fixture", "state_version": 7},
        RejectionReason.STATE_VERSION_MISMATCH,
    ),
    (
        "diagnose_machine",
        {"run_id": "run-normal-fixture", "state_version": 0, "machine_ids": ["CNC99"]},
        RejectionReason.UNKNOWN_REFERENCE,
    ),
    (
        "persist_proposal",
        {
            "run_id": "run-normal-fixture",
            "state_version": 0,
            "candidate_id": "proposal-x",
            "claims": [{"kind": "plan", "text": "x", "evidence_ids": ["validation:x"]}],
        },
        RejectionReason.PHASE_NOT_ALLOWED,
    ),
)


@pytest.mark.parametrize(("name", "arguments", "reason"), REJECTIONS)
def test_a04_registry_rejects_before_any_handler_runs(
    context: ToolContext, name: str, arguments: dict[str, Any], reason: RejectionReason
) -> None:
    registry, executed = _spy_registry()
    before = identity_for(context)
    outcome = registry.execute(name, arguments, context)
    assert outcome.status is ToolCallStatus.REJECTED
    assert outcome.reason == reason.value
    assert executed == []
    assert identity_for(context) == before


def identity_for(context: ToolContext) -> tuple[int, str]:
    checkpoint = context.facade.get_checkpoint(context.run_id)
    return checkpoint.snapshot.state_version, checkpoint.snapshot_hash


def test_a04_allowlisted_typed_tool_executes(context: ToolContext) -> None:
    registry, executed = _spy_registry()
    outcome = registry.execute(
        "summarize_event_log", {"run_id": context.run_id, "state_version": 0}, context
    )
    assert outcome.ok and executed == ["summarize_event_log"]


def test_a04_tools_and_supervisor_refuse_privileged_service_objects(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "normal")
    with pytest.raises(TypeError, match="model-readable"):
        ToolContext(
            run_id=handle.run_id,
            workflow_id="workflow-x",
            facade=handle.services,  # type: ignore[arg-type]
            catalog=handle.bundle.catalog,
            diagnostics=None,  # type: ignore[arg-type]
            scenario_clock=None,
            seed=0,
            state=WorkflowState(
                run_id=handle.run_id,
                workflow_id="workflow-x",
                kind=WorkflowKind.ORDER_REQUEST,
                request_text="x",
            ),
            wall_clock=lambda: FIXED_UTC,
        )
    with pytest.raises(TypeError, match="model-readable"):
        SupervisorWorkflow(
            facade=handle.services,  # type: ignore[arg-type]
            catalog=handle.bundle.catalog,
            scenario_clock=None,
            seed=0,
            provider=None,  # type: ignore[arg-type]
            ledger=None,  # type: ignore[arg-type]
            pricing=None,  # type: ignore[arg-type]
            budget=None,  # type: ignore[arg-type]
            trace=None,  # type: ignore[arg-type]
            run_id=handle.run_id,
            workflow_id="workflow-x",
        )


def test_a04_execution_level_prohibited_calls_never_execute(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "normal")
    before = identity(handle.services, handle.run_id)
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, "Please add 2 aluminum mounting plates today."),
        [
            ScriptedTurn(calls=(ScriptedCall("apply_plan", {"run_id": "$run_id"}),)),
            ScriptedTurn(calls=(ScriptedCall("shell", {"command": "cat /etc/passwd"}),)),
            ScriptedTurn(calls=(ScriptedCall("sql", {"query": "UPDATE runs SET x=1"}),)),
        ],
    )
    assert run.outcome.terminal is WorkflowPhase.CYCLE_DETECTED
    assert run.outcome.reason_code == "repeated_invalid_turns"
    rejections = [item for item in run.records if item.kind == "provider_rejection"]
    assert [item.payload["error"]["details"]["tool_name"] for item in rejections] == [
        "apply_plan",
        "shell",
        "sql",
    ]
    assert {item.payload["error"]["details"]["reason"] for item in rejections} == {"unknown_tool"}
    assert not [item for item in run.records if item.kind == "tool_call"]
    assert run.outcome.budget["provider_attempts"] == 3
    assert run.outcome.budget["tool_executions"] == 0
    assert identity(handle.services, handle.run_id) == before


def test_a04_execution_level_wrong_run_and_unknown_field(tmp_path: Path) -> None:
    handle = fresh_run(tmp_path, "normal")
    run = run_supervisor(
        handle,
        SupervisorTask(WorkflowKind.ORDER_REQUEST, "Please add 2 aluminum mounting plates today."),
        [
            ScriptedTurn(
                calls=(
                    ScriptedCall(
                        "submit_order_interpretation",
                        {
                            "run_id": "run-other-fixture",
                            "request_type": "new_order",
                            "priority": "normal",
                        },
                    ),
                )
            ),
            ScriptedTurn(
                calls=(
                    ScriptedCall(
                        "submit_order_interpretation",
                        {
                            "run_id": "$run_id",
                            "request_type": "new_order",
                            "priority": "normal",
                            "approve": True,
                        },
                    ),
                )
            ),
        ],
        config=None,
    )
    results = [item.payload for item in run.records if item.kind == "tool_result"]
    assert results[0]["result"]["reason"] == "wrong_run"
    rejection = next(item for item in run.records if item.kind == "provider_rejection")
    assert rejection.payload["error"]["details"]["reason"] == "unexpected_field"
    assert run.outcome.terminal is WorkflowPhase.FAILED  # script exhausted -> provider failure
    assert run.outcome.reason_code == "provider_failure"
    assert run.outcome.proposal is None
