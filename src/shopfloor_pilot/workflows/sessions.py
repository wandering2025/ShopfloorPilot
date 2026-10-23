"""Shared construction of supervisor sessions for the S11 runner and the S12 dashboard.

A session is built inside the coordinator's worker thread because the S09 ledger
and S10 trace store hold thread-bound SQLite connections. The scripted fake is
used when no paid-run settings are supplied; otherwise the bounded DeepSeek
transport is built from those settings. Credentials only ever travel inside the
settings object and are registered with the trace store for redaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from shopfloor_pilot.agent.fake import (
    DeterministicPolicyProvider,
    ScriptedTurn,
    TemplateScriptedProvider,
)
from shopfloor_pilot.agent.harness import OFFLINE_ACCOUNTING_RATES, OFFLINE_BUDGET
from shopfloor_pilot.agent.trace import TraceStore
from shopfloor_pilot.agent.workflow import (
    SupervisorBudget,
    SupervisorConfig,
    SupervisorTask,
    SupervisorWorkflow,
)
from shopfloor_pilot.budgets import BudgetLedger, ProviderLimits
from shopfloor_pilot.domain.fixtures import ScenarioBundle
from shopfloor_pilot.providers.base import Provider
from shopfloor_pilot.providers.clock import SystemClock
from shopfloor_pilot.services import ApplicationServices

from .coordinator import SessionFactory, SupervisorSession
from .models import DecisionRecord


def supervisor_session_factory(
    *,
    services: ApplicationServices,
    bundle: ScenarioBundle,
    seed: int,
    decision: DecisionRecord,
    task: SupervisorTask,
    ledger_path: Path,
    trace_path: Path,
    wall_clock: Callable[[], str],
    settings: Any = None,
    turns: Sequence[ScriptedTurn] = (),
    offline_provider_kind: str = "scripted-fake",
    offline_limits: ProviderLimits | None = None,
    on_close: Callable[[TraceStore], None] | None = None,
) -> SessionFactory:
    """Return a factory that builds one supervisor session for ``decision``."""

    def build() -> SupervisorSession:
        provider: Provider
        secrets: tuple[str, ...] = ()
        if settings is None:
            if offline_provider_kind == "deterministic-baseline":
                provider = DeterministicPolicyProvider(list(turns))
            elif offline_provider_kind == "scripted-fake":
                provider = TemplateScriptedProvider(list(turns))
            else:
                raise ValueError(f"unsupported offline provider: {offline_provider_kind}")
            pricing = OFFLINE_ACCOUNTING_RATES
            budget = (
                OFFLINE_BUDGET
                if offline_limits is None
                else replace(OFFLINE_BUDGET, limits=offline_limits)
            )
            config = SupervisorConfig()
        else:
            from shopfloor_pilot.providers.deepseek import DeepSeekProvider

            provider = DeepSeekProvider(settings.connection)
            pricing = settings.pricing
            budget = SupervisorBudget(
                currency=settings.currency,
                run_cap_micros=settings.run_cap_micros,
                workflow_cap_micros=settings.workflow_cap_micros,
                limits=settings.connection.limits,
            )
            config = SupervisorConfig(
                max_output_tokens=settings.max_output_tokens,
                force_tool_choice=not settings.connection.thinking,
            )
            secrets = settings.secrets
        ledger = BudgetLedger(ledger_path)
        trace = TraceStore(trace_path, secrets=secrets)
        workflow = SupervisorWorkflow(
            facade=services.for_model(),
            catalog=bundle.catalog,
            scenario_clock=bundle.scenario.clock,
            seed=seed,
            provider=provider,
            ledger=ledger,
            pricing=pricing,
            budget=budget,
            trace=trace,
            run_id=decision.run_id,
            workflow_id=decision.workflow_id,
            config=config,
            clock=SystemClock(),
            wall_clock=wall_clock,
        )

        def close() -> None:
            try:
                if on_close is not None:
                    on_close(trace)
            finally:
                trace.close()
                ledger.close()
                closer = getattr(provider, "close", None)
                if callable(closer):
                    closer()

        return SupervisorSession(workflow=workflow, task=task, close=close, ledger_path=ledger_path)

    return build


__all__ = ["supervisor_session_factory"]
