"""Tests for WP-C5 Step 1: per-workspace cross-package candidate capture.

``collect_cross_package_candidates`` captures exactly the import rows that
``build_reverse_index`` DROPS (a specifier resolving to no in-workspace target)
AND that are a genuine cross-package shape, for the coordinator JOIN to resolve.
Pure function over parsed-file objects + the real filesystem resolver.
"""

from __future__ import annotations

from types import SimpleNamespace

from chameleon_mcp.symbol_index import (
    _is_cross_package_specifier,
    collect_cross_package_candidates,
)


def _pf(path, rows):
    return SimpleNamespace(path=str(path), extras={"import_symbols": rows})


def test_captures_scoped_package_and_escaping_relative_not_in_workspace(tmp_path):
    ws = tmp_path / "pkg-b"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "helper.ts").write_text("export const h = 1;\n")  # './helper' resolves in-ws
    b = ws / "src" / "b.ts"
    rows = [
        {"name": "A", "module": "@scope/a", "line": 1},  # scoped pkg -> captured
        {"name": "H", "module": "./helper", "line": 2},  # resolves in-ws -> skipped
        {
            "name": "X",
            "module": "../../pkg-a/x",
            "line": 3,
        },  # escapes ws (src->pkg-b->tmp) -> captured
        {"name": "L", "module": "lodash", "line": 4},  # bare external -> captured (JOIN filters)
        {"name": "M", "module": "./missing", "line": 5},  # in-ws miss -> skipped
    ]
    got = collect_cross_package_candidates([_pf(b, rows)], ws, "typescript")
    names = sorted(c["name"] for c in got)
    assert names == ["A", "L", "X"]
    a = next(c for c in got if c["name"] == "A")
    assert a["module"] == "@scope/a"
    assert a["importer"] == "src/b.ts"  # workspace-relative
    assert a["line"] == 1


def test_is_cross_package_specifier():
    from pathlib import Path

    ws = Path("/repo/pkg-b").resolve()
    imp_dir = Path("/repo/pkg-b/src").resolve()
    assert _is_cross_package_specifier("@scope/a", imp_dir, ws) is True
    assert _is_cross_package_specifier("lodash", imp_dir, ws) is True
    assert _is_cross_package_specifier("./local", imp_dir, ws) is False  # stays in ws
    assert _is_cross_package_specifier("../../other/x", imp_dir, ws) is True  # escapes ws
    assert _is_cross_package_specifier("./deep/../local", imp_dir, ws) is False


def test_empty_and_non_indexed_language(tmp_path):
    assert collect_cross_package_candidates([], tmp_path, "typescript") == []
    # A row with no import_symbols extras contributes nothing.
    pf = SimpleNamespace(path=str(tmp_path / "x.ts"), extras={})
    assert collect_cross_package_candidates([pf], tmp_path, "typescript") == []


def test_capture_is_bounded(tmp_path, monkeypatch):
    import chameleon_mcp.symbol_index as si

    monkeypatch.setattr(si, "_MAX_CROSS_CANDIDATES_PER_WS", 3)
    ws = tmp_path / "pkg-b"
    (ws / "src").mkdir(parents=True)
    rows = [{"name": f"N{i}", "module": f"@scope/p{i}", "line": i} for i in range(10)]
    got = collect_cross_package_candidates([_pf(ws / "src" / "b.ts", rows)], ws, "typescript")
    assert len(got) == 3
