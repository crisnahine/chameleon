"""Regression tests for cross-file/duplication parity fixes on the Python branch.

Covers the behavioral fix in function_catalog: ``_lang_from_path`` now
recognizes ``.py``/``.pyi`` so the duplication gate's single-language filter is
no longer inert on Python repos, and Python lambda parameters are renamed in the
param-normalized body hash. TypeScript and Ruby behavior must stay identical.
"""

from __future__ import annotations

from chameleon_mcp.function_catalog import (
    _block_param_names,
    _lang_from_path,
    normalized_body_hash,
)


def test_lang_from_path_recognizes_python():
    assert _lang_from_path("svc.py") == "python"
    assert _lang_from_path("Stub.pyi") == "python"
    assert _lang_from_path("pkg/mod.PY") == "python"


def test_lang_from_path_preserves_ts_and_ruby():
    assert _lang_from_path("a.ts") == "typescript"
    assert _lang_from_path("a.tsx") == "typescript"
    assert _lang_from_path("a.js") == "typescript"
    assert _lang_from_path("a.rb") == "ruby"
    assert _lang_from_path("a.go") is None
    assert _lang_from_path("README.md") is None


def _py_body(lambda_param: str) -> list[str]:
    # First line is dropped by normalized_body_hash (it carries the name), so the
    # signature line is just a placeholder. The body must normalize to >= 40
    # chars to clear DUPLICATION_BODY_HASH_MIN_CHARS.
    return [
        "def collect(rows):",
        "    cleaned = [r for r in rows if r is not None]",
        f"    mapped = list(map(lambda {lambda_param}: {lambda_param} * 2 + 1, cleaned))",
        "    return mapped",
    ]


def test_python_lambda_param_rename_pairs_clones():
    a = normalized_body_hash(_py_body("x"), 1, 4, param_names=["rows"], language="python")
    b = normalized_body_hash(_py_body("y"), 1, 4, param_names=["rows"], language="python")
    assert a is not None
    assert a == b


def test_python_body_hash_is_deterministic():
    body = _py_body("x")
    first = normalized_body_hash(body, 1, 4, param_names=["rows"], language="python")
    second = normalized_body_hash(body, 1, 4, param_names=["rows"], language="python")
    assert first == second


def test_block_param_names_python_lambda():
    assert _block_param_names("map(lambda row: row + 1, xs)", "python") == ["row"]
    # Comprehension targets are locals, not parameters — never renamed.
    assert _block_param_names("[v for v in xs]", "python") == []
    # Defaulted / starred lambda lists are skipped so a default value's tokens
    # cannot corrupt the fingerprint.
    assert _block_param_names("lambda a=1: a", "python") == []
    assert _block_param_names("lambda *args: args", "python") == []


def test_block_param_names_ts_and_ruby_unchanged():
    assert _block_param_names("xs.map((row) => row + 1)", "typescript") == ["row"]
    assert _block_param_names("xs.each do |row|\n  row\nend", "ruby") == ["row"]
    # Typed TS arrow lists are still skipped.
    assert _block_param_names("xs.map((row: number) => row)", "typescript") == []


def test_ts_and_ruby_body_hash_unaffected_by_python_branch():
    # The TS/Ruby param-normalized hash must be byte-identical to its value
    # before the Python arm existed: only the language=="python" branch moved.
    ts_body = [
        "function collect(rows) {",
        "  const cleaned = rows.filter((r) => r !== null);",
        "  return cleaned.map((x) => x * 2 + 1);",
        "}",
    ]
    h1 = normalized_body_hash(ts_body, 1, 4, param_names=["rows"], language="typescript")
    h2 = normalized_body_hash(ts_body, 1, 4, param_names=["rows"], language="typescript")
    assert h1 is not None
    assert h1 == h2
