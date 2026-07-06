"""Tool-level tests for explain_edit, the post-incident decision replay.

explain_edit reads the most-recent decision_log row for a file and classifies
why the gate stayed silent: coverage-gap (no archetype / fallback-or-none match
quality), in-scope-miss (ast/exact match that raised nothing), or advised
(ast/exact match that raised advisories but did not block), with
blocked/overridden surfaced when the gate did fire. These drive the real tool
entry point against an on-disk drift.db.

Isolation mirrors test_override_audit_tool.py: CHAMELEON_PLUGIN_DATA at tmp_path,
repo resolution patched so the repo path maps to a known repo_id whose drift.db
holds the decision rows.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import record_decision
from chameleon_mcp.tools import explain_edit

REPO_ID = "c" * 64


def _close_drift_conns() -> None:
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    _close_drift_conns()
    yield
    _close_drift_conns()


@pytest.fixture
def repo(tmp_path):
    stack = ExitStack()
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True, exist_ok=True)
    stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=r))
    stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID))
    try:
        yield r
    finally:
        stack.close()


def _record(rel_path, *, match_quality, outcome, violations=0, rules=None):
    record_decision(
        REPO_ID,
        rel_path,
        archetype="react-component",
        match_quality=match_quality,
        confidence_band="high",
        violations_raised=violations,
        blockable_rules=rules,
        outcome=outcome,
    )


def test_bad_repo_arg():
    out = explain_edit("", "src/a.ts")
    assert out["data"]["status"] == "failed"


def test_missing_file_arg(repo):
    out = explain_edit(str(repo), "  ")
    assert out["data"]["status"] == "failed"


def test_not_found_when_no_row(repo):
    out = explain_edit(str(repo), "src/never-edited.ts")
    data = out["data"]
    assert data["found"] is False
    assert data["classification"] is None


def test_advised_fallback_quality_with_violations(repo):
    # fallback/none quality drops the archetype-SHAPE rules, so a raised violation
    # there is necessarily an archetype-INDEPENDENT rule (a secret, an eval) that
    # DID fire -- the gate was not silent, so this is "advised", not a coverage
    # gap. Classifying it coverage-gap routed a flagged-and-overridden credential
    # to the wrong remediation ("refresh so an archetype resolves").
    _record("src/a.ts", match_quality="fallback", outcome="advised", violations=1)
    out = explain_edit(str(repo), str(repo / "src" / "a.ts"))
    data = out["data"]
    assert data["found"] is True
    assert data["classification"] == "advised"
    assert data["decision"]["match_quality"] == "fallback"


def test_coverage_gap_none_quality(repo):
    _record("src/b.ts", match_quality="none", outcome="advised")
    out = explain_edit(str(repo), "src/b.ts")
    assert out["data"]["classification"] == "coverage-gap"


def test_advised_ast_match_with_violations(repo):
    # ast match that RAISED advisories (but did not block) -> "advised", not a
    # miss: the rules fired, they were advisory. Kept distinct from a true miss.
    _record("src/c.ts", match_quality="ast", outcome="advised", violations=2)
    out = explain_edit(str(repo), "src/c.ts")
    data = out["data"]
    assert data["classification"] == "advised"
    assert data["decision"]["violations_raised"] == 2


def test_in_scope_miss_ast_match_silent(repo):
    # ast match that raised NOTHING -> a true in-scope miss.
    _record("src/c2.ts", match_quality="ast", outcome="advised", violations=0)
    out = explain_edit(str(repo), "src/c2.ts")
    assert out["data"]["classification"] == "in-scope-miss"


def test_in_scope_miss_clean_exact_match(repo):
    _record("src/d.ts", match_quality="exact", outcome="clean")
    out = explain_edit(str(repo), "src/d.ts")
    assert out["data"]["classification"] == "in-scope-miss"


def test_blocked_outcome_surfaced(repo):
    _record(
        "src/e.ts",
        match_quality="ast",
        outcome="blocked",
        violations=1,
        rules=["import-preference-violation"],
    )
    out = explain_edit(str(repo), "src/e.ts")
    data = out["data"]
    assert data["classification"] == "blocked"
    assert data["decision"]["blockable_rules"] == ["import-preference-violation"]


def test_overridden_outcome_surfaced(repo):
    _record("src/f.ts", match_quality="ast", outcome="overridden", violations=1)
    out = explain_edit(str(repo), "src/f.ts")
    assert out["data"]["classification"] == "overridden"


def test_most_recent_row_wins(repo):
    record_decision(
        REPO_ID,
        "src/g.ts",
        archetype="react-component",
        match_quality="fallback",
        confidence_band="low",
        violations_raised=0,
        outcome="advised",
        observed_at=100,
    )
    record_decision(
        REPO_ID,
        "src/g.ts",
        archetype="react-component",
        match_quality="ast",
        confidence_band="high",
        violations_raised=0,
        outcome="clean",
        observed_at=200,
    )
    out = explain_edit(str(repo), "src/g.ts")
    # Latest is the ast/clean row -> in-scope-miss, not the earlier fallback gap.
    assert out["data"]["classification"] == "in-scope-miss"


def test_absolute_and_relative_paths_resolve_same_row(repo):
    _record("src/h.ts", match_quality="ast", outcome="clean")
    by_abs = explain_edit(str(repo), str(repo / "src" / "h.ts"))
    by_rel = explain_edit(str(repo), "src/h.ts")
    assert by_abs["data"]["found"] is True
    assert by_rel["data"]["found"] is True
    assert by_abs["data"]["rel_path"] == by_rel["data"]["rel_path"] == "src/h.ts"


def test_failopen_when_latest_decision_raises(repo, monkeypatch):
    monkeypatch.setattr(
        "chameleon_mcp.drift.observations.latest_decision",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = explain_edit(str(repo), "src/a.ts")
    # A read failure degrades to not-found rather than raising.
    assert out["data"]["found"] is False
