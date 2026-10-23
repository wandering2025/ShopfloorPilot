"""S02-A05 local concurrent-writer acceptance."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from shopfloor_pilot.domain.canonical import canonical_hash
from shopfloor_pilot.domain.models import OperationalSnapshot
from shopfloor_pilot.storage import (
    DatabaseBusyError,
    MutationResult,
    SQLiteStateRepository,
    VersionConflictError,
)


def test_a05_two_independent_writers_from_one_version_have_exactly_one_winner(
    repository: SQLiteStateRepository,
    database_path: Path,
    initial_snapshot: OperationalSnapshot,
    apply_time: Callable[..., MutationResult],
) -> None:
    run_id = initial_snapshot.run_id
    barrier = threading.Barrier(2)

    def write(key: str, target: int) -> MutationResult | VersionConflictError | DatabaseBusyError:
        independent = SQLiteStateRepository(database_path, busy_timeout_seconds=0.25)
        barrier.wait(timeout=5)
        try:
            return apply_time(
                independent,
                run_id=run_id,
                expected_state_version=0,
                target_time_seconds=target,
                idempotency_key=key,
            )
        except (VersionConflictError, DatabaseBusyError) as exc:
            return exc

    attempts = (
        ("mutation-a05-writer-one", 10),
        ("mutation-a05-writer-two", 20),
    )
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sqlite-writer") as executor:
        futures = [executor.submit(write, key, target) for key, target in attempts]
        outcomes = [future.result(timeout=5) for future in futures]

    winners = [item for item in outcomes if isinstance(item, MutationResult)]
    losers = [
        item for item in outcomes if isinstance(item, (VersionConflictError, DatabaseBusyError))
    ]
    assert len(winners) == len(losers) == 1
    winner = winners[0]
    assert winner.previous_state_version == 0
    assert winner.resulting_state_version == 1

    final = repository.get_checkpoint(run_id)
    assert final.snapshot.state_version == 1
    assert final.snapshot.simulated_time_seconds in {10, 20}
    assert canonical_hash(final.snapshot) == canonical_hash(winner.checkpoint.snapshot)
    assert repository.list_events(run_id) == winner.events
    assert len(repository.list_checkpoints(run_id)) == 2
    assert repository.replay_run(run_id) == final

    target_by_key = dict(attempts)
    exact_retry = apply_time(
        SQLiteStateRepository(database_path),
        run_id=run_id,
        expected_state_version=0,
        target_time_seconds=target_by_key[winner.idempotency_key],
        idempotency_key=winner.idempotency_key,
    )
    assert exact_retry.replayed_idempotent is True
    assert exact_retry.checkpoint == winner.checkpoint
    assert len(repository.list_events(run_id)) == 1

    loser_key = next(key for key, _ in attempts if key != winner.idempotency_key)
    with pytest.raises(VersionConflictError):
        apply_time(
            SQLiteStateRepository(database_path),
            run_id=run_id,
            expected_state_version=0,
            target_time_seconds=target_by_key[loser_key],
            idempotency_key=loser_key,
        )
