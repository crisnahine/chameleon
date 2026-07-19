"""Tests for scripts/libcst_dump.py — the Python (libcst) AST dump script.

Mirrors the prism_dump.rb / ts_dump.mjs contract: a long-lived subprocess that
reads absolute file paths on stdin (one per line) and emits one NDJSON
ParsedFile record per file on stdout. These tests pin the exact emitted schema
so a downstream PythonExtractor (and the Django/Flask/FastAPI framework priors
built on top) can rely on it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_LIBCST_DUMP = Path(__file__).resolve().parents[2] / "plugin" / "scripts" / "libcst_dump.py"


def _have_libcst() -> bool:
    try:
        import libcst  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _have_libcst(), reason="libcst unavailable")


def _dump(path: Path) -> dict:
    """Run the dump script over one file path, return the parsed record."""
    out = subprocess.run(
        [sys.executable, str(_LIBCST_DUMP)],
        input=str(path) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _write(tmp_path: Path, name: str, src: str) -> Path:
    f = tmp_path / name
    f.write_text(src, encoding="utf-8")
    return f


# --------------------------------------------------------------------------- #
# Core normalized record shape (the cluster-signature inputs)
# --------------------------------------------------------------------------- #

CORE_SRC = """import os
from django.db import models
from flask import Blueprint, request

CONST = 1


def view(request, *args, **kw):
    return None


class Foo(models.Model):
    pass
"""


def test_top_level_node_kinds_unwraps_simple_statements(tmp_path):
    # libcst wraps small statements in SimpleStatementLine; the dump must unwrap
    # them so the kinds are meaningful (Import/ImportFrom/Assign), while compound
    # statements (FunctionDef/ClassDef) emit their own kind directly.
    rec = _dump(_write(tmp_path, "core.py", CORE_SRC))
    assert rec["top_level_node_kinds"] == [
        "Import",
        "ImportFrom",
        "ImportFrom",
        "Assign",
        "FunctionDef",
        "ClassDef",
    ]


def test_has_jsx_always_false_and_diagnostics_zero(tmp_path):
    rec = _dump(_write(tmp_path, "core.py", CORE_SRC))
    assert rec["has_jsx"] is False
    assert rec["parse_diagnostics_count"] == 0
    assert rec["content_first_200_bytes"].startswith("import os")


def test_named_export_count_counts_top_level_defs_and_classes(tmp_path):
    rec = _dump(_write(tmp_path, "core.py", CORE_SRC))
    # one top-level def (view) + one top-level class (Foo)
    assert rec["named_export_count"] == 2


def test_default_export_kind_none_when_mixed_top_level(tmp_path):
    rec = _dump(_write(tmp_path, "core.py", CORE_SRC))
    # both a top-level function and a top-level class -> ambiguous -> None
    assert rec["default_export_kind"] is None


def test_default_export_kind_sole_class(tmp_path):
    rec = _dump(_write(tmp_path, "m.py", "class Only(Base):\n    pass\n"))
    assert rec["default_export_kind"] == "ClassDef"


def test_default_export_kind_sole_function(tmp_path):
    rec = _dump(_write(tmp_path, "m.py", "def only(a):\n    return a\n"))
    assert rec["default_export_kind"] == "FunctionDef"


# --------------------------------------------------------------------------- #
# import_specifiers — [module, kind]; framework module roots must survive
# --------------------------------------------------------------------------- #


def test_import_specifiers_kinds_and_modules(tmp_path):
    src = (
        "import os\n"
        "import a.b.c as abc\n"
        "from django.db import models\n"
        "from flask import Blueprint, request\n"
        "from . import sibling\n"
        "from .models import Thing\n"
        "from fastapi import *\n"
    )
    rec = _dump(_write(tmp_path, "imp.py", src))
    specs = {(m, k) for m, k in rec["import_specifiers"]}
    assert ("os", "namespace") in specs
    assert ("a.b.c", "namespace") in specs
    assert ("django.db", "named") in specs
    assert ("flask", "named") in specs
    assert (".", "named") in specs
    assert (".models", "named") in specs
    assert ("fastapi", "namespace") in specs  # star import


# --------------------------------------------------------------------------- #
# function_scopes — body shape (line_span, max_depth, branch_count, param_count)
# --------------------------------------------------------------------------- #

SCOPE_SRC = """def handler(request, *args, **kw):
    if request:
        for i in range(3):
            pass
    return None
"""


def test_function_scopes_body_shape(tmp_path):
    rec = _dump(_write(tmp_path, "h.py", SCOPE_SRC))
    scopes = rec["function_scopes"]
    assert len(scopes) == 1
    s = scopes[0]
    assert s["param_count"] == 3  # request, *args, **kw
    assert s["branch_count"] == 2  # if + for
    assert s["max_depth"] == 2  # if -> for
    assert s["line_span"] == 5


def test_nested_function_is_its_own_scope(tmp_path):
    src = "def outer():\n    def inner(a, b):\n        return a\n    return inner\n"
    rec = _dump(_write(tmp_path, "n.py", src))
    spans = sorted(s["param_count"] for s in rec["function_scopes"])
    assert spans == [0, 2]  # outer() has 0 params, inner(a, b) has 2


# --------------------------------------------------------------------------- #
# callable_signatures — name/kind/params/enclosing class/base/decorators
# --------------------------------------------------------------------------- #

SIG_SRC = """class Outer:
    class Inner(Base, Mixin):
        @staticmethod
        def helper(a, b=2, *rest, kw1, **extra):
            return a

        @classmethod
        def make(cls):
            return cls()

        def method(self):
            return 1


def plain(x):
    return x
"""


def test_callable_signature_enclosing_class_path_and_base(tmp_path):
    rec = _dump(_write(tmp_path, "s.py", SIG_SRC))
    sigs = {s["name"]: s for s in rec["callable_signatures"]}

    helper = sigs["helper"]
    assert helper["enclosing_class"] == "Inner"
    assert helper["enclosing_class_path"] == "Outer.Inner"
    assert helper["base_class"] == "Base"
    assert helper["kind"] == "staticmethod"
    assert "staticmethod" in helper["decorators"]

    make = sigs["make"]
    assert make["kind"] == "classmethod"

    method = sigs["method"]
    assert method["kind"] == "method"

    plain = sigs["plain"]
    assert plain["enclosing_class"] is None
    assert plain["enclosing_class_path"] is None
    assert plain["base_class"] is None
    assert plain["kind"] == "function"


def test_callable_signature_param_shapes(tmp_path):
    rec = _dump(_write(tmp_path, "s.py", SIG_SRC))
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    params = sigs["helper"]["params"]
    by_name = {p["name"]: p for p in params}
    assert by_name["a"] == {"name": "a", "optional": False, "kind": "positional"}
    assert by_name["b"] == {"name": "b", "optional": True, "kind": "optional"}
    assert by_name["rest"]["kind"] == "rest"
    assert by_name["kw1"]["kind"] == "keyword"
    assert by_name["extra"]["kind"] == "keyword_rest"


# --------------------------------------------------------------------------- #
# decorators — the framework-discriminating signal (Flask/FastAPI routes)
# --------------------------------------------------------------------------- #


def test_route_decorators_captured_as_dotted_targets(tmp_path):
    src = (
        '@app.route("/x", methods=["GET"])\n'
        "def flask_view():\n"
        "    return 1\n"
        "\n\n"
        '@router.get("/items")\n'
        "def fastapi_view():\n"
        "    return 2\n"
    )
    rec = _dump(_write(tmp_path, "routes.py", src))
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    assert sigs["flask_view"]["decorators"] == ["app.route"]
    assert sigs["fastapi_view"]["decorators"] == ["router.get"]


# --------------------------------------------------------------------------- #
# class_shapes — bases + decorators per class (TS-style heritage signal)
# --------------------------------------------------------------------------- #


def test_class_shapes_capture_bases_and_decorators(tmp_path):
    src = "@register\nclass UserViewSet(viewsets.ModelViewSet, LoggingMixin):\n    pass\n"
    rec = _dump(_write(tmp_path, "v.py", src))
    shapes = {c["name"]: c for c in rec["class_shapes"]}
    assert shapes["UserViewSet"]["bases"] == ["viewsets.ModelViewSet", "LoggingMixin"]
    assert shapes["UserViewSet"]["decorators"] == ["register"]


# --------------------------------------------------------------------------- #
# call_sites — caller -> callee edges
# --------------------------------------------------------------------------- #

CALL_SRC = """class Service:
    def perform(self):
        helper()
        self.flush()
        other.compute()
        Thing()
"""


def test_call_sites_classification(tmp_path):
    rec = _dump(_write(tmp_path, "c.py", CALL_SRC))
    sites = {(s["name"], s["kind"], s.get("receiver"), s["caller"]) for s in rec["call_sites"]}
    assert ("helper", "bare", None, "perform") in sites
    assert ("flush", "self", "self", "perform") in sites
    assert ("compute", "member", "other", "perform") in sites
    assert ("Thing", "bare", None, "perform") in sites
    assert rec["call_sites_total"] == len(rec["call_sites"])
    assert rec["call_sites_truncated"] is False


# --------------------------------------------------------------------------- #
# Error records — must mirror the shared error vocabulary
# --------------------------------------------------------------------------- #


def test_parse_error_record(tmp_path):
    rec = _dump(_write(tmp_path, "bad.py", "def (:\n  pass\n"))
    assert rec["error"] == "parse_error"
    assert "path" in rec


def test_symlink_refused(tmp_path):
    target = _write(tmp_path, "real.py", "x = 1\n")
    link = tmp_path / "link.py"
    os.symlink(target, link)
    rec = _dump(link)
    assert rec["error"] == "symlink_refused"


def test_file_too_large(tmp_path):
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" + ("# pad\n" * 200_000), encoding="utf-8")
    rec = _dump(big)
    assert rec["error"] == "file_too_large"
    assert rec["size"] > 1_000_000


def test_async_def_opens_a_scope(tmp_path):
    # FastAPI route handlers are `async def`; they must be measured as functions.
    src = "async def endpoint(a, b):\n    return a\n"
    rec = _dump(_write(tmp_path, "a.py", src))
    assert len(rec["function_scopes"]) == 1
    assert rec["function_scopes"][0]["param_count"] == 2
    sigs = {s["name"]: s for s in rec["callable_signatures"]}
    assert sigs["endpoint"]["kind"] == "function"


# --------------------------------------------------------------------------- #
# PKG-1 foundation fields (cross-file unlock): import_symbols, namespace_imports,
# named_export_names/export_set_open, return_type, param type, class_shapes extends
# --------------------------------------------------------------------------- #


def test_import_symbols_named_imports(tmp_path):
    src = "from a.b import Foo, Bar as Baz\nfrom . import sibling\n"
    rec = _dump(_write(tmp_path, "imp.py", src))
    rows = {(r["name"], r["local"], r["module"]) for r in rec["import_symbols"]}
    assert ("Foo", "Foo", "a.b") in rows
    assert ("Bar", "Baz", "a.b") in rows  # `as` alias -> local differs from name
    assert ("sibling", "sibling", ".") in rows
    for r in rec["import_symbols"]:
        assert "line" in r


def test_namespace_imports(tmp_path):
    src = "import os\nimport a.b.c as abc\nimport pkg.mod\n"
    rec = _dump(_write(tmp_path, "ns.py", src))
    rows = {(r["alias"], r["module"]) for r in rec["namespace_imports"]}
    assert ("os", "os") in rows
    assert ("abc", "a.b.c") in rows  # asname binds the alias
    assert ("pkg", "pkg.mod") in rows  # plain import binds the top segment


def test_star_import_not_in_symbols(tmp_path):
    rec = _dump(_write(tmp_path, "s.py", "from mod import *\n"))
    assert rec["import_symbols"] == []
    assert rec["export_set_open"] is True  # star re-export opens the export set


def test_named_export_names(tmp_path):
    src = "import os\nCONST = 1\n\n\ndef helper():\n    pass\n\n\nclass Widget:\n    pass\n"
    rec = _dump(_write(tmp_path, "m.py", src))
    assert set(rec["named_export_names"]) == {"os", "CONST", "helper", "Widget"}
    assert rec["export_set_open"] is False


def test_return_type_and_param_type(tmp_path):
    src = "def f(a: int, b: str = 'x') -> bool:\n    return True\n"
    rec = _dump(_write(tmp_path, "t.py", src))
    sig = next(s for s in rec["callable_signatures"] if s["name"] == "f")
    assert sig["return_type"] == "bool"
    by_name = {p["name"]: p for p in sig["params"]}
    assert by_name["a"]["type"] == "int"
    assert by_name["b"]["type"] == "str"


def test_no_return_type_when_unannotated(tmp_path):
    rec = _dump(_write(tmp_path, "u.py", "def g(x):\n    return x\n"))
    sig = next(s for s in rec["callable_signatures"] if s["name"] == "g")
    assert "return_type" not in sig
    assert "type" not in sig["params"][0]


def test_class_shapes_extends_mirrors_first_base(tmp_path):
    rec = _dump(_write(tmp_path, "c.py", "class V(models.Model):\n    pass\n"))
    shape = next(c for c in rec["class_shapes"] if c["name"] == "V")
    assert shape["extends"] == "models.Model"
    assert shape["bases"] == ["models.Model"]


def test_class_shapes_extends_marks_dropped_bases(tmp_path):
    # A class with multiple bases must not silently lose the extras: `bases`
    # keeps the full list, and `extends` (the single-string summary) marks
    # how many more there are instead of rendering only the first.
    rec = _dump(_write(tmp_path, "c.py", "class V(models.Model, Mixin):\n    pass\n"))
    shape = next(c for c in rec["class_shapes"] if c["name"] == "V")
    assert shape["extends"] == "models.Model (+1 more)"
    assert shape["bases"] == ["models.Model", "Mixin"]


def test_class_shapes_capture_generic_base(tmp_path):
    # A subscripted generic base (BaseRepository[User], Generic[T],
    # ModelViewSet[Order]) is the standard typed-Python idiom. Dropping it
    # entirely leaves the class looking base-less, so inheritance and
    # class-contract derivation miss the shared base of a whole typed cohort.
    # The subscript is stripped to the base's own name.
    rec = _dump(_write(tmp_path, "c.py", "class R(BaseRepository[User]):\n    pass\n"))
    shape = next(c for c in rec["class_shapes"] if c["name"] == "R")
    assert shape["bases"] == ["BaseRepository"]
    assert shape["extends"] == "BaseRepository"


def test_class_shapes_capture_dotted_generic_base(tmp_path):
    # Same for a namespaced generic base (typing.Generic[T], mod.Base[X]).
    rec = _dump(_write(tmp_path, "c.py", "class S(mod.Base[X], Mixin):\n    pass\n"))
    shape = next(c for c in rec["class_shapes"] if c["name"] == "S")
    assert shape["bases"] == ["mod.Base", "Mixin"]


# --------------------------------------------------------------------------- #
# Regression (cloud review): _module_exports must see bindings inside top-level
# try/if blocks, and an __init__ must re-export sibling submodules.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _have_libcst(), reason="libcst not installed")
def test_module_exports_includes_try_block_bindings(tmp_path):
    src = (
        "try:\n"
        "    from typing import Self\n"
        "except ImportError:\n"
        "    from typing_extensions import Self\n\n"
        "DEFAULT = 1\n"
    )
    rec = _dump(_write(tmp_path, "compat.py", src))
    assert "Self" in rec["named_export_names"]
    assert rec["export_set_open"] is False


@pytest.mark.skipif(not _have_libcst(), reason="libcst not installed")
def test_module_exports_init_includes_sibling_submodules(tmp_path):
    pkg = tmp_path / "models"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (pkg / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (pkg / "post.py").write_text("class Post:\n    pass\n", encoding="utf-8")
    rec = _dump(pkg / "__init__.py")
    names = set(rec["named_export_names"])
    assert {"VERSION", "user", "post"} <= names
