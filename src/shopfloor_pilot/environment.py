"""Reusable, side-effect-light readiness probes for the E00 environment."""

from __future__ import annotations

import importlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_IMPORTS = (
    "simpy",
    "ortools.sat.python.cp_model",
    "pydantic",
    "streamlit",
    "plotly",
    "sqlite3",
)

LIBRARY_MODULES = {
    "simpy": "simpy",
    "ortools": "ortools",
    "pydantic": "pydantic",
    "streamlit": "streamlit",
    "plotly": "plotly",
    "sqlite3": "sqlite3",
}

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PACKAGE_FILE = PACKAGE_ROOT / "src" / "shopfloor_pilot" / "__init__.py"

STREAMLIT_SMOKE_APP = """\
import streamlit as st

st.title("E00 smoke")
st.write("Environment smoke app executed.")
"""


@dataclass(frozen=True)
class ProbeResult:
    """One named readiness result with a human-readable detail."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class HeadlessSmokeResult:
    """Result of an owned Streamlit headless HTTP smoke run."""

    ok: bool
    url: str
    health_url: str
    elapsed_seconds: float
    cleanup_ok: bool
    port_released: bool
    detail: str


def interpreter_report() -> dict[str, Any]:
    """Return interpreter and virtual-environment facts without environment values."""

    pyvenv = Path(sys.prefix) / "pyvenv.cfg"
    system_site_packages: bool | None = None
    if pyvenv.is_file():
        for raw_line in pyvenv.read_text(encoding="utf-8").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and key.strip().lower() == "include-system-site-packages":
                system_site_packages = value.strip().lower() == "true"
    return {
        "executable": sys.executable,
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "virtualenv": sys.prefix != sys.base_prefix,
        "system_site_packages_disabled": system_site_packages is False,
        "pyvenv_cfg": str(pyvenv),
        "package_file": str(EXPECTED_PACKAGE_FILE),
        "package_root": str(PACKAGE_ROOT),
    }


def library_versions() -> dict[str, str]:
    """Import each required library and return its reported version."""

    versions: dict[str, str] = {}
    for name, module_name in LIBRARY_MODULES.items():
        module = importlib.import_module(module_name)
        if name == "sqlite3":
            versions[name] = sqlite3.sqlite_version
        else:
            versions[name] = str(getattr(module, "__version__", "unknown"))
    return versions


def probe_imports() -> list[ProbeResult]:
    """Import every required module independently and report failures."""

    results = []
    for module_name in REQUIRED_IMPORTS:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "imported")
        except Exception as exc:  # noqa: BLE001 - report any import failure
            results.append(ProbeResult(module_name, False, f"{type(exc).__name__}"))
        else:
            results.append(ProbeResult(module_name, True, str(version)))
    return results


def probe_package_origin() -> ProbeResult:
    """Verify that `shopfloor_pilot` resolves to this checkout's src tree."""

    try:
        import shopfloor_pilot
    except Exception as exc:  # noqa: BLE001 - report import failure
        return ProbeResult("shopfloor_pilot origin", False, f"{type(exc).__name__}")
    origin = Path(shopfloor_pilot.__file__).resolve()
    expected = EXPECTED_PACKAGE_FILE.resolve()
    ok = origin == expected
    return ProbeResult("shopfloor_pilot origin", ok, str(origin))


def probe_cp_sat(timeout_seconds: float = 10.0) -> ProbeResult:
    """Run a tiny CP-SAT model with a known optimum using one worker."""

    try:
        from ortools.sat.python import cp_model
    except Exception as exc:  # noqa: BLE001 - report import failure
        return ProbeResult("CP-SAT optimal solve", False, f"{type(exc).__name__}")

    model = cp_model.CpModel()
    x = model.NewIntVar(0, 3, "x")
    y = model.NewIntVar(0, 3, "y")
    model.Add(x + y == 3)
    model.Minimize(x)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = timeout_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)
    try:
        status_name = solver.StatusName(status)
    except Exception:  # noqa: BLE001 - older bindings may lack StatusName
        status_name = str(status)
    ok = status == cp_model.OPTIMAL and solver.Value(x) == 0 and solver.Value(y) == 3
    detail = f"status={status_name} x={solver.Value(x)} y={solver.Value(y)}"
    return ProbeResult("CP-SAT optimal solve", ok, detail)


def probe_simpy() -> ProbeResult:
    """Run a deterministic SimPy process that records logical timestamps."""

    try:
        import simpy
    except Exception as exc:  # noqa: BLE001 - report import failure
        return ProbeResult("SimPy deterministic time", False, f"{type(exc).__name__}")

    def run_once() -> list[int]:
        timestamps: list[int] = []

        def process(env: simpy.Environment):
            for target in (0, 2, 5):
                if target:
                    yield env.timeout(target - env.now)
                timestamps.append(env.now)

        env = simpy.Environment()
        env.process(process(env))
        env.run()
        return timestamps

    first = run_once()
    second = run_once()
    ok = first == [0, 2, 5] and second == first
    return ProbeResult("SimPy deterministic time", ok, f"first={first} second={second}")


def probe_sqlite() -> ProbeResult:
    """Exercise commit, rollback, and reopen using a temporary database."""

    try:
        with tempfile.TemporaryDirectory(prefix="shopfloor-pilot-sqlite-") as tmp:
            path = Path(tmp) / "probe.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
                connection.execute("INSERT INTO probe (value) VALUES (?)", ("kept",))
                connection.commit()
            with sqlite3.connect(path) as connection:
                kept = connection.execute("SELECT value FROM probe").fetchall()
                connection.execute("INSERT INTO probe (value) VALUES (?)", ("rolled-back",))
                connection.rollback()
                rolled_back = connection.execute(
                    "SELECT value FROM probe WHERE value = ?", ("rolled-back",)
                ).fetchall()
            with sqlite3.connect(path) as connection:
                reopened = connection.execute("SELECT value FROM probe").fetchall()
    except Exception as exc:  # noqa: BLE001 - report any SQLite failure
        return ProbeResult("SQLite transaction probe", False, f"{type(exc).__name__}")

    ok = kept == [("kept",)] and rolled_back == [] and reopened == [("kept",)]
    detail = f"kept={kept} rolled_back={rolled_back} reopened={reopened}"
    return ProbeResult("SQLite transaction probe", ok, detail)


def probe_pydantic() -> ProbeResult:
    """Validate, reject, and JSON round-trip a tiny Pydantic v2 model."""

    try:
        from pydantic import BaseModel, ConfigDict, Field, ValidationError
    except Exception as exc:  # noqa: BLE001 - report import failure
        return ProbeResult("Pydantic v2 validation", False, f"{type(exc).__name__}")

    class Sample(BaseModel):
        model_config = ConfigDict(extra="forbid")

        name: str
        quantity: int = Field(ge=1)

    valid = Sample(name="bracket", quantity=2)
    rejected = False
    try:
        Sample(name="bracket", quantity=0)
    except ValidationError:
        rejected = True
    round_tripped = Sample.model_validate_json(valid.model_dump_json())
    ok = rejected and round_tripped == valid
    return ProbeResult(
        "Pydantic v2 validation", ok, f"rejected={rejected} round_trip={round_tripped}"
    )


def probe_plotly() -> ProbeResult:
    """Build and JSON-serialize a tiny Plotly figure."""

    try:
        import plotly.graph_objects as go
    except Exception as exc:  # noqa: BLE001 - report import failure
        return ProbeResult("Plotly JSON serialization", False, f"{type(exc).__name__}")

    figure = go.Figure(go.Bar(x=["a", "b"], y=[1, 2]))
    try:
        payload = json.loads(figure.to_json())
    except Exception as exc:  # noqa: BLE001 - report serialization failure
        return ProbeResult("Plotly JSON serialization", False, f"{type(exc).__name__}")
    traces = payload.get("data", [])
    ok = len(traces) == 1 and traces[0].get("type") == "bar"
    return ProbeResult("Plotly JSON serialization", ok, f"traces={len(traces)}")


def probe_streamlit_app_test() -> ProbeResult:
    """Execute a temporary Streamlit script through Streamlit's AppTest API."""

    try:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(STREAMLIT_SMOKE_APP, default_timeout=30)
        app.run()
        exceptions = list(app.exception)
        title = app.title[0].value if len(app.title) else None
    except Exception as exc:  # noqa: BLE001 - report any test-harness failure
        return ProbeResult("Streamlit AppTest execution", False, f"{type(exc).__name__}")
    ok = not exceptions and title == "E00 smoke"
    detail = f"exceptions={len(exceptions)} title={title!r}"
    return ProbeResult("Streamlit AppTest execution", ok, detail)


def _health_request(url: str, timeout_seconds: float = 1.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return response.status, response.read(128).decode("utf-8", "replace").strip()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_owned_process(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def run_streamlit_headless(timeout_seconds: float = 30.0) -> HeadlessSmokeResult:
    """Start an owned temporary Streamlit app, wait for health, then stop it."""

    app_test = probe_streamlit_app_test()
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    health_url = f"http://127.0.0.1:{port}/_stcore/health"
    elapsed = 0.0
    cleanup_ok = False
    port_released = False
    detail = ""

    with tempfile.TemporaryDirectory(prefix="shopfloor-pilot-streamlit-") as tmp:
        app_path = Path(tmp) / "smoke_app.py"
        log_path = Path(tmp) / "streamlit.log"
        app_path.write_text(STREAMLIT_SMOKE_APP, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "HOME": tmp,
                "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
                "STREAMLIT_SERVER_HEADLESS": "true",
                "STREAMLIT_SERVER_FILE_WATCHER_TYPE": "none",
            }
        )
        command = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.headless=true",
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.fileWatcherType=none",
            "--browser.gatherUsageStats=false",
        ]
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        with log_path.open("wb") as log_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=tmp,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                deadline = time.monotonic() + timeout_seconds
                health_ok = False
                health_body = ""
                root_status: int | None = None
                while time.monotonic() < deadline:
                    status, body = _health_request(health_url)
                    if status == 200 and body.lower() == "ok":
                        health_ok = True
                        health_body = body
                        root_status, _ = _health_request(url)
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                elapsed = time.monotonic() - started
                if not health_ok:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
                    detail = (
                        f"health_body={health_body!r} root_status={root_status} log_tail={tail!r}"
                    )
                else:
                    detail = f"health_body={health_body!r} root_status={root_status}"
            finally:
                if process is not None:
                    cleanup_ok = _terminate_owned_process(process)
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if _port_is_free(port):
                        port_released = True
                        break
                    time.sleep(0.1)

    health_ok = "health_body='ok'" in detail
    root_ok = "root_status=200" in detail
    ok = health_ok and root_ok and cleanup_ok and port_released and app_test.ok
    if not app_test.ok and not detail:
        detail = app_test.detail
    return HeadlessSmokeResult(
        ok=ok,
        url=url,
        health_url=health_url,
        elapsed_seconds=round(elapsed, 3),
        cleanup_ok=cleanup_ok,
        port_released=port_released,
        detail=detail,
    )
