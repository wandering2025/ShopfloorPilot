#!/usr/bin/env python3
"""Report interpreter, dependency and readiness facts for a local checkout.

Run with: uv run python scripts/doctor.py
This command must not contact an inference service or print environment values.
"""

from __future__ import annotations

import os

from shopfloor_pilot import __version__
from shopfloor_pilot.environment import (
    interpreter_report,
    library_versions,
    probe_cp_sat,
    probe_imports,
    probe_package_origin,
    probe_plotly,
    probe_pydantic,
    probe_simpy,
    probe_sqlite,
)


def _print_result(label: str, ok: bool, detail: str) -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {label}: {detail}")


def main() -> int:
    failures: list[str] = []
    print("ShopfloorPilot environment doctor")
    print("================================")

    info = interpreter_report()
    print("\nInterpreter")
    print(f"  executable: {info['executable']}")
    print(f"  version: {info['version']}")
    print(f"  prefix: {info['prefix']}")
    print(f"  base_prefix: {info['base_prefix']}")
    print(f"  pyvenv.cfg: {info['pyvenv_cfg']}")

    version_ok = info["version"].startswith("3.12.")
    _print_result("Python 3.12", version_ok, info["version"])
    if not version_ok:
        failures.append("Python 3.12")

    venv_ok = bool(info["virtualenv"]) and bool(info["system_site_packages_disabled"])
    _print_result(
        "isolated virtual environment",
        venv_ok,
        f"virtualenv={info['virtualenv']} "
        f"system_site_packages_disabled={info['system_site_packages_disabled']}",
    )
    if not venv_ok:
        failures.append("isolated virtual environment")

    origin = probe_package_origin()
    _print_result(origin.name, origin.ok, origin.detail)
    if not origin.ok:
        failures.append(origin.name)

    print("\nRequired imports")
    import_results = probe_imports()
    for result in import_results:
        _print_result(result.name, result.ok, result.detail)
        if not result.ok:
            failures.append(f"import {result.name}")

    print("\nInstalled versions")
    try:
        versions = library_versions()
    except Exception as exc:  # noqa: BLE001 - report any import failure
        print(f"[FAIL] version collection: {type(exc).__name__}")
        failures.append("version collection")
        versions = {}
    for name, version in versions.items():
        print(f"  {name}: {version}")
    print(f"  shopfloor-pilot: {__version__}")

    print("\nReadiness probes")
    probes = [
        probe_cp_sat(),
        probe_simpy(),
        probe_sqlite(),
        probe_pydantic(),
        probe_plotly(),
    ]
    for probe in probes:
        _print_result(probe.name, probe.ok, probe.detail)
        if not probe.ok:
            failures.append(probe.name)

    print("\nOptional credentials (values are never printed)")
    key_present = bool(os.environ.get("DEEPSEEK_API_KEY"))
    model_present = bool(os.environ.get("DEEPSEEK_MODEL"))
    base_present = bool(os.environ.get("DEEPSEEK_BASE_URL"))
    print(f"  DEEPSEEK_API_KEY: {'configured' if key_present else 'not set (optional)'}")
    print(f"  DEEPSEEK_MODEL: {'configured' if model_present else 'not set (optional)'}")
    print(f"  DEEPSEEK_BASE_URL: {'configured' if base_present else 'not set (optional)'}")
    print("  Note: a plain `uv run` does not load `.env`; application code must load it.")

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} required check(s) failed)")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
