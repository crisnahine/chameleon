"""Tests for the CandidateIndex and build_candidate_index (Task 5)."""

from __future__ import annotations

from chameleon_mcp.duplication_review import CandidateIndex
from chameleon_mcp.function_catalog import ParsedFn


def test_index_matches_by_exact_and_pnorm():
    idx = CandidateIndex()
    idx.add_function("services/a.rb", "existing_fn", body_hash="abc", body_hash_pnorm="pnorm1")
    # exact body-hash hit, different file
    pf = ParsedFn("clone", "method", 0, 0, 5, "abc", "other", "body")
    hit, match_type = idx.lookup(pf, exclude_file="services/b.rb")
    assert hit is not None and hit.name == "existing_fn" and hit.file == "services/a.rb"
    assert match_type == "exact"
    # pnorm hit
    pf2 = ParsedFn("clone2", "method", 0, 0, 5, None, "pnorm1", "body")
    hit2, match_type2 = idx.lookup(pf2, exclude_file="services/b.rb")
    assert hit2 is not None
    assert match_type2 == "pnorm"
    # self-file excluded
    hit3, mt3 = idx.lookup(pf, exclude_file="services/a.rb")
    assert hit3 is None and mt3 is None
    # no match
    pf3 = ParsedFn("x", "method", 0, 0, 5, "zzz", "yyy", "body")
    hit4, mt4 = idx.lookup(pf3, exclude_file="services/b.rb")
    assert hit4 is None and mt4 is None
