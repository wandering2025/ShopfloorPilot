"""Score the frozen S08 lexical-retrieval fixture without network or credentials."""

from __future__ import annotations

import argparse
from pathlib import Path

from shopfloor_pilot.retrieval import (
    LexicalRetriever,
    evaluate_retrieval_fixture,
    load_corpus,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = REPOSITORY_ROOT / "tests" / "retrieval" / "fixtures" / "frozen_queries.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    retriever = LexicalRetriever(load_corpus(arguments.manifest))
    report = evaluate_retrieval_fixture(retriever, arguments.fixture)
    serialized = report.model_dump_json(indent=2) + "\n"
    if arguments.output is None:
        print(serialized, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized, encoding="utf-8")
        print(f"retrieval scoring report: {arguments.output}")

    summary = report.summary
    if not (summary.answerable_acceptance_passed and summary.no_evidence_acceptance_passed):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
