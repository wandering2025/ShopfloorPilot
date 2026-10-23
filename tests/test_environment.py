"""Acceptance-oriented readiness tests for the E00 environment."""

from __future__ import annotations

from shopfloor_pilot.environment import (
    REQUIRED_IMPORTS,
    interpreter_report,
    probe_cp_sat,
    probe_imports,
    probe_package_origin,
    probe_plotly,
    probe_pydantic,
    probe_simpy,
    probe_sqlite,
    probe_streamlit_app_test,
)


def test_interpreter_is_isolated_python_312() -> None:
    info = interpreter_report()
    assert info["version"].startswith("3.12."), info
    assert info["virtualenv"] is True, info
    assert info["system_site_packages_disabled"] is True, info


def test_package_resolves_to_checkout_source() -> None:
    result = probe_package_origin()
    assert result.ok, result.detail


def test_required_imports() -> None:
    results = probe_imports()
    assert [result.name for result in results] == list(REQUIRED_IMPORTS)
    failed = [result for result in results if not result.ok]
    assert not failed, failed


def test_cp_sat_known_optimum() -> None:
    result = probe_cp_sat()
    assert result.ok, result.detail


def test_simpy_deterministic_timestamps() -> None:
    result = probe_simpy()
    assert result.ok, result.detail


def test_sqlite_commit_rollback_and_reopen() -> None:
    result = probe_sqlite()
    assert result.ok, result.detail


def test_pydantic_validation_and_json_round_trip() -> None:
    result = probe_pydantic()
    assert result.ok, result.detail


def test_plotly_figure_serializes() -> None:
    result = probe_plotly()
    assert result.ok, result.detail


def test_temporary_streamlit_app_executes() -> None:
    result = probe_streamlit_app_test()
    assert result.ok, result.detail
