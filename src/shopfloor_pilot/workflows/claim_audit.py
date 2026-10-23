"""Deterministic semantic audit of supervisor claims against the pinned snapshot.

S10 claim checks verify that cited evidence IDs exist and have allowed kinds; they
cannot see whether claim *text* matches reality. This audit compares the simple
factual statements a claim makes (machine status words, order count, simulated
time, state version) with the snapshot the proposal was based on.

Verdicts per finding:

* ``consistent`` – the statement matches the snapshot;
* ``contradiction`` – the statement is clearly false (e.g. a running machine called
  "down", a down machine called "running" or "idle", a wrong order count);
* ``ambiguous`` – wording that is defensible but imprecise, such as "unavailable" for
  a machine that is busy running work.

Statements the audit cannot parse are not scored. It is an evaluation and review
aid, not an authority: claims never change operational state either way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from shopfloor_pilot.domain.models import MachineStatus, OperationalSnapshot

_DOWN_WORDS = (
    "down",
    "stopped",
    "faulted",
    "failed",
    "offline",
    "broken",
    "out of service",
    "inoperable",
    "under maintenance",
)
_UNAVAILABLE_WORDS = ("unavailable", "not available")
_RUNNING_WORDS = ("running", "busy", "processing", "operating", "occupied", "in use")
_IDLE_WORDS = ("idle", "available", "free")

_NEGATION = re.compile(r"\b(?:not|no longer|never|isn't|aren't|wasn't|weren't)\s+$")
_ORDER_COUNT = re.compile(r"\b(\d{1,4})\s+orders?\b", re.IGNORECASE)
_SIM_TIME = re.compile(r"\bt\s*=\s*(\d{1,7})\s*s\b", re.IGNORECASE)
_VERSION = re.compile(r"(?:\bstate\s+version\s+|\bstate\s+v|(?<![\w-])v)(\d{1,7})\b", re.IGNORECASE)


def _clauses(text: str) -> list[str]:
    # Split on ";" and on sentence ends, but not on decimal points such as "48.3".
    return [item.strip() for item in re.split(r";|(?<!\d)\.(?!\d)|\.\s", text) if item.strip()]


def _word_hits(clause: str, words: Iterable[str]) -> list[int]:
    lowered = clause.lower()
    hits = []
    for word in words:
        for match in re.finditer(rf"\b{re.escape(word)}\b", lowered):
            if not _NEGATION.search(lowered[: match.start()]):
                hits.append(match.start())
    return hits


def _status_category(clause: str) -> str | None:
    lowered = clause.lower()
    categories = set()
    if _word_hits(lowered, _UNAVAILABLE_WORDS):
        categories.add("unavailable")
    rest = re.sub(r"\bunavailable\b|\bnot available\b", " ", lowered)
    for category, words in (
        ("down", _DOWN_WORDS),
        ("running", _RUNNING_WORDS),
        ("idle", _IDLE_WORDS),
    ):
        if _word_hits(rest, words):
            categories.add(category)
    # Several status categories in one clause cannot be attributed reliably.
    return categories.pop() if len(categories) == 1 else None


def _machine_verdict(claimed: str, actual: MachineStatus) -> str:
    if claimed == "down":
        return (
            "consistent"
            if actual in (MachineStatus.DOWN, MachineStatus.MAINTENANCE)
            else "contradiction"
        )
    if claimed == "running":
        return "consistent" if actual is MachineStatus.RUNNING else "contradiction"
    if claimed == "idle":
        return "consistent" if actual is MachineStatus.AVAILABLE else "contradiction"
    # "unavailable": exact for down/maintenance, defensible-but-imprecise for busy.
    if actual in (MachineStatus.DOWN, MachineStatus.MAINTENANCE):
        return "consistent"
    if actual is MachineStatus.RUNNING:
        return "ambiguous"
    return "contradiction"


def audit_claim_text(text: str, snapshot: OperationalSnapshot) -> list[dict[str, Any]]:
    machines = {item.machine_id: item.status for item in snapshot.machines}
    findings: list[dict[str, Any]] = []
    for clause in _clauses(text):
        mentioned = [machine for machine in machines if re.search(rf"\b{machine}\b", clause)]
        if mentioned:
            category = _status_category(clause)
            if category is not None:
                for machine in mentioned:
                    findings.append(
                        {
                            "type": "machine_status",
                            "machine_id": machine,
                            "claimed": category,
                            "actual": machines[machine].value,
                            "verdict": _machine_verdict(category, machines[machine]),
                            "clause": clause[:160],
                        }
                    )
        for match in _ORDER_COUNT.finditer(clause):
            claimed = int(match.group(1))
            findings.append(
                {
                    "type": "order_count",
                    "claimed": claimed,
                    "actual": len(snapshot.orders),
                    "verdict": "consistent" if claimed == len(snapshot.orders) else "contradiction",
                    "clause": clause[:160],
                }
            )
        for match in _SIM_TIME.finditer(clause):
            claimed = int(match.group(1))
            findings.append(
                {
                    "type": "simulated_time",
                    "claimed": claimed,
                    "actual": snapshot.simulated_time_seconds,
                    # Earlier readings ("stable until t=2520s") are legitimate history;
                    # only a time after the pinned snapshot is impossible to have observed.
                    "verdict": "consistent"
                    if claimed <= snapshot.simulated_time_seconds
                    else "contradiction",
                    "clause": clause[:160],
                }
            )
        for match in _VERSION.finditer(clause):
            claimed = int(match.group(1))
            findings.append(
                {
                    "type": "state_version",
                    "claimed": claimed,
                    "actual": snapshot.state_version,
                    "verdict": "consistent"
                    if claimed == snapshot.state_version
                    else "contradiction",
                    "clause": clause[:160],
                }
            )
    return findings


def audit_claims(
    claims: Iterable[Mapping[str, Any]], snapshot: OperationalSnapshot
) -> dict[str, Any]:
    """Audit every claim; return per-claim findings and verdict counts."""

    results = []
    counts = {"consistent": 0, "contradiction": 0, "ambiguous": 0}
    for claim in claims:
        findings = audit_claim_text(str(claim.get("text", "")), snapshot)
        for item in findings:
            counts[item["verdict"]] += 1
        results.append({"kind": claim.get("kind"), "text": claim.get("text"), "findings": findings})
    return {
        "snapshot_state_version": snapshot.state_version,
        "snapshot_simulated_time_seconds": snapshot.simulated_time_seconds,
        "claims": results,
        "counts": counts,
        "has_contradiction": counts["contradiction"] > 0,
    }


__all__ = ["audit_claim_text", "audit_claims"]
