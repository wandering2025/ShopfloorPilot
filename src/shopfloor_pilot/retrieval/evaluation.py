"""Reproduce the frozen lexical-retrieval acceptance measurement."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .corpus import REPOSITORY_ROOT
from .lexical import LexicalRetriever
from .models import (
    RetrievalEvaluationQueryResult,
    RetrievalEvaluationReport,
    RetrievalEvaluationSummary,
    RetrievalScoringDefinition,
    RetrievalStatus,
)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value  # type: ignore[return-value]


def _integer(value: object, label: str, *, minimum: int = 0, maximum: int = 10) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _identifier_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a non-empty JSON string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} values must be unique")
    return result


def evaluate_retrieval_fixture(
    retriever: LexicalRetriever,
    fixture_path: Path,
) -> RetrievalEvaluationReport:
    """Run every frozen query and retain its complete bounded ranking."""

    path = fixture_path.resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read retrieval fixture {path}: {exc}") from exc
    root = _mapping(payload, "retrieval fixture")
    if root.get("schema_version") != 1:
        raise ValueError("retrieval fixture schema_version must equal 1")
    fixture_id = root.get("fixture_id")
    if not isinstance(fixture_id, str):
        raise ValueError("retrieval fixture_id must be a string")
    if root.get("frozen_before_scoring") is not True:
        raise ValueError("retrieval fixture must be frozen before scoring")

    scoring_payload = _mapping(root.get("scoring"), "retrieval scoring definition")
    scoring = RetrievalScoringDefinition.model_validate(scoring_payload)
    raw_queries = root.get("queries")
    if not isinstance(raw_queries, list):
        raise ValueError("retrieval fixture queries must be a JSON array")

    query_results: list[RetrievalEvaluationQueryResult] = []
    seen_query_ids: set[str] = set()
    answerable_count = 0
    answerable_hits = 0
    no_evidence_count = 0
    no_evidence_abstentions = 0
    for index, raw_case in enumerate(raw_queries):
        case = _mapping(raw_case, f"retrieval query {index}")
        query_id = case.get("query_id")
        query = case.get("query")
        if not isinstance(query_id, str) or query_id in seen_query_ids:
            raise ValueError("retrieval query_id values must be unique strings")
        seen_query_ids.add(query_id)
        if not isinstance(query, str):
            raise ValueError(f"retrieval query {query_id!r} must contain a string query")
        top_k = _integer(case.get("top_k"), f"retrieval query {query_id!r} top_k", minimum=1)
        if top_k != scoring.top_k:
            raise ValueError(f"retrieval query {query_id!r} top_k disagrees with scoring")

        expect_no_evidence = case.get("expect_no_evidence", False)
        if not isinstance(expect_no_evidence, bool):
            raise ValueError(f"retrieval query {query_id!r} expect_no_evidence must be boolean")
        raw_relevant = case.get("relevant_chunk_ids")
        if expect_no_evidence:
            if raw_relevant is not None:
                raise ValueError(f"no-evidence query {query_id!r} cannot carry relevant_chunk_ids")
            relevant_chunk_ids: tuple[str, ...] = ()
        else:
            relevant_chunk_ids = _identifier_list(
                raw_relevant,
                f"retrieval query {query_id!r} relevant_chunk_ids",
            )
            missing = [
                chunk_id for chunk_id in relevant_chunk_ids if retriever.get_chunk(chunk_id) is None
            ]
            if missing:
                raise ValueError(f"retrieval query {query_id!r} labels unknown chunks: {missing}")

        result = retriever.search({"query": query, "top_k": top_k})
        if expect_no_evidence:
            no_evidence_count += 1
            abstained = result.status is RetrievalStatus.NO_EVIDENCE and not result.hits
            no_evidence_abstentions += int(abstained)
            answerable_hit: bool | None = None
            no_evidence_abstained: bool | None = abstained
        else:
            answerable_count += 1
            ranked_ids = {item.chunk_id for item in result.hits}
            answerable_hit = bool(ranked_ids.intersection(relevant_chunk_ids))
            answerable_hits += int(answerable_hit)
            no_evidence_abstained = None
        query_results.append(
            RetrievalEvaluationQueryResult(
                query_id=query_id,
                query=query,
                relevant_chunk_ids=relevant_chunk_ids,
                expect_no_evidence=expect_no_evidence,
                status=result.status,
                ranking=result.hits,
                answerable_hit_in_top_k=answerable_hit,
                no_evidence_abstained=no_evidence_abstained,
            )
        )

    if answerable_count != scoring.answerable_query_count:
        raise ValueError("measured answerable denominator disagrees with frozen scoring")
    if no_evidence_count != scoring.no_evidence_query_count:
        raise ValueError("measured no-evidence denominator disagrees with frozen scoring")
    if scoring.minimum_answerable_top3_hits > answerable_count:
        raise ValueError("minimum answerable hits exceeds the answerable denominator")
    if scoring.required_no_evidence_abstentions > no_evidence_count:
        raise ValueError("required abstentions exceeds the no-evidence denominator")

    summary = RetrievalEvaluationSummary(
        total_queries=len(query_results),
        answerable_queries=answerable_count,
        answerable_hits_in_top_k=answerable_hits,
        no_evidence_queries=no_evidence_count,
        no_evidence_abstentions=no_evidence_abstentions,
        answerable_acceptance_passed=(answerable_hits >= scoring.minimum_answerable_top3_hits),
        no_evidence_acceptance_passed=(
            no_evidence_abstentions >= scoring.required_no_evidence_abstentions
        ),
    )
    try:
        reported_path = path.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        reported_path = path
    return RetrievalEvaluationReport(
        fixture_id=fixture_id,
        fixture_path=reported_path,
        corpus_id=retriever.corpus.corpus_id,
        config=retriever.config,
        scoring=scoring,
        queries=tuple(query_results),
        summary=summary,
    )


# Short alias for callers that already operate inside the retrieval namespace.
evaluate_fixture = evaluate_retrieval_fixture
