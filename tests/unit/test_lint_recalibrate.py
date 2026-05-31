"""recalibrate_ast_query enables all 5 lint dimensions (regex-vs-regex).

Production lint always recalibrates the witness ast_query before comparing, and
it used to NULL 3 of the 5 dimensions (default_export_kind, jsx_present,
named_export_count_bucket) — so only 2 of 5 were ever enforced and the
error-severity jsx rule was dead. Sourcing the 3 dims from the witness's own
regex snapshot makes the comparison regex-vs-regex (the gap the nulling avoided),
so all 5 enforce without false positives on conforming code.
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import extract_dimensions, lint, recalibrate_ast_query


def _q(witness_src: str) -> dict:
    return recalibrate_ast_query(extract_dimensions(witness_src, language="typescript"))


def test_recalibrate_enables_the_three_nulled_dimensions():
    q = _q("export default class Foo {}\n")
    assert q["default_export_kind"] is not None  # was hardcoded None


def test_conforming_candidate_has_no_violations():
    """Regex-vs-regex: a same-shape candidate must NOT raise a false positive."""
    q = _q("export default class Foo {}\n")
    same = extract_dimensions("export default class Bar {}\n", language="typescript")
    assert lint(same, q) == []


def test_divergent_default_export_is_flagged():
    q = _q("export default class Foo {}\n")
    diff = extract_dimensions("export default function bar() {}\n", language="typescript")
    rules = {v.rule for v in lint(diff, q)}
    assert "default-export-kind-mismatch" in rules


def test_core_only_flag_restores_two_dimensions(monkeypatch):
    monkeypatch.setenv("CHAMELEON_LINT_DIMENSIONS", "core")
    q = _q("export default class Foo {}\n")
    assert q["default_export_kind"] is None
    assert q["jsx_present"] is None
    assert q["named_export_count_bucket"] is None
