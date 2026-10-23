"""Workflow-scoped evidence identities that claims and proposals may cite."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shopfloor_pilot.domain.canonical import canonical_hash

MAX_SUMMARY_CHARACTERS = 200


class EvidenceKind(StrEnum):
    INTERPRETATION = "interpretation"
    ROUTE = "route"
    SNAPSHOT = "snapshot"
    EVENT_LOG = "event_log"
    OBSERVATION = "observation"
    GUIDANCE = "guidance"
    HYPOTHESIS = "hypothesis"
    DIAGNOSIS = "diagnosis"
    SOLVER = "solver"
    VALIDATION = "validation"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One resolvable piece of observable or deterministic evidence."""

    evidence_id: str
    kind: EvidenceKind
    source_tool: str
    summary: str
    refs: Mapping[str, Any] = field(default_factory=dict)
    untrusted_text: bool = False

    @property
    def content_sha256(self) -> str:
        return canonical_hash(
            {
                "evidence_id": self.evidence_id,
                "kind": self.kind.value,
                "summary": self.summary,
                "refs": dict(self.refs),
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "source_tool": self.source_tool,
            "summary": self.summary,
            "refs": dict(self.refs),
            "untrusted_text": self.untrusted_text,
            "content_sha256": self.content_sha256,
        }


class EvidenceRegistry:
    """Append-only registry; an ID is only citable after a tool produced it."""

    def __init__(self) -> None:
        self._items: dict[str, EvidenceItem] = {}

    def add(
        self,
        evidence_id: str,
        kind: EvidenceKind,
        *,
        source_tool: str,
        summary: str,
        refs: Mapping[str, Any] | None = None,
        untrusted_text: bool = False,
    ) -> bool:
        """Register evidence; return whether the registry grew."""

        if evidence_id in self._items:
            return False
        text = " ".join(summary.split())
        if len(text) > MAX_SUMMARY_CHARACTERS:
            text = text[: MAX_SUMMARY_CHARACTERS - 3] + "..."
        self._items[evidence_id] = EvidenceItem(
            evidence_id=evidence_id,
            kind=kind,
            source_tool=source_tool,
            summary=text,
            refs=dict(refs or {}),
            untrusted_text=untrusted_text,
        )
        return True

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self._items.get(evidence_id)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._items.values())

    def of_kind(self, *kinds: EvidenceKind) -> tuple[EvidenceItem, ...]:
        wanted = set(kinds)
        return tuple(item for item in self._items.values() if item.kind in wanted)

    def resolve(self, evidence_ids: Iterable[str]) -> tuple[list[EvidenceItem], list[str]]:
        found: list[EvidenceItem] = []
        missing: list[str] = []
        for evidence_id in evidence_ids:
            item = self._items.get(evidence_id)
            if item is None:
                missing.append(evidence_id)
            else:
                found.append(item)
        return found, missing
