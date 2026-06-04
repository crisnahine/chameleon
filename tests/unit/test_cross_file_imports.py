"""Unit tests for the index-backed cross-file import advisory.

`lint_cross_file_imports` reads the prebuilt reverse index (symbol -> importers)
and the edited module's current export set. It emits, advisory-only:
  - cross-file-importers: "N files import `name` from this module" for a name the
    module still exports that has indexed importers.
  - removed-export-breaks-importers: a name an importer references that the module
    no longer exports (a deterministic existence break).
"""

import json
from pathlib import Path

from chameleon_mcp.phantom_imports import _current_export_names, lint_cross_file_imports
from chameleon_mcp.symbol_index import REVERSE_INDEX_FILENAME, SCHEMA_VERSION


def _write_reverse(repo: Path, targets: dict) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / REVERSE_INDEX_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "targets": targets}), encoding="utf-8"
    )


def _lint(repo: Path, module_rel: str, content: str):
    return lint_cross_file_imports(
        content,
        file_path=str(repo / module_rel),
        repo_root=str(repo),
        language="typescript",
    )


class TestCurrentExportNames:
    def test_direct_declarations(self):
        names, open_set = _current_export_names(
            "export const a = 1;\n"
            "export function b() {}\n"
            "export class C {}\n"
            "export async function d() {}\n"
            "export type T = number;\n"
        )
        assert open_set is False
        assert names == frozenset({"a", "b", "C", "d", "T"})

    def test_export_clause_with_alias(self):
        names, open_set = _current_export_names("const x = 1;\nexport { x as price };\n")
        assert open_set is False
        assert "price" in names
        assert "x" not in names  # only the EXPORTED name counts

    def test_export_star_is_open(self):
        names, open_set = _current_export_names("export * from './other';\n")
        assert open_set is True
        assert names == frozenset()

    def test_default_not_counted(self):
        names, _ = _current_export_names("export default function () {}\n")
        assert "default" not in names

    def test_export_in_string_is_ignored(self):
        # An export written inside a string literal must not be picked up.
        names, _ = _current_export_names('const s = "export const fake = 1";\n')
        assert "fake" not in names


class TestCrossFileImporters:
    def test_reports_importer_count(self, tmp_path):
        _write_reverse(
            tmp_path,
            {
                "pricing.ts": {
                    "editPrice": [
                        {"path": "cart.ts", "line": 3},
                        {"path": "checkout.ts", "line": 8},
                    ]
                }
            },
        )
        v = _lint(tmp_path, "pricing.ts", "export function editPrice() {}\n")
        assert len(v) == 1
        assert v[0].rule == "cross-file-importers"
        assert v[0].expected == "editPrice"
        assert v[0].actual == "2"
        assert v[0].severity == "info"
        assert "2 files import 'editPrice'" in v[0].message

    def test_singular_phrasing(self, tmp_path):
        _write_reverse(tmp_path, {"m.ts": {"foo": [{"path": "a.ts", "line": 1}]}})
        v = _lint(tmp_path, "m.ts", "export const foo = 1;\n")
        assert "1 file import 'foo'" in v[0].message

    def test_no_index_silent(self, tmp_path):
        # No reverse_index.json on disk.
        v = _lint(tmp_path, "m.ts", "export const foo = 1;\n")
        assert v == []

    def test_module_with_no_importers_silent(self, tmp_path):
        _write_reverse(tmp_path, {"other.ts": {"x": [{"path": "a.ts", "line": 1}]}})
        v = _lint(tmp_path, "m.ts", "export const foo = 1;\n")
        assert v == []

    def test_non_typescript_silent(self, tmp_path):
        _write_reverse(tmp_path, {"m.ts": {"foo": [{"path": "a.ts", "line": 1}]}})
        v = lint_cross_file_imports(
            "export const foo = 1;\n",
            file_path=str(tmp_path / "m.ts"),
            repo_root=str(tmp_path),
            language="ruby",
        )
        assert v == []


class TestRemovedExportBreak:
    def test_removed_export_flags_break(self, tmp_path):
        _write_reverse(
            tmp_path,
            {"pricing.ts": {"editPrice": [{"path": "cart.ts", "line": 3}]}},
        )
        # The module no longer exports editPrice (renamed to setPrice).
        v = _lint(tmp_path, "pricing.ts", "export function setPrice() {}\n")
        rules = {x.rule for x in v}
        assert "removed-export-breaks-importers" in rules
        broken = next(x for x in v if x.rule == "removed-export-breaks-importers")
        assert broken.expected == "editPrice"
        assert broken.severity == "warning"
        assert "cart.ts:3" in broken.message

    def test_present_export_not_broken(self, tmp_path):
        _write_reverse(
            tmp_path,
            {"pricing.ts": {"editPrice": [{"path": "cart.ts", "line": 3}]}},
        )
        v = _lint(tmp_path, "pricing.ts", "export function editPrice() {}\n")
        assert all(x.rule != "removed-export-breaks-importers" for x in v)

    def test_export_star_suppresses_break(self, tmp_path):
        # A re-export barrel can re-export editPrice transitively; the name is not
        # statically visible, so suppress the break rather than false-positive.
        _write_reverse(
            tmp_path,
            {"pricing.ts": {"editPrice": [{"path": "cart.ts", "line": 3}]}},
        )
        v = _lint(tmp_path, "pricing.ts", "export * from './core';\n")
        assert v == []

    def test_ignore_directive_suppresses_break(self, tmp_path):
        _write_reverse(
            tmp_path,
            {"pricing.ts": {"editPrice": [{"path": "cart.ts", "line": 3}]}},
        )
        content = (
            "// chameleon-ignore removed-export-breaks-importers\nexport const setPrice = 1;\n"
        )
        v = _lint(tmp_path, "pricing.ts", content)
        assert all(x.rule != "removed-export-breaks-importers" for x in v)
