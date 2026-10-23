"""Hash-verified loading for the small original-synthetic knowledge corpus."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from shopfloor_pilot.domain.canonical import canonical_hash

from .models import (
    CorpusManifest,
    CorpusManifestEntry,
    KnowledgeChunk,
    KnowledgeCorpus,
    KnowledgeDocument,
    KnowledgeNote,
    KnowledgeNoteChunk,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KNOWLEDGE_MANIFEST = REPOSITORY_ROOT / "data" / "knowledge" / "manifest.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _source_path(manifest_path: Path, source_path: str) -> Path:
    """Resolve a manifest-relative or repository-relative path within the corpus root."""

    if "\\" in source_path:
        raise ValueError(f"knowledge source_path must use POSIX separators: {source_path!r}")
    relative = PurePosixPath(source_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError(f"unsafe knowledge source_path: {source_path!r}")
    if relative.suffix != ".json":
        raise ValueError(f"knowledge source must be JSON: {source_path!r}")

    corpus_root = manifest_path.parent.resolve()
    local_candidate = (corpus_root / Path(*relative.parts)).resolve()
    candidates = [local_candidate]
    resolved_repository = REPOSITORY_ROOT.resolve()
    if _is_within(manifest_path.resolve(), resolved_repository):
        candidates.append((resolved_repository / Path(*relative.parts)).resolve())

    for candidate in candidates:
        if _is_within(candidate, corpus_root) and candidate.is_file():
            return candidate
    safe_candidates = [candidate for candidate in candidates if _is_within(candidate, corpus_root)]
    if not safe_candidates:
        raise ValueError(f"knowledge source_path escapes corpus root: {source_path!r}")
    raise ValueError(f"knowledge source is missing: {source_path!r}")


def _chunk_hash(note: KnowledgeNote, chunk: KnowledgeNoteChunk) -> str:
    """Bind citations to all note metadata that gives the excerpt its meaning."""

    return canonical_hash(
        {
            "schema_version": note.schema_version,
            "document_id": note.document_id,
            "document_version": note.version,
            "title": note.title,
            "provenance": note.provenance,
            "provenance_detail": note.provenance_detail,
            "applicable_capabilities": note.applicable_capabilities,
            "chunk": chunk,
        },
        exclude_wall_clock=False,
    )


def _resolved_chunk(
    note: KnowledgeNote,
    entry: CorpusManifestEntry,
    chunk: KnowledgeNoteChunk,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        document_id=note.document_id,
        document_version=note.version,
        source_path=entry.source_path,
        source_sha256=entry.source_sha256,
        chunk_id=chunk.chunk_id,
        chunk_hash=_chunk_hash(note, chunk),
        title=note.title,
        provenance=note.provenance,
        provenance_detail=note.provenance_detail,
        applicable_capabilities=note.applicable_capabilities,
        evidence_excerpt=chunk.evidence_excerpt,
        observable_symptoms=chunk.observable_symptoms,
        observable_signal_fields=chunk.observable_signal_fields,
        possible_explanation=chunk.possible_explanation,
        suggested_checks=chunk.suggested_checks,
        additional_signal_fields=chunk.additional_signal_fields,
        limitations=chunk.limitations,
    )


def _load_document(
    manifest_path: Path,
    entry: CorpusManifestEntry,
) -> KnowledgeDocument:
    path = _source_path(manifest_path, entry.source_path)
    raw = path.read_bytes()
    actual_hash = _sha256_bytes(raw)
    if actual_hash != entry.source_sha256:
        raise ValueError(f"knowledge source hash mismatch for {entry.source_path!r}: {actual_hash}")
    note = KnowledgeNote.model_validate_json(raw)
    if note.document_id != entry.document_id or note.version != entry.version:
        raise ValueError(
            f"manifest identity/version disagrees with knowledge source {entry.source_path!r}"
        )
    source_chunk_ids = tuple(item.chunk_id for item in note.chunks)
    if source_chunk_ids != entry.chunk_ids:
        raise ValueError(f"manifest chunk_ids disagree with knowledge source {entry.source_path!r}")
    chunks = tuple(
        sorted(
            (_resolved_chunk(note, entry, chunk) for chunk in note.chunks),
            key=lambda item: item.chunk_id,
        )
    )
    return KnowledgeDocument(
        document_id=note.document_id,
        version=note.version,
        title=note.title,
        provenance=note.provenance,
        provenance_detail=note.provenance_detail,
        applicable_capabilities=note.applicable_capabilities,
        source_path=entry.source_path,
        source_sha256=entry.source_sha256,
        chunks=chunks,
    )


def _verify_note_coverage(manifest_path: Path, documents: tuple[KnowledgeDocument, ...]) -> None:
    """Reject every unlisted JSON source beneath the dedicated corpus root."""

    corpus_root = manifest_path.parent.resolve()
    actual = {
        path.resolve()
        for path in corpus_root.rglob("*.json")
        if path.is_file() and path.resolve() != manifest_path.resolve()
    }
    expected = {
        _source_path(
            manifest_path,
            document.source_path,
        )
        for document in documents
    }
    if actual != expected:
        missing = sorted(path.as_posix() for path in expected - actual)
        unlisted = sorted(path.as_posix() for path in actual - expected)
        raise ValueError(
            f"knowledge manifest coverage mismatch: missing={missing}, unlisted={unlisted}"
        )


def load_corpus(manifest_path: Path | None = None) -> KnowledgeCorpus:
    """Load, authenticate, and normalize every source listed by the corpus manifest."""

    path = (manifest_path or DEFAULT_KNOWLEDGE_MANIFEST).resolve()
    if not path.is_file():
        raise ValueError(f"knowledge manifest is missing: {path}")
    raw_manifest = path.read_bytes()
    manifest = CorpusManifest.model_validate_json(raw_manifest)
    documents = tuple(
        sorted(
            (_load_document(path, entry) for entry in manifest.entries),
            key=lambda item: item.document_id,
        )
    )
    _verify_note_coverage(path, documents)
    chunks = tuple(
        sorted(
            (chunk for document in documents for chunk in document.chunks),
            key=lambda item: item.chunk_id,
        )
    )
    return KnowledgeCorpus(
        corpus_id=manifest.corpus_id,
        manifest_path=path,
        manifest_sha256=_sha256_bytes(raw_manifest),
        documents=documents,
        chunks=chunks,
    )
