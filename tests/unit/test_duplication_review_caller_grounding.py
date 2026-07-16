"""Caller-grounded duplication verdict: a confirmed re-implementation cites how
many committed sites already call the original, so the advisory reads
"reuse it; already called from N sites" instead of a bare "reuse it".

The count is the committed calls index's graded-edge total (the same edges the
judge's caller facts use): it can miss dynamic dispatch and, rarely, overcount a
binding-shadowed import, so it is an estimate. A function called only by its own
recursion is not "reused", so the clause is dropped; a zero/absent entry is
dropped too rather than claim "called from 0 sites".
"""

from __future__ import annotations

import chameleon_mcp.duplication_review as dr
from chameleon_mcp.calls_index import CallsIndex
from chameleon_mcp.duplication_review import Finding
from chameleon_mcp.function_catalog import (
    CatalogedFunction,
    FunctionCatalog,
    ParsedFn,
    name_tokens,
)
from chameleon_mcp.stop.lenses.duplication import _claim_for


def _calls(rel: str, name: str, *, total: int, truncated: bool = False, rows=None) -> CallsIndex:
    if rows is None:
        rows = [{"path": "caller.rb", "caller": "c", "line": 1, "grade": "import"}]
    return CallsIndex({rel: {name: {"callers": rows, "total": total, "truncated": truncated}}})


# --- _caller_count: external-caller estimate, or None --------------------------


def test_caller_count_returns_total():
    calls = _calls("app/b.rb", "original", total=7)
    assert dr._caller_count(calls, "app/b.rb", "original") == 7


def test_caller_count_none_for_unrecorded_pair():
    calls = _calls("app/b.rb", "original", total=7)
    assert dr._caller_count(calls, "app/b.rb", "missing") is None


def test_caller_count_zero_collapses_to_none():
    # An entry that exists but records no callers must NOT render "0 sites".
    calls = CallsIndex({"app/b.rb": {"orig": {"callers": [], "total": 0, "truncated": False}}})
    assert dr._caller_count(calls, "app/b.rb", "orig") is None


def test_caller_count_none_index():
    assert dr._caller_count(None, "app/b.rb", "original") is None


def test_caller_count_fails_open_on_lookup_error():
    class Boom:
        def callers_of(self, rel, name):
            raise RuntimeError("boom")

    assert dr._caller_count(Boom(), "app/b.rb", "original") is None


def test_caller_count_tolerates_non_dict_rows():
    # The module fails open everywhere: a malformed callers list (non-dict rows)
    # must never raise out of _caller_count into the gather loop. A row that is
    # not a self-call dict simply does not satisfy the self-only drop, so the
    # total stands.
    class Weird:
        def callers_of(self, rel, name):
            return {"callers": ["not-a-dict", None], "total": 3, "truncated": False}

    assert dr._caller_count(Weird(), "app/b.rb", "x") == 3


def test_caller_count_drops_purely_self_referential():
    # A function whose only committed caller is its own recursion is not
    # "reused" — the clause must not render "called from 1 site".
    rows = [{"path": "app/b.rb", "caller": "fib", "line": 3, "grade": "same_file"}]
    calls = _calls("app/b.rb", "fib", total=1, truncated=False, rows=rows)
    assert dr._caller_count(calls, "app/b.rb", "fib") is None


def test_caller_count_keeps_mixed_self_and_external():
    # Self-calls alongside a real external caller keep the (judge-consistent)
    # total; only purely-self counts are dropped.
    rows = [
        {"path": "app/b.rb", "caller": "fib", "line": 3, "grade": "same_file"},
        {"path": "app/caller.rb", "caller": "compute", "line": 9, "grade": "import"},
    ]
    calls = _calls("app/b.rb", "fib", total=2, truncated=False, rows=rows)
    assert dr._caller_count(calls, "app/b.rb", "fib") == 2


def test_caller_count_keeps_total_when_truncated_self_only():
    # When the stored rows are capped (truncated), a hidden external caller may
    # exist, so a self-only visible list must NOT drop the count.
    rows = [{"path": "app/b.rb", "caller": "fib", "line": 3, "grade": "same_file"}]
    calls = _calls("app/b.rb", "fib", total=40, truncated=True, rows=rows)
    assert dr._caller_count(calls, "app/b.rb", "fib") == 40


# --- _claim_for: rendering the caller-count clause ------------------------------


def test_claim_renders_exact_caller_count():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb", called_from_n_sites=7)
    claim = _claim_for(f)
    assert "already called from 7 sites" in claim
    assert "7+ sites" not in claim


def test_claim_caller_count_singular():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb", called_from_n_sites=1)
    claim = _claim_for(f)
    assert "already called from 1 site" in claim
    assert "1 sites" not in claim


def test_claim_omits_clause_when_no_callers():
    f = Finding("renamed", "app/a.rb", 7, "x", "original", "app/b.rb")
    claim = _claim_for(f)
    assert "called from" not in claim
    assert claim.rstrip().endswith("reuse it.")


# --- gather wiring: counts attach to real findings -----------------------------


def test_gather_body_match_attaches_caller_count(monkeypatch, tmp_path):
    idx = dr.CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("renamed", "method", 0, 0, 7, "H", "Pother", "do_work(x)\n")],
    )
    monkeypatch.setattr(
        dr, "_load_calls", lambda root: _calls("services/orig.rb", "original", total=5)
    )
    findings = dr.gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="ruby")
    assert len(findings) == 1
    assert findings[0].called_from_n_sites == 5


def test_gather_semantic_attaches_caller_count(monkeypatch, tmp_path):
    existing = CatalogedFunction(
        name="strip_attributes",
        kind="method",
        file="app/models/concerns/sanitizable.rb",
        arity=1,
        required=1,
        tokens=name_tokens("strip_attributes"),
        body_hash="EXISTING_HASH",
        body_hash_pnorm="EXISTING_PNORM",
    )
    catalog = FunctionCatalog([existing])
    new_fn = ParsedFn(
        name="strip_widget_attributes",
        kind="method",
        arity=1,
        required=1,
        start_line=5,
        body_hash="NEW_HASH",
        body_hash_pnorm="NEW_PNORM",
        excerpt="def strip_widget_attributes(w)\n  w.strip\nend\n",
    )
    monkeypatch.setattr(dr, "_parse", lambda root, path: [new_fn])
    monkeypatch.setattr(
        dr,
        "_load_calls",
        lambda root: _calls("app/models/concerns/sanitizable.rb", "strip_attributes", total=12),
    )
    findings = dr.gather_semantic_findings(tmp_path, ["app/models/widget.rb"], catalog, lang="ruby")
    assert len(findings) == 1
    assert findings[0].called_from_n_sites == 12


def test_gather_body_match_no_calls_index_leaves_count_none(monkeypatch, tmp_path):
    idx = dr.CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("renamed", "method", 0, 0, 7, "H", "Pother", "do_work(x)\n")],
    )
    monkeypatch.setattr(dr, "_load_calls", lambda root: None)
    findings = dr.gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="ruby")
    assert len(findings) == 1
    assert findings[0].called_from_n_sites is None


def test_gather_body_match_caller_lookup_failure_fails_open(monkeypatch, tmp_path):
    class Boom:
        def callers_of(self, rel, name):
            raise RuntimeError("boom")

    idx = dr.CandidateIndex()
    idx.add_function("services/orig.rb", "original", body_hash="H", body_hash_pnorm="P")
    monkeypatch.setattr(
        dr,
        "_parse",
        lambda root, path: [ParsedFn("renamed", "method", 0, 0, 7, "H", "Pother", "do_work(x)\n")],
    )
    monkeypatch.setattr(dr, "_load_calls", lambda root: Boom())
    findings = dr.gather_body_match_findings(tmp_path, ["services/renamed.rb"], idx, lang="ruby")
    assert len(findings) == 1
    assert findings[0].called_from_n_sites is None
