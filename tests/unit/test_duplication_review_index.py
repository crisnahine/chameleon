"""Tests for the CandidateIndex and build_candidate_index (Task 5)."""

from __future__ import annotations

from chameleon_mcp.duplication_review import CandidateIndex
from chameleon_mcp.function_catalog import ParsedFn


def test_index_matches_by_exact_and_pnorm():
    idx = CandidateIndex()
    idx.add_function("services/a.rb", "existing_fn", body_hash="abc", body_hash_pnorm="pnorm1")
    # exact body-hash hit, different file
    pf = ParsedFn("clone", "method", 0, 0, 5, "abc", "other", "body")
    hit = idx.lookup(pf, exclude_file="services/b.rb")
    assert hit is not None and hit.name == "existing_fn" and hit.file == "services/a.rb"
    # pnorm hit
    pf2 = ParsedFn("clone2", "method", 0, 0, 5, None, "pnorm1", "body")
    assert idx.lookup(pf2, exclude_file="services/b.rb") is not None
    # self-file excluded
    assert idx.lookup(pf, exclude_file="services/a.rb") is None
    # no match
    pf3 = ParsedFn("x", "method", 0, 0, 5, "zzz", "yyy", "body")
    assert idx.lookup(pf3, exclude_file="services/b.rb") is None
