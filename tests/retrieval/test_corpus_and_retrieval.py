"""S08-A01 through A03 lexical corpus and deterministic retrieval acceptance."""

from __future__ import annotations

import json
from pathlib import Path

from shopfloor_pilot.retrieval import (
    KnowledgeCitation,
    KnowledgeCorpus,
    KnowledgeDocument,
    LexicalRetriever,
    RetrievalHit,
    RetrievalIssueCode,
    RetrievalRequest,
    RetrievalStatus,
    evaluate_retrieval_fixture,
    load_corpus,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN_QUERIES = ROOT / "tests" / "retrieval" / "fixtures" / "frozen_queries.json"


def test_a01_manifest_resolves_original_synthetic_versioned_notes_and_chunks() -> None:
    corpus = load_corpus()

    assert len(corpus.documents) == 11
    assert len(corpus.chunks) == 11
    assert all(document.provenance == "original_synthetic" for document in corpus.documents)
    assert all(document.version.startswith("v") for document in corpus.documents)
    assert all(document.applicable_capabilities for document in corpus.documents)
    assert len({document.document_id for document in corpus.documents}) == 11
    assert len({chunk.chunk_id for chunk in corpus.chunks}) == 11

    documents = {document.document_id: document for document in corpus.documents}
    for chunk in corpus.chunks:
        document = documents[chunk.document_id]
        assert chunk in document.chunks
        assert chunk.observable_symptoms
        assert chunk.suggested_checks
        assert chunk.limitations
        assert not Path(chunk.source_path).is_absolute()
        source = ROOT / chunk.source_path
        payload = json.loads(source.read_text(encoding="utf-8"))
        source_chunk = next(
            item for item in payload["chunks"] if item["chunk_id"] == chunk.chunk_id
        )
        assert source_chunk["evidence_excerpt"] == chunk.evidence_excerpt


def test_a02_frozen_queries_exceed_required_hit_rate_and_all_abstain() -> None:
    report = evaluate_retrieval_fixture(LexicalRetriever.from_default_corpus(), FROZEN_QUERIES)

    assert len(report.queries) == 12
    assert report.summary.answerable_queries == 9
    assert report.summary.answerable_hits_in_top_k == 9
    assert report.summary.answerable_hits_in_top_k >= 8
    assert report.summary.no_evidence_queries == 3
    assert report.summary.no_evidence_abstentions == 3
    assert report.summary.answerable_acceptance_passed
    assert report.summary.no_evidence_acceptance_passed
    assert all(len(item.ranking) <= report.scoring.top_k for item in report.queries)
    assert str(report.fixture_path) == "tests/retrieval/fixtures/frozen_queries.json"


def _tie_retriever(source: LexicalRetriever) -> LexicalRetriever:
    base = source.corpus.chunks[0]
    chunks = tuple(
        base.model_copy(
            update={
                "document_id": f"note-tie-{suffix}",
                "document_version": "v1",
                "source_path": f"data/knowledge/notes/tie_{suffix}.json",
                "source_sha256": suffix * 64,
                "chunk_id": f"chunk-tie-{suffix}",
                "chunk_hash": suffix * 64,
                "title": "Identical deterministic tie text",
            }
        )
        for suffix in ("a", "b")
    )
    documents = tuple(
        KnowledgeDocument(
            document_id=chunk.document_id,
            version=chunk.document_version,
            title=chunk.title,
            provenance=chunk.provenance,
            provenance_detail=chunk.provenance_detail,
            applicable_capabilities=chunk.applicable_capabilities,
            source_path=chunk.source_path,
            source_sha256=chunk.source_sha256,
            chunks=(chunk,),
        )
        for chunk in chunks
    )
    corpus = KnowledgeCorpus(
        corpus_id="tie-corpus",
        manifest_path=Path("data/knowledge/tie-manifest.json"),
        manifest_sha256="c" * 64,
        documents=documents,
        chunks=chunks,
    )
    return LexicalRetriever(corpus)


def test_a03_repeated_equal_score_and_invalid_queries_are_typed_and_deterministic(
    retriever: LexicalRetriever,
) -> None:
    request = {
        "query": "low lubrication pressure reservoir filter pump gauge leaks",
        "top_k": 3,
    }
    assert retriever.search(request) == retriever.search(request)

    tied = _tie_retriever(retriever).search(
        {
            "query": (
                "conflicting synthetic readings same machine signal sensor values "
                "disagree timestamp"
            ),
            "top_k": 2,
        }
    )
    assert tied.status is RetrievalStatus.MATCHES
    assert [item.chunk_id for item in tied.hits] == ["chunk-tie-a", "chunk-tie-b"]
    assert tied.hits[0].score == tied.hits[1].score

    empty = retriever.search({"query": "   ", "top_k": 3})
    assert empty.status is RetrievalStatus.NO_EVIDENCE
    assert empty.hits == ()
    assert empty.issues[0].code is RetrievalIssueCode.EMPTY_QUERY

    invalid_cases = (
        {"query": "x" * 501, "top_k": 3},
        {"query": "word " * 65, "top_k": 3},
        {"query": "temperature vibration", "top_k": True},
        {"query": "temperature vibration", "capability_filters": ["unknown"]},
        {"query": "temperature vibration", "unexpected": "field"},
        {"query": 17},
    )
    for invalid_request in invalid_cases:
        result = retriever.search(invalid_request)
        assert result.status is RetrievalStatus.INVALID_REQUEST
        assert result.hits == ()
        assert result.issues
        assert len(result.query) <= 160

    valid_typed = RetrievalRequest(query="temperature vibration", top_k=3)
    forged_typed_requests = (
        valid_typed.model_copy(update={"query": 17}),
        valid_typed.model_copy(update={"top_k": 0}),
        valid_typed.model_copy(update={"top_k": True}),
        valid_typed.model_copy(update={"capability_filters": ("unknown",)}),
    )
    for forged_request in forged_typed_requests:
        result = retriever.search(forged_request)
        assert result.status is RetrievalStatus.INVALID_REQUEST
        assert result.hits == ()
        assert result.issues


def test_a04_guidance_citations_require_exact_current_identity_and_excerpt(
    retriever: LexicalRetriever,
) -> None:
    result = retriever.search(
        {"query": "low lubrication pressure reservoir pump gauge", "top_k": 1}
    )
    assert result.status is RetrievalStatus.MATCHES
    citation = KnowledgeCitation.from_hit(result.hits[0])
    assert retriever.resolve_citation(citation)
    assert not retriever.resolve_citation(citation.model_copy(update={"chunk_hash": "f" * 64}))
    assert not retriever.resolve_citation(
        citation.model_copy(update={"evidence_excerpt": "stale excerpt"})
    )
    assert not retriever.resolve_citation(
        citation.model_copy(update={"chunk_id": "chunk-invented"})
    )
    assert not retriever.resolve_citation(KnowledgeCitation.model_construct())
    assert not retriever.resolve_citation(RetrievalHit.model_construct())
    assert not retriever.resolve_citation(citation.model_copy(update={"chunk_id": 17}))
