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


def _scoping_fixture(monkeypatch, tmp_path):
    # A real file under tmp_path so _repo_rel resolves to "services/x.rb" (the
    # changed_ranges key); one parsed function "dup" spanning lines 5..9 whose
    # body_hash matches an indexed original.
    svc = tmp_path / "services"
    svc.mkdir()
    xrb = svc / "x.rb"
    xrb.touch()
    idx = CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    pf = ParsedFn("dup", "method", 0, 0, 5, "H", "P2", "body\n", end_line=9)
    monkeypatch.setattr(dr, "_parse", lambda root, path: [pf])
    return idx, str(xrb)


def test_gather_diff_scoping_suppresses_untouched_function(monkeypatch, tmp_path):
    # A pre-existing duplicate the turn did NOT touch must not be flagged just
    # because the file was edited elsewhere (the recurring-noise complaint):
    # changed line 20 does not overlap the function span 5..9.
    idx, xrb = _scoping_fixture(monkeypatch, tmp_path)
    assert (
        gather_body_match_findings(
            tmp_path, [xrb], idx, lang="ruby", changed_ranges={"services/x.rb": {20}}
        )
        == []
    )


def test_gather_diff_scoping_surfaces_changed_function(monkeypatch, tmp_path):
    # A duplicate the author actually edited (span overlaps a changed line), a new
    # file (None marker), and a non-git repo (no changed_ranges) all stay in scope.
    idx, xrb = _scoping_fixture(monkeypatch, tmp_path)
    assert (
        len(
            gather_body_match_findings(
                tmp_path, [xrb], idx, lang="ruby", changed_ranges={"services/x.rb": {7}}
            )
        )
        == 1
    )
    assert (
        len(
            gather_body_match_findings(
                tmp_path, [xrb], idx, lang="ruby", changed_ranges={"services/x.rb": None}
            )
        )
        == 1
    )
    assert len(gather_body_match_findings(tmp_path, [xrb], idx, lang="ruby")) == 1
