"""Small deterministic BM25 retrieval over verified synthetic guidance."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping

from pydantic import ValidationError

from shopfloor_pilot.domain.models import Capability

from .corpus import load_corpus
from .models import (
    KnowledgeChunk,
    KnowledgeCitation,
    KnowledgeCorpus,
    RetrievalConfig,
    RetrievalHit,
    RetrievalIssue,
    RetrievalIssueCode,
    RetrievalRequest,
    RetrievalResult,
    RetrievalStatus,
)

_TOKEN_PATTERN = re.compile(r"[^\W_]+(?:_[^\W_]+)*", flags=re.UNICODE)
_INVALID_QUERY_PREVIEW = 160
_REQUEST_FIELDS = frozenset({"query", "top_k", "capability_filters"})
_CITATION_FIELDS = frozenset(KnowledgeCitation.model_fields)
_HIT_FIELDS = frozenset(RetrievalHit.model_fields)
_DEFAULT_CONFIG = RetrievalConfig()


def tokenize(text: str) -> tuple[str, ...]:
    """Apply the documented NFKC/casefold identifier tokenizer.

    Underscore-delimited telemetry identifiers retain their full token and also emit
    their components, so `temperature_millicelsius` remains exact-searchable while a
    plain-language `temperature` query can still match it.
    """

    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(normalized):
        token = match.group(0)
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if part and part != token)
    return tuple(tokens)


def _searchable_text(chunk: KnowledgeChunk) -> str:
    fields = [
        chunk.title,
        *(item.value for item in chunk.applicable_capabilities),
        chunk.evidence_excerpt,
        *chunk.observable_symptoms,
        *chunk.observable_signal_fields,
        *(value for value in (chunk.possible_explanation,) if value is not None),
        *chunk.suggested_checks,
        *chunk.additional_signal_fields,
        *chunk.limitations,
    ]
    return "\n".join(fields)


def _issue(code: RetrievalIssueCode, field: str, message: str) -> RetrievalIssue:
    return RetrievalIssue(code=code, field=field, message=message)


def _query_preview(raw_query: object) -> str:
    return raw_query[:_INVALID_QUERY_PREVIEW] if isinstance(raw_query, str) else ""


def _invalid(
    config: RetrievalConfig,
    raw_query: object,
    issue: RetrievalIssue,
) -> RetrievalResult:
    return RetrievalResult(
        status=RetrievalStatus.INVALID_REQUEST,
        query=_query_preview(raw_query),
        issues=(issue,),
        config=config,
    )


def _strict_model_json(value: object, expected_fields: frozenset[str]) -> str:
    """Serialize raw model state without trusting its prior Pydantic validation.

    ``model_copy(update=...)`` and ``model_construct(...)`` can produce instances whose
    runtime values have never passed the model validators.  Reading ``__dict__`` and using
    the standard JSON encoder preserves invalid primitives such as ``True`` instead of
    letting Pydantic's schema serializer coerce them (for example, into integer ``1``).
    """

    raw = vars(value)
    if set(raw) != expected_fields:
        raise ValueError("model fields do not match the public schema")
    return json.dumps(
        raw,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _mapping_request(
    value: Mapping[str, object],
    config: RetrievalConfig,
) -> tuple[RetrievalRequest | None, RetrievalResult | None]:
    unknown = set(value) - _REQUEST_FIELDS
    raw_query = value.get("query")
    if unknown:
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.INVALID_REQUEST,
                "request",
                "request contains fields outside the allowlisted schema",
            ),
        )
    if not isinstance(raw_query, str):
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.INVALID_REQUEST,
                "query",
                "query must be a string",
            ),
        )
    if len(raw_query) > config.max_query_characters:
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.QUERY_TOO_LONG,
                "query",
                f"query exceeds {config.max_query_characters} characters",
            ),
        )

    top_k = value.get("top_k", config.default_top_k)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= config.max_top_k:
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.INVALID_TOP_K,
                "top_k",
                f"top_k must be an integer from 1 through {config.max_top_k}",
            ),
        )

    raw_filters = value.get("capability_filters", ())
    if not isinstance(raw_filters, (list, tuple)) or len(raw_filters) > 16:
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.MALFORMED_FILTER,
                "capability_filters",
                "capability_filters must be a bounded list or tuple of known capabilities",
            ),
        )
    filters: list[Capability] = []
    try:
        for item in raw_filters:
            if isinstance(item, Capability):
                filters.append(item)
            elif isinstance(item, str):
                filters.append(Capability(item))
            else:
                raise ValueError
        request = RetrievalRequest(
            query=raw_query,
            top_k=top_k,
            capability_filters=tuple(filters),
        )
    except (ValueError, ValidationError):
        return None, _invalid(
            config,
            raw_query,
            _issue(
                RetrievalIssueCode.MALFORMED_FILTER,
                "capability_filters",
                "capability_filters contain an unknown or duplicate capability",
            ),
        )
    return request, None


def _coerce_request(
    value: RetrievalRequest | Mapping[str, object],
    config: RetrievalConfig,
) -> tuple[RetrievalRequest | None, RetrievalResult | None]:
    if isinstance(value, RetrievalRequest):
        try:
            parsed = RetrievalRequest.model_validate_json(
                _strict_model_json(value, _REQUEST_FIELDS)
            )
        except Exception:
            return None, _invalid(
                config,
                "",
                _issue(
                    RetrievalIssueCode.INVALID_REQUEST,
                    "request",
                    "typed request did not match the strict bounded schema",
                ),
            )
        return _mapping_request(
            {
                "query": parsed.query,
                "top_k": parsed.top_k,
                "capability_filters": parsed.capability_filters,
            },
            config,
        )
    if isinstance(value, Mapping):
        return _mapping_request(value, config)
    return None, _invalid(
        config,
        "",
        _issue(
            RetrievalIssueCode.INVALID_REQUEST,
            "request",
            "request must be RetrievalRequest or a mapping",
        ),
    )


def _citation(value: KnowledgeCitation | RetrievalHit | Mapping[str, object]) -> KnowledgeCitation:
    if isinstance(value, KnowledgeCitation):
        return KnowledgeCitation.model_validate_json(_strict_model_json(value, _CITATION_FIELDS))
    if isinstance(value, RetrievalHit):
        hit = RetrievalHit.model_validate_json(_strict_model_json(value, _HIT_FIELDS))
        return KnowledgeCitation.from_hit(hit)
    return KnowledgeCitation.model_validate(value)


def resolve_citation(
    corpus: KnowledgeCorpus,
    citation: KnowledgeCitation | RetrievalHit | Mapping[str, object],
) -> bool:
    """Return whether a citation exactly resolves against this corpus version."""

    try:
        expected = _citation(citation)
    except Exception:
        return False
    chunk = next((item for item in corpus.chunks if item.chunk_id == expected.chunk_id), None)
    if chunk is None:
        return False
    return (
        expected.document_id == chunk.document_id
        and expected.document_version == chunk.document_version
        and expected.chunk_hash == chunk.chunk_hash
        and expected.source_path == chunk.source_path
        and expected.source_sha256 == chunk.source_sha256
        and expected.evidence_excerpt == chunk.evidence_excerpt
    )


class LexicalRetriever:
    """Immutable in-memory BM25 index with bounded typed query handling."""

    def __init__(
        self,
        corpus: KnowledgeCorpus,
        config: RetrievalConfig = _DEFAULT_CONFIG,
    ) -> None:
        self.corpus = corpus
        self.config = config
        self._chunks = {item.chunk_id: item for item in corpus.chunks}
        self._term_frequencies: dict[str, Counter[str]] = {}
        self._document_lengths: dict[str, int] = {}
        document_frequencies: Counter[str] = Counter()
        for chunk in corpus.chunks:
            frequencies = Counter(tokenize(_searchable_text(chunk)))
            self._term_frequencies[chunk.chunk_id] = frequencies
            self._document_lengths[chunk.chunk_id] = sum(frequencies.values())
            # BM25 uses the number of chunks containing a term, never the raw
            # within-chunk frequency. Counter.update(mapping) would incorrectly add
            # each term count and can make document frequency exceed corpus size.
            document_frequencies.update(frequencies.keys())
        self._document_frequencies = document_frequencies
        total_length = sum(self._document_lengths.values())
        self._average_document_length = total_length / len(corpus.chunks)

    @classmethod
    def from_default_corpus(
        cls,
        config: RetrievalConfig = _DEFAULT_CONFIG,
    ) -> LexicalRetriever:
        return cls(load_corpus(), config=config)

    def get_chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        return self._chunks.get(chunk_id)

    def resolve_citation(
        self,
        citation: KnowledgeCitation | RetrievalHit | Mapping[str, object],
    ) -> bool:
        return resolve_citation(self.corpus, citation)

    def _score(
        self, query_terms: tuple[str, ...], chunk: KnowledgeChunk
    ) -> tuple[float, tuple[str, ...]]:
        frequencies = self._term_frequencies[chunk.chunk_id]
        matched_terms = tuple(sorted(term for term in query_terms if frequencies.get(term, 0)))
        if len(matched_terms) < self.config.min_distinct_terms:
            return 0.0, matched_terms
        chunk_count = len(self.corpus.chunks)
        document_length = self._document_lengths[chunk.chunk_id]
        average_length = self._average_document_length or 1.0
        score = 0.0
        for term in matched_terms:
            frequency = frequencies[term]
            document_frequency = self._document_frequencies[term]
            inverse_document_frequency = math.log(
                1.0 + (chunk_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + self.config.k1 * (
                1.0 - self.config.b + self.config.b * document_length / average_length
            )
            score += inverse_document_frequency * (frequency * (self.config.k1 + 1.0) / denominator)
        return round(score, self.config.score_precision), matched_terms

    @staticmethod
    def _hit(chunk: KnowledgeChunk, score: float, matched_terms: tuple[str, ...]) -> RetrievalHit:
        return RetrievalHit(
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            chunk_id=chunk.chunk_id,
            chunk_hash=chunk.chunk_hash,
            source_path=chunk.source_path,
            source_sha256=chunk.source_sha256,
            score=score,
            matched_terms=matched_terms,
            evidence_excerpt=chunk.evidence_excerpt,
            title=chunk.title,
            applicable_capabilities=chunk.applicable_capabilities,
            observable_symptoms=chunk.observable_symptoms,
            observable_signal_fields=chunk.observable_signal_fields,
            possible_explanation=chunk.possible_explanation,
            suggested_checks=chunk.suggested_checks,
            additional_signal_fields=chunk.additional_signal_fields,
            limitations=chunk.limitations,
        )

    def search(
        self,
        request: RetrievalRequest | Mapping[str, object],
    ) -> RetrievalResult:
        """Search locally; every untrusted-input failure is returned as typed data."""

        parsed, invalid = _coerce_request(request, self.config)
        if invalid is not None:
            return invalid
        assert parsed is not None
        query_tokens = tokenize(parsed.query)
        if not query_tokens:
            return RetrievalResult(
                status=RetrievalStatus.NO_EVIDENCE,
                query=parsed.query,
                issues=(
                    _issue(
                        RetrievalIssueCode.EMPTY_QUERY,
                        "query",
                        "query contains no searchable terms",
                    ),
                ),
                config=self.config,
            )
        if len(query_tokens) > self.config.max_query_tokens:
            return RetrievalResult(
                status=RetrievalStatus.INVALID_REQUEST,
                query=parsed.query[:_INVALID_QUERY_PREVIEW],
                issues=(
                    _issue(
                        RetrievalIssueCode.TOO_MANY_QUERY_TOKENS,
                        "query",
                        f"query exceeds {self.config.max_query_tokens} lexical tokens",
                    ),
                ),
                config=self.config,
            )

        unique_query_terms = tuple(sorted(set(query_tokens)))
        required_capabilities = frozenset(parsed.capability_filters)
        scored: list[tuple[float, str, tuple[str, ...], KnowledgeChunk]] = []
        for chunk in self.corpus.chunks:
            chunk_capabilities = frozenset(chunk.applicable_capabilities)
            if (
                required_capabilities
                and chunk_capabilities
                and required_capabilities.isdisjoint(chunk_capabilities)
            ):
                continue
            score, matched_terms = self._score(unique_query_terms, chunk)
            if score >= self.config.min_score:
                scored.append((score, chunk.chunk_id, matched_terms, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        hits = tuple(
            self._hit(chunk, score, matched_terms)
            for score, _chunk_id, matched_terms, chunk in scored[: parsed.top_k]
        )
        if not hits:
            return RetrievalResult(
                status=RetrievalStatus.NO_EVIDENCE,
                query=parsed.query,
                config=self.config,
            )
        return RetrievalResult(
            status=RetrievalStatus.MATCHES,
            query=parsed.query,
            hits=hits,
            config=self.config,
        )
