"""S06-A10/A11/A12 CLI policy, failure, and portable replay checks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shopfloor_pilot.baseline import BaselineExitCode, BaselineFailure, run_replay
from shopfloor_pilot.cli import main


def test_batch_mode_has_no_implicit_approval_policy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "must-not-exist"
    exit_code = main(
        [
            "baseline",
            "--scenario",
            "normal",
            "--seed",
            "0",
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert exit_code == BaselineExitCode.USAGE
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "invalid_arguments"
    assert "--approval-policy" in payload["message"]
    assert not output.exists()


def test_nonempty_output_is_rejected_without_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "owned.txt"
    marker.write_text("user-owned\n", encoding="utf-8")
    exit_code = main(
        [
            "baseline",
            "--scenario",
            "normal",
            "--seed",
            "0",
            "--approval-policy",
            "deterministic",
            "--output",
            str(output),
        ]
    )
    payload = json.loads(capsys.readouterr().err)
    assert exit_code == BaselineExitCode.INPUT_ERROR
    assert payload["reason_code"] == "output_not_empty"
    assert marker.read_text(encoding="utf-8") == "user-owned\n"


def test_sqlite_initialization_error_has_structured_artifact_failure_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_database_initialization(_repository: object) -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(
        "shopfloor_pilot.baseline.SQLiteStateRepository.initialize",
        reject_database_initialization,
    )
    output = tmp_path / "unopenable-database"
    exit_code = main(
        [
            "baseline",
            "--scenario",
            "normal",
            "--seed",
            "0",
            "--approval-policy",
            "deterministic",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert captured.out == ""
    assert exit_code == BaselineExitCode.ARTIFACT_FAILURE
    assert payload == {
        "schema_version": 1,
        "status": "failed",
        "reason_code": "artifact_or_storage_failure",
        "message": "unable to open database file",
        "exit_code": 5,
        "details": {},
    }


def test_successful_cli_process_creates_a_fresh_run_and_structured_output(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    output = tmp_path / "fresh-cli-output"
    environment = os.environ.copy()
    environment.pop("DEEPSEEK_API_KEY", None)
    environment["PYTHONPATH"] = str(repository_root / "src")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shopfloor_pilot.cli",
            "baseline",
            "--scenario",
            "normal",
            "--seed",
            "0",
            "--approval-policy",
            "deterministic",
            "--output",
            str(output),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["output_directory"] == str(output.resolve())
    assert (output / "state.sqlite3").is_file()
    report = json.loads(
        (output / "proposals" / "initial" / "proposal_comparison.json").read_text(encoding="utf-8")
    )
    assert report["decision_latency"]["simulated_elapsed_seconds"] == 0
    assert report["decision_latency"]["wall_clock_elapsed_ms"] >= 0


@pytest.mark.parametrize("scenario_id", ("normal", "machine_breakdown"))
def test_portable_replay_matches_snapshot_and_metrics_without_solving(
    baseline_runs: dict[str, tuple[object, Path]],
    scenario_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, source = baseline_runs[scenario_id]

    def solver_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("portable replay must not invoke the scheduler")

    monkeypatch.setattr("shopfloor_pilot.scheduling.solver.solve_schedule", solver_forbidden)
    monkeypatch.setattr("shopfloor_pilot.services.application.solve_schedule", solver_forbidden)
    replayed = run_replay(source / "events.jsonl", output=tmp_path / scenario_id)

    assert replayed.snapshot_hash == original.snapshot_hash
    assert replayed.event_count == original.event_count
    assert json.loads((tmp_path / scenario_id / "metrics.json").read_text(encoding="utf-8")) == (
        json.loads((source / "metrics.json").read_text(encoding="utf-8"))
    )


def test_tampered_replay_is_structurally_rejected(
    baseline_runs: dict[str, tuple[object, Path]],
    tmp_path: Path,
) -> None:
    _, source = baseline_runs["normal"]
    lines = (source / "events.jsonl").read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["operational_stream_hash"] = "0" * 64
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(
        "\n".join((json.dumps(header, sort_keys=True), *lines[1:])) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BaselineFailure) as captured:
        run_replay(tampered, output=tmp_path / "rejected")
    assert captured.value.reason_code == "replay_rejected"
    assert captured.value.exit_code is BaselineExitCode.WORKFLOW_FAILURE


def test_console_entry_point_is_declared() -> None:
    import tomllib

    configuration = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert configuration["project"]["scripts"]["shopfloor-pilot"] == ("shopfloor_pilot.cli:main")
