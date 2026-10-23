"""Child process for S11-A10: dies hard at a deterministic point of application.

Usage: crash_child.py DB SCENARIO RUN WORKFLOW PROPOSAL HASH APPROVAL MODE
MODE is ``crash_before_commit`` (exit inside the open write transaction) or
``exit_after_commit`` (exit before the caller can observe the result).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from shopfloor_pilot.domain.fixtures import load_scenario
from shopfloor_pilot.services import ApplicationServices
from shopfloor_pilot.storage import SQLiteStateRepository
from shopfloor_pilot.workflows import RecoveryCoordinator


def main(argv: list[str]) -> None:
    database, scenario_id, run_id, workflow_id, proposal_id, proposal_hash, approval_id, mode = argv
    bundle = load_scenario(scenario_id)
    services = ApplicationServices(SQLiteStateRepository(Path(database)), bundle.catalog, seed=0)
    coordinator = RecoveryCoordinator(services)

    def crash(point: str) -> None:
        if mode == "crash_before_commit" and point == "before_commit":
            os._exit(17)  # no rollback, no cleanup: a hard process death

    coordinator.apply(
        run_id,
        workflow_id,
        proposal_id=proposal_id,
        proposal_hash=proposal_hash,
        approval_id=approval_id,
        _failure_injector=crash,
    )
    if mode == "exit_after_commit":
        os._exit(23)  # committed, but the client never sees the response
    os._exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
