"""Unit tests for the phantom-symbol half of lint_phantom_imports.

A named import of a binding the resolved in-repo module does not export is
flagged phantom-symbol. The check is purely additive over the path check: it
fires only when a committed exports_index.json exists, the target resolves and is
indexed, and the indexed export set is authoritative (not open).
"""

import json
from pathlib import Path

from chameleon_mcp.phantom_imports import lint_phantom_imports
from chameleon_mcp.symbol_index import EXPORTS_INDEX_FILENAME, SCHEMA_VERSION


def _write(p: Path, text: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _index(repo: Path, files: dict) -> None:
    cham = repo / ".chameleon"
    cham.mkdir(parents=True, exist_ok=True)
    (cham / EXPORTS_INDEX_FILENAME).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "files": files}), encoding="utf-8"
    )


def _lint(repo: Path, editing_name: str, content: str):
    return lint_phantom_imports(
        content,
        file_path=str(repo / editing_name),
        repo_root=str(repo),
        language="typescript",
        rules={},
    )


class TestPhantomSymbol:
    def test_missing_export_is_flagged(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { fetchUser } from './api';\n")
        assert len(v) == 1
        assert v[0].rule == "phantom-symbol"
        assert v[0].actual == "fetchUser"
        assert v[0].severity == "warning"
        assert "not exported" in v[0].message

    def test_present_export_is_clean(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { getUser } from './api';\n")
        assert v == []

    def test_alias_checks_imported_name_not_local(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        # `getUser as gu` is clean (getUser is exported); `nope as x` is phantom.
        clean = _lint(tmp_path, "app.ts", "import { getUser as gu } from './api';\n")
        assert clean == []
        flagged = _lint(tmp_path, "app.ts", "import { nope as x } from './api';\n")
        assert [f.actual for f in flagged] == ["nope"]

    def test_open_export_set_is_skipped(self, tmp_path):
        # A barrel (export * from) has a non-authoritative set; never flag.
        _write(tmp_path / "barrel.ts")
        _index(tmp_path, {"barrel.ts": {"names": [], "open": True}})
        v = _lint(tmp_path, "app.ts", "import { anything } from './barrel';\n")
        assert v == []

    def test_target_not_in_index_is_skipped(self, tmp_path):
        # File resolves on disk but was not indexed (edited this turn / generated).
        _write(tmp_path / "fresh.ts")
        _index(tmp_path, {"other.ts": {"names": ["x"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { whatever } from './fresh';\n")
        assert v == []

    def test_no_index_no_symbol_check(self, tmp_path):
        # Without a committed index the check cannot run; path check still works.
        _write(tmp_path / "api.ts")
        v = _lint(tmp_path, "app.ts", "import { fetchUser } from './api';\n")
        assert v == []

    def test_default_import_skipped(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import Thing from './api';\n")
        assert v == []

    def test_namespace_import_skipped(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import * as api from './api';\n")
        assert v == []

    def test_type_only_import_skipped(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import type { Phantom } from './api';\n")
        assert v == []

    def test_inline_type_specifier_skipped_value_checked(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        # `type Phantom` is a type position (skip); `nope` is a value (flag).
        v = _lint(tmp_path, "app.ts", "import { type Phantom, nope } from './api';\n")
        assert [f.actual for f in v] == ["nope"]

    def test_default_as_specifier_skipped(self, tmp_path):
        # `{ default as Foo }` targets the default export, which is not indexed.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { default as Foo } from './api';\n")
        assert v == []

    def test_default_plus_named_checks_only_named(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import Thing, { ghost } from './api';\n")
        assert [f.actual for f in v] == ["ghost"]

    def test_re_export_from_not_treated_as_import(self, tmp_path):
        # `export { x } from` is a re-export surface, not a binding this file uses.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "export { ghost } from './api';\n")
        assert v == []

    def test_multiple_specifiers_mixed(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser", "save"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { getUser, ghost1, save, ghost2 } from './api';\n")
        assert sorted(f.actual for f in v) == ["ghost1", "ghost2"]

    def test_second_import_from_same_module_is_symbol_checked(self, tmp_path):
        # Two import statements from one module carry different bindings; the
        # path-resolution dedup must not silence the symbol check on the second.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        content = "import { getUser } from './api';\nimport { ghost } from './api';\n"
        v = _lint(tmp_path, "app.ts", content)
        assert [f.actual for f in v] == ["ghost"]

    def test_path_typo_still_phantom_import_not_symbol(self, tmp_path):
        # A bad path is phantom-import; the symbol check never runs on it.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { getUser } from './apii';\n")
        assert len(v) == 1
        assert v[0].rule == "phantom-import"

    def test_ignore_phantom_symbol_suppresses_only_symbol(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        content = (
            "// chameleon-ignore phantom-symbol\n"
            "import { ghost } from './api';\n"
            "import { y } from './missing-path';\n"
        )
        v = _lint(tmp_path, "app.ts", content)
        # phantom-symbol suppressed; the path-resolution phantom-import survives.
        assert [x.rule for x in v] == ["phantom-import"]

    def test_ignore_phantom_import_suppresses_everything(self, tmp_path):
        # The broad phantom-import ignore short-circuits the whole TS branch.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        content = "// chameleon-ignore phantom-import\nimport { ghost } from './api';\n"
        v = _lint(tmp_path, "app.ts", content)
        assert v == []

    def test_js_specifier_resolves_to_ts_for_symbol_check(self, tmp_path):
        # NodeNext: import from './api.js' resolves to api.ts on disk.
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        v = _lint(tmp_path, "app.ts", "import { ghost } from './api.js';\n")
        assert [f.actual for f in v] == ["ghost"]

    def test_index_file_target_symbol_check(self, tmp_path):
        # Directory import resolves to widgets/index.ts and is symbol-checked.
        _write(tmp_path / "widgets" / "index.ts")
        _index(tmp_path, {"widgets/index.ts": {"names": ["Button"], "open": False}})
        clean = _lint(tmp_path, "app.ts", "import { Button } from './widgets';\n")
        assert clean == []
        flagged = _lint(tmp_path, "app.ts", "import { Ghost } from './widgets';\n")
        assert [f.actual for f in flagged] == ["Ghost"]

    def test_import_inside_string_literal_not_checked(self, tmp_path):
        _write(tmp_path / "api.ts")
        _index(tmp_path, {"api.ts": {"names": ["getUser"], "open": False}})
        content = "const code = \"import { ghost } from './api'\";\n"
        v = _lint(tmp_path, "app.ts", content)
        assert v == []
