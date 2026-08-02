"""Python monorepos get cross-workspace edges, not just TypeScript ones.

`build_reverse_index` runs per workspace, so a file in package B importing from
package A is absent from A's own index and a removed export goes unseen. The
coordinator JOIN closed that for TypeScript; Python was a documented gap --
candidates were captured (python is in `REVERSE_INDEXED_LANGUAGES`) but nothing
could resolve them, because the package map was built only from `package.json`
`name` and the target probe only tried JS extensions. Every Python candidate
therefore fell out at `packages.get(pkg) is None` and no edge was ever emitted.

The map is keyed on the IMPORT name, not the distribution name. `pyproject.toml`
routinely spells a package `my-lib` while the import is `my_lib`, so keying on
the distribution name would resolve almost nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.symbol_index import (
    build_cross_reverse_index,
    python_package_dirs,
)


def _mono(tmp_path: Path) -> Path:
    """A two-package Python monorepo: `libs/core` (flat) and `apps/api` (src/)."""
    core = tmp_path / "libs" / "core" / "core_lib"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")

    api = tmp_path / "apps" / "api" / "src" / "api_svc"
    api.mkdir(parents=True)
    (api / "__init__.py").write_text("", encoding="utf-8")
    (api / "handlers.py").write_text(
        "from core_lib.models import User\n\n\ndef handle():\n    return User()\n",
        encoding="utf-8",
    )
    return tmp_path


def test_python_package_dirs_finds_flat_and_src_layouts(tmp_path: Path):
    mono = _mono(tmp_path)
    assert python_package_dirs(mono / "libs" / "core") == {"core_lib": "core_lib"}
    # The PyPA src/ layout is where most packages live, so it must resolve too.
    assert python_package_dirs(mono / "apps" / "api") == {"api_svc": "src/api_svc"}


def test_python_package_dirs_ignores_non_packages(tmp_path: Path):
    """A directory without `__init__` is not an importable package."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "not-an-identifier").mkdir()
    (tmp_path / "not-an-identifier" / "__init__.py").write_text("", encoding="utf-8")
    assert python_package_dirs(tmp_path) == {}


def test_a_python_cross_package_import_resolves_to_a_real_edge(tmp_path: Path):
    """The end-to-end JOIN: api_svc imports core_lib.models.User across packages."""
    mono = _mono(tmp_path)
    candidates = [
        {
            "importer": "apps/api/src/api_svc/handlers.py",
            "name": "User",
            "module": "core_lib.models",
            "line": 1,
            "language": "python",
        }
    ]
    python_packages = {"core_lib": "libs/core/core_lib", "api_svc": "apps/api/src/api_svc"}
    payload = build_cross_reverse_index(
        candidates,
        {},
        mono,
        {"libs/core/core_lib/models.py": {"User"}},
        python_packages,
    )

    target = payload["targets"].get("libs/core/core_lib/models.py")
    assert target is not None, f"no cross-workspace edge was produced: {payload['targets']}"
    assert target["User"] == [{"path": "apps/api/src/api_svc/handlers.py", "line": 1}]


def test_the_name_check_stays_fail_closed(tmp_path: Path):
    """An imported name the target does not actually export yields NO edge.

    This is what keeps a third-party import (`from requests.models import
    Response`) from inventing an edge onto a same-named local module.
    """
    mono = _mono(tmp_path)
    candidates = [
        {
            "importer": "apps/api/src/api_svc/handlers.py",
            "name": "NotExported",
            "module": "core_lib.models",
            "line": 1,
            "language": "python",
        }
    ]
    payload = build_cross_reverse_index(
        candidates,
        {},
        mono,
        {"libs/core/core_lib/models.py": {"User"}},
        {"core_lib": "libs/core/core_lib"},
    )
    assert payload["targets"] == {}


def test_an_external_package_yields_no_edge(tmp_path: Path):
    """A head that names no sibling package is a third-party dependency."""
    mono = _mono(tmp_path)
    candidates = [
        {
            "importer": "apps/api/src/api_svc/handlers.py",
            "name": "BaseModel",
            "module": "pydantic",
            "line": 1,
            "language": "python",
        }
    ]
    payload = build_cross_reverse_index(
        candidates, {}, mono, lambda _k: {"BaseModel"}, {"core_lib": "libs/core/core_lib"}
    )
    assert payload["targets"] == {}


def test_a_package_import_resolves_through_its_dunder_init(tmp_path: Path):
    """`from core_lib import X` targets the package's own `__init__.py`."""
    mono = _mono(tmp_path)
    (mono / "libs/core/core_lib/__init__.py").write_text("X = 1\n", encoding="utf-8")
    payload = build_cross_reverse_index(
        [
            {
                "importer": "apps/api/src/api_svc/handlers.py",
                "name": "X",
                "module": "core_lib",
                "line": 3,
                "language": "python",
            }
        ],
        {},
        mono,
        {"libs/core/core_lib/__init__.py": {"X"}},
        {"core_lib": "libs/core/core_lib"},
    )
    assert "libs/core/core_lib/__init__.py" in payload["targets"]


def test_a_typescript_candidate_is_untouched_by_the_python_arm(tmp_path: Path):
    """The two arms must not interfere: TS still resolves by its own rules.

    A candidate carrying no `language` key (an index built before the field
    existed) has to keep resolving as TypeScript, which is what the default in
    the dispatch guarantees.
    """
    pkg = tmp_path / "packages" / "ui"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text('{"name": "@acme/ui", "main": "index.ts"}', encoding="utf-8")
    (pkg / "index.ts").write_text("export const Button = 1;\n", encoding="utf-8")
    app = tmp_path / "packages" / "app"
    app.mkdir(parents=True)
    (app / "main.ts").write_text("import { Button } from '@acme/ui';\n", encoding="utf-8")

    for candidate in (
        {"importer": "packages/app/main.ts", "name": "Button", "module": "@acme/ui", "line": 1},
        {
            "importer": "packages/app/main.ts",
            "name": "Button",
            "module": "@acme/ui",
            "line": 1,
            "language": "typescript",
        },
    ):
        payload = build_cross_reverse_index(
            [candidate],
            {"@acme/ui": "packages/ui"},
            tmp_path,
            {"packages/ui/index.ts": {"Button"}},
        )
        assert "packages/ui/index.ts" in payload["targets"], candidate


@pytest.mark.parametrize("module", ["..core_lib.models", "...escape"])
def test_a_relative_import_that_escapes_the_workspace_is_handled(tmp_path: Path, module: str):
    """A relative specifier walking out of its own package must not raise, and
    must never resolve outside the monorepo root."""
    mono = _mono(tmp_path)
    payload = build_cross_reverse_index(
        [
            {
                "importer": "apps/api/src/api_svc/handlers.py",
                "name": "User",
                "module": module,
                "line": 1,
                "language": "python",
            }
        ],
        {},
        mono,
        lambda _k: {"User"},
        {},
    )
    for key in payload["targets"]:
        assert not key.startswith(".."), key
