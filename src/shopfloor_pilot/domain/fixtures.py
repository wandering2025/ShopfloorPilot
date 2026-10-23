"""Typed fixture schemas, deterministic materialization, and manifest verification."""

from __future__ import annotations

import hashlib
import random
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import (
    Identifier,
    PositiveSeconds,
    Seconds,
    Sha256,
    StrictModel,
)
from .models import (
    Capability,
    DomainCatalog,
    MachineStatus,
    Observation,
    OperationalSnapshot,
    Order,
    ResourceKind,
    TelemetryValue,
    TimeWindow,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENARIO_ROOT = REPOSITORY_ROOT / "scenarios"
# S13 evaluation manifests are co-located for the public CLI but are validated by
# shopfloor_pilot.evaluation.manifest, not by the domain-fixture manifest.
EVALUATION_MANIFEST_PATHS = frozenset(
    {"evaluation.json", "evaluation_v1.json", "evaluation_v2.json"}
)


class ScenarioOutcome(StrEnum):
    COMPLETES = "completes"
    REPLAN_REQUIRED = "replan_required"
    RECOVERY_FEASIBLE = "recovery_feasible"
    NO_FEASIBLE_RECOVERY = "no_feasible_recovery"


class InterventionKind(StrEnum):
    CHECKPOINT = "checkpoint"
    ADD_ORDER = "add_order"
    MACHINE_FAULT = "machine_fault"
    MACHINE_RESTORED = "machine_restored"


class ScenarioClock(StrictModel):
    origin_label: str = Field(min_length=1, max_length=80)
    start_seconds: Seconds
    shift_boundary_seconds: PositiveSeconds

    @model_validator(mode="after")
    def validate_clock(self) -> Self:
        if self.shift_boundary_seconds <= self.start_seconds:
            raise ValueError("shift boundary must be after scenario start")
        return self


class ScenarioDefinition(StrictModel):
    schema_version: Literal[1]
    scenario_id: Identifier
    display_name: str = Field(min_length=1, max_length=100)
    seed: int = Field(strict=True, ge=0, le=2**32 - 1)
    run_id: Identifier
    clock: ScenarioClock
    hard_horizon_seconds: PositiveSeconds
    orders: tuple[Order, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        order_ids = [item.order_id for item in self.orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("scenario order_id values must be unique")
        if self.clock.start_seconds >= self.hard_horizon_seconds:
            raise ValueError("hard horizon must be after scenario start")
        for order in self.orders:
            if order.release_time_seconds > self.hard_horizon_seconds:
                raise ValueError(f"order {order.order_id} releases after the hard horizon")
        return self


class ScheduledIntervention(StrictModel):
    intervention_id: Identifier
    at_seconds: Seconds
    kind: InterventionKind
    target_machine_id: Identifier | None = None
    injected_status: MachineStatus | None = None
    order_to_add: Order | None = None
    unavailable_until_seconds: Seconds | None = None
    private_truth_ref: Identifier | None = None

    @model_validator(mode="after")
    def validate_intervention(self) -> Self:
        if self.kind is InterventionKind.CHECKPOINT:
            if any(
                value is not None
                for value in (
                    self.target_machine_id,
                    self.injected_status,
                    self.order_to_add,
                    self.unavailable_until_seconds,
                    self.private_truth_ref,
                )
            ):
                raise ValueError("checkpoint interventions cannot inject operational values")
        elif self.kind is InterventionKind.ADD_ORDER:
            if self.order_to_add is None:
                raise ValueError("add_order intervention requires order_to_add")
            if self.target_machine_id is not None or self.injected_status is not None:
                raise ValueError("add_order intervention cannot target a machine")
        elif self.kind is InterventionKind.MACHINE_FAULT:
            if self.target_machine_id is None or self.injected_status is not MachineStatus.DOWN:
                raise ValueError(
                    "machine_fault requires target_machine_id and injected_status=down"
                )
            if self.order_to_add is not None:
                raise ValueError("machine_fault cannot add an order")
        elif self.kind is InterventionKind.MACHINE_RESTORED:
            if (
                self.target_machine_id is None
                or self.injected_status is not MachineStatus.AVAILABLE
            ):
                raise ValueError(
                    "machine_restored requires target_machine_id and injected_status=available"
                )
        if (
            self.unavailable_until_seconds is not None
            and self.unavailable_until_seconds <= self.at_seconds
        ):
            raise ValueError("unavailable_until_seconds must be after intervention time")
        return self


class FaultTruth(StrictModel):
    truth_id: Identifier
    machine_id: Identifier
    cause_label: str = Field(min_length=1, max_length=120)
    starts_at_seconds: Seconds
    clears_at_seconds: Seconds | None = None

    @model_validator(mode="after")
    def validate_truth_window(self) -> Self:
        if self.clears_at_seconds is not None and self.clears_at_seconds <= self.starts_at_seconds:
            raise ValueError("truth clear time must be after truth start")
        return self


class SyntheticSignalSpec(StrictModel):
    name: Identifier
    base_value: int = Field(strict=True)
    jitter_min: int = Field(strict=True)
    jitter_max: int = Field(strict=True)
    unit: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_jitter(self) -> Self:
        if self.jitter_max < self.jitter_min:
            raise ValueError("jitter_max must be greater than or equal to jitter_min")
        return self


class ObservationTemplate(StrictModel):
    observation_id: Identifier
    machine_id: Identifier
    observed_at_seconds: Seconds
    signals: tuple[SyntheticSignalSpec, ...] = Field(min_length=1)
    public_summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_signal_names(self) -> Self:
        names = [item.name for item in self.signals]
        if len(names) != len(set(names)):
            raise ValueError("signal names must be unique within an observation template")
        return self


class DiagnosticVariant(StrictModel):
    variant_id: Identifier
    truth_ref: Identifier
    plausible_hypotheses: tuple[str, ...] = Field(min_length=2)
    ambiguity_reason: str = Field(min_length=1, max_length=300)


class EvaluatorScenario(StrictModel):
    schema_version: Literal[1]
    scenario_id: Identifier
    seed: int = Field(strict=True, ge=0, le=2**32 - 1)
    interventions: tuple[ScheduledIntervention, ...] = Field(min_length=1)
    fault_truth: tuple[FaultTruth, ...] = ()
    observation_templates: tuple[ObservationTemplate, ...] = ()
    diagnostic_variant: DiagnosticVariant | None = None

    @model_validator(mode="after")
    def validate_evaluator_data(self) -> Self:
        intervention_ids = [item.intervention_id for item in self.interventions]
        truth_ids = [item.truth_id for item in self.fault_truth]
        observation_ids = [item.observation_id for item in self.observation_templates]
        for values, label in (
            (intervention_ids, "intervention_id"),
            (truth_ids, "truth_id"),
            (observation_ids, "observation_id"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} values must be unique")
        if list(self.interventions) != sorted(
            self.interventions, key=lambda item: (item.at_seconds, item.intervention_id)
        ):
            raise ValueError("interventions must be sorted by time and ID")
        truth_id_set = set(truth_ids)
        for intervention in self.interventions:
            if intervention.private_truth_ref not in (None, *truth_id_set):
                raise ValueError("intervention private_truth_ref is unknown")
        if self.diagnostic_variant and self.diagnostic_variant.truth_ref not in truth_id_set:
            raise ValueError("diagnostic variant truth_ref is unknown")
        return self


class OutcomeAssertion(StrictModel):
    code: Identifier
    message: str = Field(min_length=1, max_length=300)


class AlternateRecovery(StrictModel):
    failed_machine_id: Identifier
    alternate_machine_id: Identifier
    capability: Capability
    restart_full_duration_seconds: PositiveSeconds


class HardContradiction(StrictModel):
    capability: Capability
    required_duration_seconds: PositiveSeconds
    capable_machine_ids: tuple[Identifier, ...] = Field(min_length=1)
    blocked_windows: tuple[TimeWindow, ...] = Field(min_length=1)
    hard_horizon_seconds: PositiveSeconds
    explanation: str = Field(min_length=1, max_length=400)


class ScenarioExpectation(StrictModel):
    schema_version: Literal[1]
    scenario_id: Identifier
    expected_outcome: ScenarioOutcome
    assertions: tuple[OutcomeAssertion, ...] = Field(min_length=1)
    alternate_recovery: AlternateRecovery | None = None
    hard_contradiction: HardContradiction | None = None

    @model_validator(mode="after")
    def validate_expected_evidence(self) -> Self:
        if self.expected_outcome is ScenarioOutcome.RECOVERY_FEASIBLE:
            if self.alternate_recovery is None:
                raise ValueError("recovery_feasible expectation requires alternate_recovery")
        elif self.alternate_recovery is not None:
            raise ValueError("alternate_recovery is only valid for recovery_feasible")
        if self.expected_outcome is ScenarioOutcome.NO_FEASIBLE_RECOVERY:
            if self.hard_contradiction is None:
                raise ValueError("no_feasible_recovery requires a hard_contradiction")
        elif self.hard_contradiction is not None:
            raise ValueError("hard_contradiction is only valid for no_feasible_recovery")
        return self


class UnitContract(StrictModel):
    simulated_time: Literal["integer_seconds"]
    intervals: Literal["half_open"]
    quantity: Literal["integer_units_1_to_10_one_route_chain_per_unit"]
    telemetry: Literal["scaled_integers_with_explicit_units"]


class ScenarioManifestEntry(StrictModel):
    scenario_id: Identifier
    seed: int = Field(strict=True, ge=0, le=2**32 - 1)
    public_path: str = Field(min_length=1)
    public_sha256: Sha256
    evaluator_path: str = Field(min_length=1)
    evaluator_sha256: Sha256
    expectation_path: str = Field(min_length=1)
    expectation_sha256: Sha256
    expected_outcome: ScenarioOutcome


class FixtureManifest(StrictModel):
    schema_version: Literal[1]
    catalog_path: str = Field(min_length=1)
    catalog_sha256: Sha256
    units: UnitContract
    scenarios: tuple[ScenarioManifestEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        ids = [item.scenario_id for item in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest scenario_id values must be unique")
        return self


class PublicScenarioView(StrictModel):
    """Allowlisted projection; evaluator truth and interventions have no fields here."""

    schema_version: Literal[1]
    scenario_id: Identifier
    run_id: Identifier
    simulated_time_seconds: Seconds
    observations: tuple[Observation, ...]


class ScenarioBundle(StrictModel):
    catalog: DomainCatalog
    scenario: ScenarioDefinition
    evaluator: EvaluatorScenario
    expectation: ScenarioExpectation

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if not (
            self.scenario.scenario_id == self.evaluator.scenario_id == self.expectation.scenario_id
        ):
            raise ValueError("public, evaluator, and expectation scenario IDs must match")
        if self.scenario.seed != self.evaluator.seed:
            raise ValueError("public and evaluator seeds must match")
        if len(self.scenario.orders) != 8:
            raise ValueError("each S01 scenario must contain exactly eight initial orders")
        processing = [
            machine for machine in self.catalog.machines if machine.kind is ResourceKind.PROCESSING
        ]
        inspection = [
            machine for machine in self.catalog.machines if machine.kind is ResourceKind.INSPECTION
        ]
        if len(processing) != 3 or len(inspection) != 1:
            raise ValueError(
                "S01 cell requires three processing machines and one inspection station"
            )
        if not 2 <= len(self.catalog.products) <= 3:
            raise ValueError("S01 catalog requires two or three product routes")
        product_ids = {item.product_id for item in self.catalog.products}
        machine_ids = {item.machine_id for item in self.catalog.machines}
        for order in self.scenario.orders:
            if order.product_id not in product_ids:
                raise ValueError(f"order {order.order_id} references an unknown product")
        for intervention in self.evaluator.interventions:
            if intervention.target_machine_id not in (None, *machine_ids):
                raise ValueError("intervention targets an unknown machine")
            if (
                intervention.order_to_add
                and intervention.order_to_add.product_id not in product_ids
            ):
                raise ValueError("intervention order references an unknown product")
        for truth in self.evaluator.fault_truth:
            if truth.machine_id not in machine_ids:
                raise ValueError("fault truth references an unknown machine")
        for observation in self.evaluator.observation_templates:
            if observation.machine_id not in machine_ids:
                raise ValueError("observation template references an unknown machine")
            if observation.observed_at_seconds > self.scenario.hard_horizon_seconds:
                raise ValueError("observation occurs after the hard horizon")
        capability_counts: dict[Capability, int] = {}
        for machine in processing:
            for capability in machine.capabilities:
                capability_counts[capability] = capability_counts.get(capability, 0) + 1
        if not any(count >= 2 for count in capability_counts.values()):
            raise ValueError("at least one processing capability needs an alternate machine")
        return self


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(f"fixture path escapes scenario root: {relative}")
    return candidate


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_model[ModelT: StrictModel](path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate_json(path.read_bytes())


def load_manifest(root: Path = DEFAULT_SCENARIO_ROOT) -> FixtureManifest:
    return _load_model(root / "manifest.json", FixtureManifest)


def _verified_path(root: Path, relative: str, expected_hash: str) -> Path:
    path = _safe_path(root, relative)
    if not path.is_file():
        raise ValueError(f"fixture is missing: {relative}")
    actual = _sha256(path)
    if actual != expected_hash:
        raise ValueError(f"fixture hash mismatch for {relative}: {actual}")
    return path


def load_scenario(scenario_id: str, root: Path = DEFAULT_SCENARIO_ROOT) -> ScenarioBundle:
    manifest = load_manifest(root)
    entries = {item.scenario_id: item for item in manifest.scenarios}
    if scenario_id not in entries:
        raise ValueError(f"scenario is not listed in manifest: {scenario_id}")
    entry = entries[scenario_id]
    catalog_path = _verified_path(root, manifest.catalog_path, manifest.catalog_sha256)
    public_path = _verified_path(root, entry.public_path, entry.public_sha256)
    evaluator_path = _verified_path(root, entry.evaluator_path, entry.evaluator_sha256)
    expectation_path = _verified_path(root, entry.expectation_path, entry.expectation_sha256)
    scenario = _load_model(public_path, ScenarioDefinition)
    evaluator = _load_model(evaluator_path, EvaluatorScenario)
    expectation = _load_model(expectation_path, ScenarioExpectation)
    if entry.seed != scenario.seed or entry.expected_outcome is not expectation.expected_outcome:
        raise ValueError("manifest seed or expected outcome disagrees with fixture content")
    return ScenarioBundle(
        catalog=_load_model(catalog_path, DomainCatalog),
        scenario=scenario,
        evaluator=evaluator,
        expectation=expectation,
    )


def load_all_scenarios(root: Path = DEFAULT_SCENARIO_ROOT) -> tuple[ScenarioBundle, ...]:
    manifest = load_manifest(root)
    expected_paths = {"manifest.json", manifest.catalog_path}
    for entry in manifest.scenarios:
        expected_paths.update({entry.public_path, entry.evaluator_path, entry.expectation_path})
    actual_paths = {
        path.relative_to(root).as_posix() for path in root.rglob("*.json") if path.is_file()
    } - EVALUATION_MANIFEST_PATHS
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unlisted = sorted(actual_paths - expected_paths)
        raise ValueError(f"manifest coverage mismatch: missing={missing}, unlisted={unlisted}")
    return tuple(load_scenario(entry.scenario_id, root) for entry in manifest.scenarios)


def materialize_initial_snapshot(
    bundle: ScenarioBundle,
    *,
    run_id: str | None = None,
) -> OperationalSnapshot:
    """Create a version-zero run snapshot from one verified scenario bundle.

    ``run_id`` may replace the fixture identity when an operator deliberately resets
    the scenario. All manufacturing inputs remain the verified fixture content.
    """

    scenario = bundle.scenario
    return OperationalSnapshot(
        schema_version=1,
        run_id=run_id or scenario.run_id,
        state_version=0,
        plan_revision=0,
        simulated_time_seconds=scenario.clock.start_seconds,
        hard_horizon_seconds=scenario.hard_horizon_seconds,
        machines=bundle.catalog.machines,
        orders=scenario.orders,
        operations=(),
        attempts=(),
    )


def project_public_view(
    bundle: ScenarioBundle,
    simulated_time_seconds: int,
    *,
    seed_override: int | None = None,
) -> PublicScenarioView:
    """Materialize only observations visible at ``simulated_time_seconds``.

    Seed variation affects only each signal's documented integer jitter. Initial
    operational data, intervention schedules, truth, and expectation data are not
    copied into this allowlisted public type.
    """

    if type(simulated_time_seconds) is not int or simulated_time_seconds < 0:
        raise ValueError("simulated_time_seconds must be a non-negative integer")
    if simulated_time_seconds > bundle.scenario.hard_horizon_seconds:
        raise ValueError("public projection time exceeds the hard horizon")
    seed = bundle.scenario.seed if seed_override is None else seed_override
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed_override must be a uint32 integer")
    observations: list[Observation] = []
    for template in bundle.evaluator.observation_templates:
        if template.observed_at_seconds > simulated_time_seconds:
            continue
        signals: list[TelemetryValue] = []
        for signal in template.signals:
            rng = random.Random(
                f"shopfloor-pilot:{bundle.scenario.scenario_id}:{seed}:"
                f"{template.observation_id}:{signal.name}"
            )
            value = signal.base_value + rng.randint(signal.jitter_min, signal.jitter_max)
            signals.append(TelemetryValue(name=signal.name, value=value, unit=signal.unit))
        observations.append(
            Observation(
                schema_version=1,
                observation_id=template.observation_id,
                run_id=bundle.scenario.run_id,
                machine_id=template.machine_id,
                observed_at_seconds=template.observed_at_seconds,
                signals=tuple(signals),
                summary=template.public_summary,
            )
        )
    return PublicScenarioView(
        schema_version=1,
        scenario_id=bundle.scenario.scenario_id,
        run_id=bundle.scenario.run_id,
        simulated_time_seconds=simulated_time_seconds,
        observations=tuple(observations),
    )
