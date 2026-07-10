"""Regression tests for the libcst dump's cross-language-parity fixes.

Covers three confirmed findings against scripts/libcst_dump.py:

1. class_shapes was appended uncapped while ts_dump caps the equivalent push at
   MAX_CALLABLE_SIGNATURES (200) -- a generated megafile bloated the dump.
2. A libcst ParserSyntaxError dropped the whole file, where ts_dump / prism_dump
   keep contributing; libcst's grammar is release-pinned, so valid Python newer
   than that grammar dropped silently. The dump now re-parses with stdlib ``ast``
   and emits a degraded-but-valid import/export record when the interpreter
   accepts the file, dropping only when ``ast`` also rejects it.
3. A walk-time exception other than the node ceiling / RecursionError was labeled
   extractor_crash by main()'s catch-all instead of the finer walk_error that
   ts_dump / prism_dump emit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

cst = pytest.importorskip("libcst")

_LIBCST_DUMP = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "libcst_dump.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("libcst_dump_under_test", _LIBCST_DUMP)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


dump = _load_module()


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# --- Finding 1: class_shapes is capped at MAX_CALLABLE_SIGNATURES ---


def test_class_shapes_capped_like_callable_signatures(tmp_path):
    over_cap = dump.MAX_CALLABLE_SIGNATURES + 50
    src = "".join(f"class C{i}(Base):\n    pass\n\n" for i in range(over_cap))
    record = dump.extract_file(_write(tmp_path, "many_classes.py", src))

    assert "error" not in record
    assert len(record["class_shapes"]) == dump.MAX_CALLABLE_SIGNATURES
    # named_export_count is the honest pre-cap total, mirroring how call_sites
    # keeps call_sites_total alongside the capped list.
    assert record["named_export_count"] == over_cap


def test_class_shapes_uncapped_when_under_limit(tmp_path):
    src = "class Only(Base):\n    pass\n"
    record = dump.extract_file(_write(tmp_path, "one_class.py", src))

    assert "error" not in record
    assert len(record["class_shapes"]) == 1
    assert record["class_shapes"][0]["name"] == "Only"


# --- Finding 2: grammar-skew recovery via stdlib ast ---


def test_genuine_syntax_error_drops_as_parse_error(tmp_path):
    # ast also rejects this, so there is nothing to recover: still parse_error.
    src = "import os\ndef good():\n    return 1\ndef broken(:\n    pass\n"
    record = dump.extract_file(_write(tmp_path, "broken.py", src))

    assert record["error"] == "parse_error"


def test_libcst_reject_but_ast_accepts_recovers_record(tmp_path, monkeypatch):
    # Simulate the grammar-skew case: libcst refuses a file the running
    # interpreter parses fine. The file must still contribute its import/export
    # surface instead of being dropped.
    src = (
        "from package import helper, Widget as W\n"
        "import os.path as op\n"
        "from . import sibling\n"
        "CONST = 1\n"
        "class Service(Base):\n"
        "    pass\n"
        "def run():\n"
        "    pass\n"
    )
    path = _write(tmp_path, "future_syntax.py", src)

    def _raise(_content):
        raise cst.ParserSyntaxError("Syntax Error @ 1:1.", lines=[src], raw_line=1, raw_column=0)

    monkeypatch.setattr(dump.cst, "parse_module", _raise)
    record = dump.extract_file(path)

    assert "error" not in record
    # Recovered records are marked, not a clean parse.
    assert record["parse_diagnostics_count"] == 1
    # A top-level-only scan cannot enumerate the closed export set, so the set is
    # left open and the phantom-symbol absence check skips this record.
    assert record["export_set_open"] is True
    # Import/export surface is recovered.
    assert "helper" in record["named_export_names"]
    assert "W" in record["named_export_names"]
    assert "op" in record["named_export_names"]
    assert "sibling" in record["named_export_names"]
    assert "CONST" in record["named_export_names"]
    assert "Service" in record["named_export_names"]
    assert "run" in record["named_export_names"]
    assert ["package", "named"] in record["import_specifiers"]
    assert ["os.path", "namespace"] in record["import_specifiers"]
    # The CST-only fields are empty (no libcst tree to walk), but the shape holds.
    assert record["callable_signatures"] == []
    assert record["call_sites"] == []
    assert record["import_symbols"] == []


def test_recover_with_ast_marks_star_import_open(tmp_path):
    record = dump._recover_with_ast("/abs/star.py", "from m import *\nX = 1\n")
    assert record is not None
    assert record["export_set_open"] is True


def test_recover_set_open_even_for_conditional_only_bindings():
    # A name bound only inside `if TYPE_CHECKING:` is invisible to the top-level
    # scan. Leaving the set closed would draw a false "module does not export"
    # absence violation, so the recovered record's set must be open regardless.
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from pkg import Conditional\n"
        "VALUE = 1\n"
    )
    record = dump._recover_with_ast("/abs/typed.py", src)
    assert record is not None
    assert record["export_set_open"] is True
    # The conditional binding is not in the top-level name set, which is exactly
    # why the set is opened rather than treated as authoritative.
    assert "Conditional" not in record["named_export_names"]


def test_recover_with_ast_returns_none_on_real_syntax_error():
    assert dump._recover_with_ast("/abs/x.py", "def f(:\n    pass\n") is None


# --- Finding 3: a walk-time fault is classified walk_error, not extractor_crash ---


def test_walk_time_fault_is_walk_error(tmp_path, monkeypatch):
    path = _write(tmp_path, "ok.py", "def f():\n    return 1\n")

    class _Boom:
        def __init__(self, *_a, **_k):
            pass

        def visit(self, _collector):
            raise RuntimeError("metadata resolution exploded")

    monkeypatch.setattr(dump, "MetadataWrapper", _Boom)
    record = dump.extract_file(path)

    assert record["error"] == "walk_error"
    assert "metadata resolution exploded" in record["message"]
