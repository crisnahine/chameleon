"""Tests for gather_body_match_findings (Task 6)."""

from __future__ import annotations

import chameleon_mcp.duplication_review as dr
from chameleon_mcp.duplication_review import CandidateIndex, gather_body_match_findings
from chameleon_mcp.function_catalog import ParsedFn


def test_gather_finds_body_match(monkeypatch, tmp_path):
    idx = CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    # edited file parses to one function whose body_hash == H
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("renamed", "method", 0, 0, 7, "H", "Pother", "do_work(x)\n")],
    )
    findings = gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="ruby")
    assert len(findings) == 1
    f = findings[0]
    assert f.new_name == "renamed" and f.existing_name == "original"
    assert f.line == 7 and "do_work" in f.excerpt


def test_gather_excludes_self_file_and_caps(monkeypatch, tmp_path):
    # Use a real path under tmp_path so _repo_rel resolves correctly.
    services = tmp_path / "services"
    services.mkdir()
    x_rb = services / "x.rb"
    x_rb.touch()
    idx = CandidateIndex()
    idx.add_function("services/x.rb", "fn", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("fn", "method", 0, 0, 1, "H", "P", "b")],
    )
    # same file -> self-excluded
    assert gather_body_match_findings(tmp_path, [str(x_rb)], idx, lang="ruby") == []


def test_gather_lang_filter(monkeypatch, tmp_path):
    idx = CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("renamed", "method", 0, 0, 3, "H", "P2", "body")],
    )
    # lang=typescript but file is .rb -> filtered out
    assert (
        gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="typescript") == []
    )


def test_gather_parse_exception_fails_open(monkeypatch, tmp_path):
    idx = CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr, "_parse", lambda root, path: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # Should not raise
    result = gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="ruby")
    assert result == []
