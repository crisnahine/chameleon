"""WP-C5 Step 3: the cross-workspace index loader (plugin-data, fail-open).

``load_cross_reverse_index(path)`` reads the plugin-data cross index into a
(ReverseIndex, packages) pair, reusing ReverseIndex.broken_importers over the
mono-relative targets. Fail-open to None on any ambiguity.
"""

from __future__ import annotations

import json

from chameleon_mcp.symbol_index import CROSSWS_SCHEMA_VERSION, load_cross_reverse_index


def _write(p, payload):
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _valid(tmp_path):
    return _write(
        tmp_path / "cross_reverse_index.json",
        {
            "schema_version": CROSSWS_SCHEMA_VERSION,
            "targets": {"packages/a/index.ts": {"foo": [{"path": "packages/b/b.ts", "line": 1}]}},
            "packages": {"@scope/a": "packages/a"},
        },
    )


def test_loads_targets_and_packages(tmp_path):
    ri, packages = load_cross_reverse_index(_valid(tmp_path))
    assert packages == {"@scope/a": "packages/a"}
    broken = ri.broken_importers("packages/a/index.ts", set())  # foo removed
    assert [(i.path, i.line) for i in broken["foo"]] == [("packages/b/b.ts", 1)]


def test_still_exported_is_not_broken(tmp_path):
    ri, _ = load_cross_reverse_index(_valid(tmp_path))
    assert ri.broken_importers("packages/a/index.ts", {"foo"}) == {}


def test_missing_path_returns_none(tmp_path):
    assert load_cross_reverse_index(tmp_path / "nope.json") is None
    assert load_cross_reverse_index(None) is None


def test_foreign_schema_returns_none(tmp_path):
    p = _write(tmp_path / "cross_reverse_index.json", {"schema_version": 999, "targets": {}})
    assert load_cross_reverse_index(p) is None


def test_corrupt_and_empty_return_none(tmp_path):
    p = tmp_path / "cross_reverse_index.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_cross_reverse_index(p) is None
    p.write_text("", encoding="utf-8")
    assert load_cross_reverse_index(p) is None


def test_malformed_rows_skipped(tmp_path):
    p = _write(
        tmp_path / "cross_reverse_index.json",
        {
            "schema_version": CROSSWS_SCHEMA_VERSION,
            "targets": {
                "a.ts": {"x": [{"path": "b.ts", "line": 2}, {"nopath": 1}, "junk"]},
                123: {"y": []},
            },
            "packages": {"@scope/a": "packages/a", "bad": 5},
        },
    )
    ri, packages = load_cross_reverse_index(p)
    assert packages == {"@scope/a": "packages/a"}  # non-str value dropped
    broken = ri.broken_importers("a.ts", set())
    assert [(i.path, i.line) for i in broken["x"]] == [("b.ts", 2)]


def test_mtime_cache_refresh(tmp_path):
    import os

    p = _valid(tmp_path)
    ri1, _ = load_cross_reverse_index(p)
    _write(p, {"schema_version": CROSSWS_SCHEMA_VERSION, "targets": {}, "packages": {}})
    os.utime(p, (0, 0))  # force a distinct mtime token
    ri2, _ = load_cross_reverse_index(p)
    assert ri2.broken_importers("packages/a/index.ts", set()) == {}  # picked up the rewrite
