"""Command-line entry point for the deterministic ShopfloorPilot baseline."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from shopfloor_pilot.baseline import (
    BaselineExitCode,
    BaselineFailure,
    failure_payload,
    run_baseline,
    run_replay,
)
from shopfloor_pilot.metrics import MetricsError
from shopfloor_pilot.providers.readiness import run_provider_check
from shopfloor_pilot.reporting import ReportingError
from shopfloor_pilot.services import ApplicationServiceError
from shopfloor_pilot.simulation import SimulationError
from shopfloor_pilot.storage import StorageError
from shopfloor_pilot.workflows.models import CoordinatorError


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BaselineFailure(
            "invalid_arguments",
            message,
            BaselineExitCode.USAGE,
        )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="shopfloor-pilot", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    baseline = subparsers.add_parser(
        "baseline",
        help="run one deterministic no-LLM scenario",
    )
    baseline.add_argument(
        "--scenario",
        required=True,
        choices=("normal", "urgent_order", "machine_breakdown", "no_feasible_recovery"),
    )
    baseline.add_argument("--seed", required=True, type=int)
    baseline.add_argument(
        "--approval-policy",
        required=True,
        choices=("deterministic",),
        help="explicit named batch policy; there is no implicit approval",
    )
    baseline.add_argument("--output", required=True, type=Path)

    replay = subparsers.add_parser(
        "replay",
        help="verify and replay a portable event bundle without solving",
    )
    replay.add_argument("--events", required=True, type=Path)
    replay.add_argument("--output", required=True, type=Path)

    provider = subparsers.add_parser(
        "provider-check",
        help=(
            "run the labeled offline provider contract check, or the opt-in live "
            "DeepSeek readiness probe with --live"
        ),
    )
    provider.add_argument(
        "--live",
        action="store_true",
        help="contact DeepSeek; requires complete explicit paid-run settings",
    )
    provider.add_argument("--output", required=True, type=Path)
    provider.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="optional provider-only KEY=VALUE file; process environment wins",
    )

    agent = subparsers.add_parser(
        "agent",
        help=(
            "run one supervisor workflow; scripted-fake is labeled offline evidence, "
            "deepseek is an explicit paid run; stops at awaiting human approval"
        ),
    )
    agent.add_argument("--provider", required=True, choices=("scripted-fake", "deepseek"))
    agent.add_argument(
        "--scenario", required=True, choices=("normal", "urgent_order", "machine_breakdown")
    )
    agent.add_argument("--seed", required=True, type=int)
    agent.add_argument("--output", required=True, type=Path)
    agent.add_argument("--request-fixture", default=None, help="order request fixture ID")
    agent.add_argument(
        "--approval-policy",
        choices=("deterministic",),
        default=None,
        help="explicit offline batch approval/apply of the supervisor proposal; default none",
    )
    agent.add_argument(
        "--bootstrap-policy",
        choices=("deterministic",),
        default=None,
        help="explicit policy approving the scenario's pre-supervisor initial plan",
    )
    agent.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="optional provider-only KEY=VALUE file for --provider deepseek",
    )

    recovery = subparsers.add_parser(
        "recovery",
        help=(
            "run the S11 closed loop (initial plan, disruption, supervisor, approval, "
            "application, completion) with invariant checks; stops for human approval "
            "unless an explicit offline batch policy is given"
        ),
    )
    recovery.add_argument("--provider", required=True, choices=("scripted-fake", "deepseek"))
    recovery.add_argument(
        "--scenario",
        required=True,
        choices=("normal", "urgent_order", "machine_breakdown", "no_feasible_recovery"),
    )
    recovery.add_argument("--seed", required=True, type=int)
    recovery.add_argument("--output", required=True, type=Path)
    recovery.add_argument(
        "--approval-policy",
        choices=("deterministic",),
        default=None,
        help="explicit, recorded offline batch approval policy; default stops for a human",
    )
    recovery.add_argument("--env-file", type=Path, default=None)

    decide = subparsers.add_parser(
        "recovery-decide",
        help="record one explicit human decision on a paused recovery run and resume it",
    )
    decide.add_argument("--output", required=True, type=Path)
    decide.add_argument("--workflow-id", required=True)
    decide.add_argument("--proposal-hash", required=True)
    decide.add_argument("--actor", required=True, help="human:<name> or operator:<name>")
    decide.add_argument("--decision", required=True, choices=("approve", "reject"))
    decide.add_argument("--reason", default=None)
    decide.add_argument("--env-file", type=Path, default=None)

    evaluate = subparsers.add_parser(
        "evaluate",
        help=(
            "run a frozen S13 baseline, scripted-infrastructure, paired live, or "
            "live-reliability evaluation matrix"
        ),
    )
    evaluate.add_argument("--mode", required=True, choices=("baseline", "scripted", "deepseek"))
    evaluate.add_argument("--manifest", required=True, type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    evaluate.add_argument(
        "--campaign",
        choices=("offline", "paired_live", "reliability"),
        default=None,
        help="optional explicit campaign; defaults from mode",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "baseline":
            result = run_baseline(
                arguments.scenario,
                seed=arguments.seed,
                approval_policy=arguments.approval_policy,
                output=arguments.output,
            )
        elif arguments.command == "recovery" or (
            arguments.command == "agent"
            and arguments.approval_policy == "deterministic"
            and arguments.bootstrap_policy is None
            and arguments.request_fixture is None
            and arguments.scenario in ("urgent_order", "machine_breakdown")
        ):
            # The agent form without --bootstrap-policy used to be a usage error; with
            # an explicit batch policy it now means the S11 closed loop, where that one
            # recorded policy covers every decision.
            from shopfloor_pilot.workflows.scenario_runner import run_recovery_scenario

            recovery_result = run_recovery_scenario(
                provider_kind=arguments.provider,
                scenario_id=arguments.scenario,
                seed=arguments.seed,
                output=arguments.output,
                approval_policy=arguments.approval_policy,
                env_file=arguments.env_file,
            )
            print(json.dumps(recovery_result.summary, ensure_ascii=False, sort_keys=True))
            return recovery_result.exit_code
        elif arguments.command == "recovery-decide":
            from shopfloor_pilot.workflows.scenario_runner import decide_recovery_scenario

            decided = decide_recovery_scenario(
                output=arguments.output,
                workflow_id=arguments.workflow_id,
                proposal_hash=arguments.proposal_hash,
                actor=arguments.actor,
                approve=arguments.decision == "approve",
                reason=arguments.reason,
                env_file=arguments.env_file,
            )
            print(json.dumps(decided.summary, ensure_ascii=False, sort_keys=True))
            return decided.exit_code
        elif arguments.command == "agent":
            from shopfloor_pilot.agent.harness import run_agent_scenario

            agent_result = run_agent_scenario(
                provider_kind=arguments.provider,
                scenario_id=arguments.scenario,
                seed=arguments.seed,
                output=arguments.output,
                request_fixture_id=arguments.request_fixture,
                approval_policy=arguments.approval_policy,
                bootstrap_policy=arguments.bootstrap_policy,
                env_file=arguments.env_file,
            )
            print(json.dumps(agent_result.summary, ensure_ascii=False, sort_keys=True))
            return agent_result.exit_code
        elif arguments.command == "provider-check":
            outcome = run_provider_check(
                live=arguments.live,
                output=arguments.output,
                env_file=arguments.env_file,
            )
            print(json.dumps(outcome.report, ensure_ascii=False, sort_keys=True))
            return int(outcome.exit_code)
        elif arguments.command == "evaluate":
            from shopfloor_pilot.evaluation import run_evaluation

            evaluation = run_evaluation(
                mode=arguments.mode,
                manifest_path=arguments.manifest,
                output=arguments.output,
                campaign_id=arguments.campaign,
            )
            print(json.dumps(evaluation.report, ensure_ascii=False, sort_keys=True))
            return evaluation.exit_code
        else:
            result = run_replay(arguments.events, output=arguments.output)
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
        return int(result.exit_code)
    except BaselineFailure as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(exc.exit_code)
    except ApplicationServiceError as exc:
        failure = BaselineFailure(
            exc.code.value,
            str(exc),
            BaselineExitCode.WORKFLOW_FAILURE,
            exc.details,
        )
        print(json.dumps(failure.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(failure.exit_code)
    except CoordinatorError as exc:
        failure = BaselineFailure(
            exc.code.value,
            exc.message,
            BaselineExitCode.WORKFLOW_FAILURE,
            exc.details,
        )
        print(json.dumps(failure.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(failure.exit_code)
    except SimulationError as exc:
        failure = BaselineFailure(
            "simulation_rejected",
            str(exc),
            BaselineExitCode.WORKFLOW_FAILURE,
        )
        print(json.dumps(failure.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(failure.exit_code)
    except (MetricsError, ReportingError, StorageError, sqlite3.Error, OSError) as exc:
        failure = BaselineFailure(
            "artifact_or_storage_failure",
            str(exc),
            BaselineExitCode.ARTIFACT_FAILURE,
        )
        print(json.dumps(failure.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(failure.exit_code)
    except Exception as exc:  # pragma: no cover - exercised at the process boundary
        print(json.dumps(failure_payload(exc), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(BaselineExitCode.SOFTWARE_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
