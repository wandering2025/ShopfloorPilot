"""Offline fixtures shared by S08 retrieval and diagnostic acceptance tests."""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from shopfloor_pilot.diagnostics import DiagnosticService
from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.domain.models import EventType, Observation, OperationalSnapshot
from shopfloor_pilot.events import (
    EventDraft,
    MachineDownPayload,
    ObservationRecordedPayload,
    TimeAdvancedPayload,
)
from shopfloor_pilot.retrieval import LexicalRetriever
from shopfloor_pilot.storage import MutationResult, SQLiteStateRepository

FIXED_UTC = "2026-10-20T20:00:00+02:00"


@dataclass(frozen=True)
class DiagnosticHarness:
    """An authoritative local run exposed to diagnostics through readers only."""

    repository: SQLiteStateRepository
    service: DiagnosticService
    run_id: str

    def persist(
        self,
        observations: Sequence[Observation] = (),
        *,
        at_seconds: int = 2_700,
        machine_down: bool = False,
        machine_id: str = "CNC02",
        mutation_id: str = "mutation-diagnostic-evidence",
    ) -> MutationResult:
        checkpoint = self.repository.get_checkpoint(self.run_id)
        drafts: list[EventDraft] = []
        if machine_down:
            down = MachineDownPayload(machine_id=machine_id, status="down")
            drafts.append(
                EventDraft(
                    simulated_time_seconds=at_seconds,
                    event_type=EventType.MACHINE_DOWN,
                    payload=down.model_dump(mode="json"),
                    recorded_at_utc=FIXED_UTC,
                )
            )
        for observation in observations:
            payload = ObservationRecordedPayload(observation=observation)
            drafts.append(
                EventDraft(
                    simulated_time_seconds=at_seconds,
                    event_type=EventType.OBSERVATION_RECORDED,
                    payload=payload.model_dump(mode="json"),
                    recorded_at_utc=FIXED_UTC,
                )
            )
        advance = TimeAdvancedPayload(target_time_seconds=at_seconds)
        drafts.append(
            EventDraft(
                simulated_time_seconds=at_seconds,
                event_type=EventType.TIME_ADVANCED,
                payload=advance.model_dump(mode="json"),
                recorded_at_utc=FIXED_UTC,
            )
        )
        return self.repository.apply_mutation(
            run_id=self.run_id,
            expected_state_version=checkpoint.snapshot.state_version,
            idempotency_key=mutation_id,
            command_name="record_diagnostic_fixture",
            command_payload={"at_seconds": at_seconds},
            events=tuple(drafts),
            created_at_utc=FIXED_UTC,
        )


@pytest.fixture(autouse=True)
def deny_network_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every S08 test independent of credentials and network access."""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("S08 retrieval/diagnostics must not access the network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def retriever(deny_network_and_credentials: None) -> LexicalRetriever:
    """Construct the complete local index only after network/credential denial."""

    assert deny_network_and_credentials is None
    return LexicalRetriever.from_default_corpus()


@pytest.fixture
def diagnostic_harness_factory(
    tmp_path: Path,
    retriever: LexicalRetriever,
) -> Callable[..., DiagnosticHarness]:
    """Create isolated SQLite runs while giving production code only read callables."""

    created = 0

    def build(*, run_id: str = "run-s08-diagnostic") -> DiagnosticHarness:
        nonlocal created
        created += 1
        bundle = load_scenario("normal")
        snapshot = OperationalSnapshot(
            schema_version=1,
            run_id=run_id,
            state_version=0,
            plan_revision=0,
            simulated_time_seconds=bundle.scenario.clock.start_seconds,
            hard_horizon_seconds=bundle.scenario.hard_horizon_seconds,
            machines=bundle.catalog.machines,
            orders=bundle.scenario.orders,
            operations=(),
            attempts=(),
        )
        repository = SQLiteStateRepository(tmp_path / f"diagnostic-{created}.db")
        repository.initialize(applied_at_utc=FIXED_UTC)
        repository.create_run(snapshot, created_at_utc=FIXED_UTC)
        service = DiagnosticService(
            run_id=run_id,
            get_checkpoint=repository.get_checkpoint,
            list_events=repository.list_events,
            retriever=retriever,
        )
        return DiagnosticHarness(
            repository=repository,
            service=service,
            run_id=run_id,
        )

    return build
