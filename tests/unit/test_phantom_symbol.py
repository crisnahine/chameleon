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
    (cham / "COMMITTED").write_text("committed-at=1\npid=1\n", encoding="utf-8")
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
        v = _lint(
            tmp_path,
            "app.ts",
            "import { getUser, ghost1, save, ghost2 } from './api';\n",
        )
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

    def test_alias_import_missing_export_is_flagged(self, tmp_path):
        # A hallucinated named binding imported via a tsconfig path alias must be
        # flagged the same as a relative import. The alias branch used to skip
        # the symbol check entirely, so this was silently dead for the dominant
        # import style in many repos.
        (tmp_path / "tsconfig.json").write_text(
            '{"compilerOptions":{"baseUrl":".","paths":{"~/*":["src/*"]}}}',
            encoding="utf-8",
        )
        _write(tmp_path / "src" / "utils" / "env.ts")
        _index(tmp_path, {"src/utils/env.ts": {"names": ["getEnv"], "open": False}})
        v = lint_phantom_imports(
            "import { getEnv, ghost } from '~/utils/env';\n",
            file_path=str(tmp_path / "src" / "app.ts"),
            repo_root=str(tmp_path),
            language="typescript",
            rules={},
        )
        assert [f.actual for f in v] == ["ghost"]

    def test_alias_import_present_export_is_clean(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text(
            '{"compilerOptions":{"baseUrl":".","paths":{"~/*":["src/*"]}}}',
            encoding="utf-8",
        )
        _write(tmp_path / "src" / "utils" / "env.ts")
        _index(tmp_path, {"src/utils/env.ts": {"names": ["getEnv"], "open": False}})
        v = lint_phantom_imports(
            "import { getEnv } from '~/utils/env';\n",
            file_path=str(tmp_path / "src" / "app.ts"),
            repo_root=str(tmp_path),
            language="typescript",
            rules={},
        )
        assert v == []

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


class TestPythonAbsolutePhantomSymbol:
    """Absolute first-party `from pkg.mod import x`: the symbol check must fire
    when the spec resolves to an indexed in-repo module whose closed export set
    lacks the name — repos whose idiom is absolute imports (Flask/Django) got
    no symbol check at all when only relative forms were scanned. The module
    itself is never flagged: an unresolvable spec may be stdlib or a dependency."""

    def _plint(self, repo, editing_rel, content):
        return lint_phantom_imports(
            content,
            file_path=str(repo / editing_rel),
            repo_root=str(repo),
            language="python",
            rules={},
        )

    def test_absolute_missing_symbol_flagged(self, tmp_path):
        _write(tmp_path / "flaskbb" / "__init__.py", "")
        _write(tmp_path / "flaskbb" / "utils" / "__init__.py", "")
        _write(
            tmp_path / "flaskbb" / "utils" / "helpers.py",
            "def real_helper():\n    pass\n",
        )
        _index(
            tmp_path,
            {"flaskbb/utils/helpers.py": {"names": ["real_helper"], "open": False}},
        )
        v = self._plint(
            tmp_path,
            "flaskbb/forum/views.py",
            "from flaskbb.utils.helpers import totally_fake\n",
        )
        assert [x.actual for x in v if x.rule == "phantom-symbol"] == ["totally_fake"]

    def test_absolute_present_symbol_clean(self, tmp_path):
        _write(tmp_path / "flaskbb" / "__init__.py", "")
        _write(tmp_path / "flaskbb" / "utils" / "__init__.py", "")
        _write(
            tmp_path / "flaskbb" / "utils" / "helpers.py",
            "def real_helper():\n    pass\n",
        )
        _index(
            tmp_path,
            {"flaskbb/utils/helpers.py": {"names": ["real_helper"], "open": False}},
        )
        v = self._plint(
            tmp_path,
            "flaskbb/forum/views.py",
            "from flaskbb.utils.helpers import real_helper\n",
        )
        assert v == []

    def test_unresolvable_absolute_module_silent(self, tmp_path):
        # `from django.db import models` with no in-repo django/: could be a
        # dependency; both the module and its symbols stay unflagged.
        _write(tmp_path / "app.py", "")
        _index(tmp_path, {"app.py": {"names": [], "open": False}})
        v = self._plint(tmp_path, "views.py", "from django.db import made_up_models\n")
        assert v == []

    def test_absolute_open_export_set_silent(self, tmp_path):
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(tmp_path / "pkg" / "lazy.py", "def __getattr__(n):\n    return n\n")
        _index(tmp_path, {"pkg/lazy.py": {"names": [], "open": True}})
        v = self._plint(tmp_path, "views.py", "from pkg.lazy import anything\n")
        assert v == []

    def test_submodule_import_from_package_clean(self, tmp_path):
        # `from flaskbb.utils import helpers` binds a SUBMODULE; the dump lists
        # sibling submodules in the package __init__'s export set, so this is
        # clean, not phantom.
        _write(tmp_path / "flaskbb" / "__init__.py", "")
        _write(tmp_path / "flaskbb" / "utils" / "__init__.py", "")
        _write(
            tmp_path / "flaskbb" / "utils" / "helpers.py",
            "def real_helper():\n    pass\n",
        )
        _index(
            tmp_path,
            {"flaskbb/utils/__init__.py": {"names": ["helpers"], "open": False}},
        )
        v = self._plint(tmp_path, "views.py", "from flaskbb.utils import helpers\n")
        assert v == []

    def test_namespace_subpackage_import_clean(self, tmp_path):
        # PEP 420: `from pkg import sub` where sub/ has NO __init__.py is a
        # real import, but a directory without __init__ is unenumerable at
        # dump time so it is absent from pkg/__init__'s closed export set.
        # Reality on disk beats the index: no flag.
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(tmp_path / "pkg" / "sub" / "mod.py", "def f():\n    pass\n")
        _index(tmp_path, {"pkg/__init__.py": {"names": [], "open": False}})
        v = self._plint(tmp_path, "views.py", "from pkg import sub\n")
        assert v == []

    def test_stale_index_missing_submodule_listing_clean(self, tmp_path):
        # An exports index built by an older engine may lack submodule names
        # in package __init__ entries; the on-disk submodule file rescues it.
        _write(tmp_path / "pkg" / "__init__.py", "")
        _write(tmp_path / "pkg" / "helpers.py", "def h():\n    pass\n")
        _index(tmp_path, {"pkg/__init__.py": {"names": [], "open": False}})
        v = self._plint(tmp_path, "views.py", "from pkg import helpers\n")
        assert v == []
