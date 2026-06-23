"""Tests for hook-time Python dimension extraction (_extract_python).

The per-edit hot path cannot spawn the libcst subprocess, so dimension
extraction parses in-process with stdlib ``ast`` (≈10x faster than libcst). The
node-kind vocabulary it produces must match the libcst dump's stored cluster
signature -- chiefly: an ``async def`` must read as ``FunctionDef`` (libcst's
name), not stdlib's ``AsyncFunctionDef``, or every async file would false-flag a
top-level-node-kinds mismatch.
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import extract_dimensions


def test_top_level_kinds_basic():
    src = "import os\nfrom x import y\nCONST = 1\ndef f():\n    pass\nclass C:\n    pass\n"
    snap = extract_dimensions(src, language="python")
    assert set(snap.top_level_node_kinds) == {
        "Import",
        "ImportFrom",
        "Assign",
        "FunctionDef",
        "ClassDef",
    }


def test_async_def_normalized_to_functiondef():
    snap = extract_dimensions("async def handler():\n    return 1\n", language="python")
    assert "FunctionDef" in snap.top_level_node_kinds
    assert "AsyncFunctionDef" not in snap.top_level_node_kinds


def test_default_export_kind_sole_class():
    snap = extract_dimensions("class Only:\n    pass\n", language="python")
    assert snap.default_export_kind == "ClassDef"


def test_default_export_kind_sole_function():
    snap = extract_dimensions("def only():\n    return 1\n", language="python")
    assert snap.default_export_kind == "FunctionDef"


def test_default_export_kind_none_when_mixed():
    snap = extract_dimensions("def f():\n    pass\nclass C:\n    pass\n", language="python")
    assert snap.default_export_kind is None


def test_named_export_count_counts_defs_and_classes():
    src = "def a():\n pass\nasync def b():\n pass\nclass C:\n pass\nX = 1\n"
    snap = extract_dimensions(src, language="python")
    assert snap.named_export_count == 3


def test_jsx_always_false():
    snap = extract_dimensions("x = 1\n", language="python")
    assert snap.jsx_present is False


def test_syntax_error_returns_empty_snapshot_not_crash():
    snap = extract_dimensions("def (:\n  pass\n", language="python")
    assert snap.top_level_node_kinds == []
    assert snap.default_export_kind is None
    assert snap.named_export_count == 0


def test_kinds_match_libcst_dump_on_real_source():
    # Consistency cross-check: the hot-path ast extractor and the bootstrap
    # libcst dump must agree on the SET of top-level kinds (the cluster
    # signature is a sorted set). Mixed async/sync/imports/assignments.
    src = (
        "import os\n"
        "from a.b import c\n"
        "TIMEOUT = 30\n"
        "x: int = 1\n"
        "@deco\n"
        "def sync_fn():\n    pass\n"
        "async def async_fn():\n    return 1\n"
        "class Model(Base):\n    pass\n"
        "if __name__ == '__main__':\n    sync_fn()\n"
    )
    snap = extract_dimensions(src, language="python")
    assert set(snap.top_level_node_kinds) == {
        "Import",
        "ImportFrom",
        "Assign",
        "AnnAssign",
        "FunctionDef",
        "ClassDef",
        "If",
    }
