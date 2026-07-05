"""Tests for WP-C5 Step 2 core: the coordinator cross-workspace JOIN.

``build_cross_reverse_index`` resolves each captured cross-package candidate to
the sibling workspace file it targets (via the package-name map + filesystem
probing), confirms the imported name is actually exported there (fail-closed),
and emits the mono-relative-keyed cross_reverse_index.json payload. Resolution
probes the real filesystem, so tests build a real crafted monorepo tree.
"""

from __future__ import annotations

import json

from chameleon_mcp.symbol_index import (
    CROSSWS_SCHEMA_VERSION,
    build_cross_reverse_index,
)


def _w(root, rel, body=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _mono(tmp_path):
    # package A exports foo (via package.json main), plus a subpath module.
    _w(
        tmp_path,
        "packages/a/package.json",
        json.dumps({"name": "@scope/a", "main": "src/index.ts"}),
    )
    _w(tmp_path, "packages/a/src/index.ts", "export function foo() {}\n")
    # Plain subpath `@scope/a/sub` resolves relative to the PACKAGE ROOT (v1); a
    # src/-layout subpath needs a package.json exports map (documented v1 gap).
    _w(tmp_path, "packages/a/sub.ts", "export const bar = 1;\n")
    _w(tmp_path, "packages/b/src/b.ts", "")
    return {"@scope/a": "packages/a"}


def test_scoped_root_import_resolves_via_package_main(tmp_path):
    packages = _mono(tmp_path)
    cands = [{"importer": "packages/b/src/b.ts", "name": "foo", "module": "@scope/a", "line": 1}]
    exports = {"packages/a/src/index.ts": {"foo"}}
    out = build_cross_reverse_index(cands, packages, tmp_path, exports)
    assert out["schema_version"] == CROSSWS_SCHEMA_VERSION
    assert out["targets"]["packages/a/src/index.ts"]["foo"] == [
        {"path": "packages/b/src/b.ts", "line": 1}
    ]
    assert out["packages"]["@scope/a"] == "packages/a"


def test_scoped_subpath_import_resolves(tmp_path):
    packages = _mono(tmp_path)
    cands = [
        {"importer": "packages/b/src/b.ts", "name": "bar", "module": "@scope/a/sub", "line": 2}
    ]
    exports = {"packages/a/sub.ts": {"bar"}}
    out = build_cross_reverse_index(cands, packages, tmp_path, exports)
    assert out["targets"]["packages/a/sub.ts"]["bar"] == [
        {"path": "packages/b/src/b.ts", "line": 2}
    ]


def test_name_not_exported_is_fail_closed_no_edge(tmp_path):
    packages = _mono(tmp_path)
    cands = [{"importer": "packages/b/src/b.ts", "name": "ghost", "module": "@scope/a", "line": 1}]
    exports = {"packages/a/src/index.ts": {"foo"}}  # ghost is NOT exported
    out = build_cross_reverse_index(cands, packages, tmp_path, exports)
    assert out["targets"] == {}


def test_external_package_no_workspace_entry_no_edge(tmp_path):
    packages = _mono(tmp_path)
    cands = [{"importer": "packages/b/src/b.ts", "name": "x", "module": "lodash", "line": 1}]
    out = build_cross_reverse_index(cands, packages, tmp_path, {"whatever": {"x"}})
    assert out["targets"] == {}


def test_relative_escape_import_resolves(tmp_path):
    _mono(tmp_path)
    # b imports from a via a relative path that escapes package b.
    cands = [
        {"importer": "packages/b/src/b.ts", "name": "foo", "module": "../../a/src/index", "line": 3}
    ]
    exports = {"packages/a/src/index.ts": {"foo"}}
    out = build_cross_reverse_index(cands, {}, tmp_path, exports)
    assert out["targets"]["packages/a/src/index.ts"]["foo"] == [
        {"path": "packages/b/src/b.ts", "line": 3}
    ]


def test_callable_exports_lookup(tmp_path):
    packages = _mono(tmp_path)
    cands = [{"importer": "packages/b/src/b.ts", "name": "foo", "module": "@scope/a", "line": 1}]
    out = build_cross_reverse_index(
        cands, packages, tmp_path, lambda k: {"foo"} if "index" in k else set()
    )
    assert "foo" in out["targets"]["packages/a/src/index.ts"]


def test_empty_and_malformed_candidates(tmp_path):
    out = build_cross_reverse_index([], {}, tmp_path, {})
    assert out["targets"] == {} and out["schema_version"] == CROSSWS_SCHEMA_VERSION
    out2 = build_cross_reverse_index([None, {"importer": "x"}, "junk"], {}, tmp_path, {})
    assert out2["targets"] == {}
