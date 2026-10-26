"""Synthetic ranking truth must not claim unverified star posts are positives."""

import runpy
from pathlib import Path

from researchlib.eligibility import is_eligible_article
from researchlib.sources import load_sources


def test_ranking_fixture_authors_are_current_eligible_sources():
    root = Path(__file__).resolve().parents[2]
    authors = runpy.run_path(str(root / "tools/retrieval_eval/gen_corpus.py"))["AUTHORS"]
    sources = {s.id: s for s in load_sources(root / "config/sources.yaml")}
    for source_id, author, _weight in authors:
        source = sources[source_id]
        assert source.enabled and not source.paused and source.author == author
        assert is_eligible_article({"source_id": source_id, "source_kind": source.kind, "raw_meta": {"eval": True}})
