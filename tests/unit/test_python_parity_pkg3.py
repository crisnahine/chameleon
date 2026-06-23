"""PKG-3: Python cross-file intelligence.

Sub-step A: signature contract-diff routes .py through the PythonExtractor, so a
narrowed positional contract on a Python callable is detectable (the param shapes
carry the positional/keyword kinds the diff needs).
"""

from __future__ import annotations

from chameleon_mcp.signature_diff import _extractor_for_ext, parse_callables


def test_extractor_for_py():
    ext = _extractor_for_ext(".py")
    assert ext is not None and ext.language == "python"
    assert _extractor_for_ext(".pyi") is not None


def test_parse_callables_python(tmp_path):
    f = tmp_path / "svc.py"
    f.write_text(
        "def handle(a, b, c=1):\n    return a\n\n\ndef other(x):\n    return x\n", encoding="utf-8"
    )
    callables = parse_callables(tmp_path, str(f))
    assert "handle" in callables and "other" in callables
    # params carry the kind discrimination the contract diff needs
    handle = {p["name"]: p for p in callables["handle"]}
    assert handle["a"]["kind"] == "positional"
    assert handle["c"]["kind"] == "optional"


def test_parse_callables_drops_ambiguous_names(tmp_path):
    # Two same-named callables in one file are ambiguous -> dropped (fail-safe).
    f = tmp_path / "dup.py"
    f.write_text(
        "class A:\n    def run(self):\n        pass\n\n\nclass B:\n    def run(self, x):\n        pass\n",
        encoding="utf-8",
    )
    callables = parse_callables(tmp_path, str(f))
    assert "run" not in callables


# --------------------------------------------------------------------------- #
# Sub-step B: signature index carries Python param/return type text (unlocked
# by PKG-1 emitting return_type + param type; the build is language-agnostic).
# --------------------------------------------------------------------------- #


def test_signature_index_python_types(tmp_path):
    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.symbol_signatures import build_symbol_signatures

    f = tmp_path / "svc.py"
    f.write_text("def fetch(a: int, b: str = 'x') -> bool:\n    return True\n", encoding="utf-8")
    files = PythonExtractor().parse_repo(tmp_path, paths=[f]).files
    sigs = build_symbol_signatures(files, tmp_path)
    # find the fetch row regardless of the index's keying shape
    blob = repr(sigs)
    assert "bool" in blob and "int" in blob  # return + param types present


# --------------------------------------------------------------------------- #
# Sub-step C: phantom-import — a relative import resolving to no file on disk.
# --------------------------------------------------------------------------- #


def test_phantom_import_python(tmp_path):
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "real.py").write_text("x = 1\n", encoding="utf-8")
    editing = pkg / "views.py"

    # A relative import of a module that does not exist -> phantom-import.
    v = lint_phantom_imports(
        "from .nonexistent import thing\n",
        file_path=str(editing),
        repo_root=tmp_path,
        language="python",
    )
    assert any(x.rule == "phantom-import" for x in v)

    # A relative import that resolves on disk -> clean.
    v2 = lint_phantom_imports(
        "from .real import x\n",
        file_path=str(editing),
        repo_root=tmp_path,
        language="python",
    )
    assert not any(x.rule == "phantom-import" for x in v2)


def test_phantom_import_python_from_dot_import_not_flagged(tmp_path):
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # `from . import x` targets the package itself (present) -> not a phantom.
    v = lint_phantom_imports(
        "from . import anything\n",
        file_path=str(pkg / "views.py"),
        repo_root=tmp_path,
        language="python",
    )
    assert not any(x.rule == "phantom-import" for x in v)


def test_phantom_import_block_eligible_for_python():
    from chameleon_mcp.violation_class import BLOCK_RULE_LANGUAGES

    assert "python" in BLOCK_RULE_LANGUAGES["phantom-import"]


# --------------------------------------------------------------------------- #
# Sub-step D: exports + reverse index for Python (named_export_names /
# import_symbols, resolved through a Python-aware module resolver).
# --------------------------------------------------------------------------- #


def test_exports_and_reverse_index_python(tmp_path):
    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.symbol_index import build_exports_index, build_reverse_index

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(
        "class User:\n    pass\n\n\nclass Widget:\n    pass\n", encoding="utf-8"
    )
    (pkg / "views.py").write_text(
        "from .models import User\n\n\ndef show():\n    return User\n", encoding="utf-8"
    )
    files = PythonExtractor().parse_repo(tmp_path).files

    exports = build_exports_index(files, tmp_path)["files"]
    assert "User" in exports["pkg/models.py"]["names"]
    assert "Widget" in exports["pkg/models.py"]["names"]

    reverse = build_reverse_index(files, tmp_path, language="python")["targets"]
    # views.py imports User FROM pkg/models.py -> recorded under that target.
    importers = reverse["pkg/models.py"]["User"]
    assert any(row.get("path") == "pkg/views.py" for row in importers)


def test_resolve_python_index_key(tmp_path):
    from chameleon_mcp.symbol_index import make_module_resolver

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "models.py").write_text("x = 1\n", encoding="utf-8")
    resolve = make_module_resolver(tmp_path.resolve(), "python")
    # relative from a sibling file
    assert resolve(".models", (pkg / "views.py").resolve().parent) == "pkg/models.py"
    # absolute repo-rooted
    assert resolve("pkg.models", (pkg).resolve()) == "pkg/models.py"
    # external -> None
    assert resolve("django.db", pkg.resolve()) is None


# --------------------------------------------------------------------------- #
# Sub-step E: phantom-symbol — a named import absent from the target's exports.
# --------------------------------------------------------------------------- #


def test_phantom_symbol_python(tmp_path):
    import json

    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.phantom_imports import lint_phantom_imports
    from chameleon_mcp.symbol_index import build_exports_index

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    # Build + commit the exports index where load_exports_index reads it.
    files = PythonExtractor().parse_repo(tmp_path).files
    (tmp_path / ".chameleon").mkdir()
    (tmp_path / ".chameleon" / "exports_index.json").write_text(
        json.dumps(build_exports_index(files, tmp_path)), encoding="utf-8"
    )

    editing = pkg / "views.py"
    v = lint_phantom_imports(
        "from .models import User, Ghost\n",
        file_path=str(editing),
        repo_root=tmp_path,
        language="python",
    )
    rules = [(x.rule, x.actual) for x in v]
    # Ghost is not exported by models.py -> phantom-symbol; User is fine.
    assert any(r == "phantom-symbol" and "Ghost" in a for r, a in rules)
    assert not any(r == "phantom-symbol" and "User" in a for r, a in rules)


# --------------------------------------------------------------------------- #
# Sub-step F: cross-file-importers + removed-export (read the reverse index).
# --------------------------------------------------------------------------- #


def _py_repo_with_reverse_index(tmp_path):
    import json

    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.symbol_index import build_reverse_index

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (pkg / "views.py").write_text(
        "from .models import User\n\n\ndef show():\n    return User\n", encoding="utf-8"
    )
    files = PythonExtractor().parse_repo(tmp_path).files
    (tmp_path / ".chameleon").mkdir()
    (tmp_path / ".chameleon" / "reverse_index.json").write_text(
        json.dumps(build_reverse_index(files, tmp_path, language="python")), encoding="utf-8"
    )
    return pkg / "models.py"


def test_cross_file_importers_python(tmp_path):
    from chameleon_mcp.phantom_imports import lint_cross_file_imports

    models = _py_repo_with_reverse_index(tmp_path)
    # models.py still exports User -> blast-radius advisory naming its importer.
    v = lint_cross_file_imports(
        models.read_text(), file_path=str(models), repo_root=tmp_path, language="python"
    )
    assert any(x.rule == "cross-file-importers" and "User" in x.expected for x in v)


def test_removed_export_breaks_importers_python(tmp_path):
    from chameleon_mcp.phantom_imports import lint_cross_file_imports

    models = _py_repo_with_reverse_index(tmp_path)
    # Edited content no longer defines User, but views.py still imports it.
    v = lint_cross_file_imports(
        "class Account:\n    pass\n", file_path=str(models), repo_root=tmp_path, language="python"
    )
    assert any(x.rule == "removed-export-breaks-importers" and x.expected == "User" for x in v)


# --------------------------------------------------------------------------- #
# Sub-step G: calls index import grade + forward definition hydration (Python).
# --------------------------------------------------------------------------- #


def test_calls_index_import_grade_python(tmp_path):
    from chameleon_mcp.calls_index import build_calls_index
    from chameleon_mcp.extractors.python import PythonExtractor

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    # A bare call of a named import resolves to svc.py's closed export `run`.
    (pkg / "views.py").write_text(
        "from .svc import run\n\n\ndef handler():\n    return run()\n", encoding="utf-8"
    )
    files = PythonExtractor().parse_repo(tmp_path).files
    index = build_calls_index(files, tmp_path, language="python")

    entry = index["callees"]["pkg/svc.py"]["run"]
    rows = entry["callers"]
    assert any(r["path"] == "pkg/views.py" and r["grade"] == "import" for r in rows)


def test_calls_index_member_grade_python(tmp_path):
    from chameleon_mcp.calls_index import build_calls_index
    from chameleon_mcp.extractors.python import PythonExtractor

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    # `import pkg.util as u; u.helper()` -> member call against the namespace
    # import's closed export set.
    (pkg / "views.py").write_text(
        "import pkg.util as u\n\n\ndef handler():\n    return u.helper()\n", encoding="utf-8"
    )
    files = PythonExtractor().parse_repo(tmp_path).files
    index = build_calls_index(files, tmp_path, language="python")

    rows = index["callees"]["pkg/util.py"]["helper"]["callers"]
    assert any(r["path"] == "pkg/views.py" and r["grade"] == "import" for r in rows)


def test_calls_index_external_member_not_graded_python(tmp_path):
    # `requests.get()` -- an external namespace -- must not produce a phantom edge.
    from chameleon_mcp.calls_index import build_calls_index
    from chameleon_mcp.extractors.python import PythonExtractor

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "views.py").write_text(
        "import requests\n\n\ndef handler():\n    return requests.get('x')\n", encoding="utf-8"
    )
    files = PythonExtractor().parse_repo(tmp_path).files
    index = build_calls_index(files, tmp_path, language="python")
    # No in-repo callee for an external receiver.
    assert "get" not in {n for names in index["callees"].values() for n in names}


def test_hydrate_imported_definitions_python(tmp_path):
    import json

    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.symbol_signatures import (
        build_symbol_signatures,
        hydrate_imported_definitions,
    )

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "svc.py").write_text(
        "def fetch(a: int, b: str = 'x') -> bool:\n    return True\n", encoding="utf-8"
    )
    editing = pkg / "views.py"
    editing.write_text("from .svc import fetch\n", encoding="utf-8")
    files = PythonExtractor().parse_repo(tmp_path).files
    (tmp_path / ".chameleon").mkdir()
    (tmp_path / ".chameleon" / "symbol_signatures.json").write_text(
        json.dumps(build_symbol_signatures(files, tmp_path)), encoding="utf-8"
    )

    out = hydrate_imported_definitions(tmp_path, [str(editing)])
    blob = "\n".join(out)
    # The imported symbol's signature is rendered for the reviewer.
    assert "fetch(" in blob and "bool" in blob


# --------------------------------------------------------------------------- #
# Regression (cloud review): the live export read must match the dump on
# multi-line / parenthesized imports, try-block bindings, and __init__ siblings.
# --------------------------------------------------------------------------- #


def test_py_imported_names_parenthesized_single_line():
    from chameleon_mcp.phantom_imports import _py_imported_names

    assert _py_imported_names("(User, GhostName)") == ["User", "GhostName"]
    assert _py_imported_names("(User, Widget as W)") == ["User", "Widget"]
    # The bare multi-line opener still collapses to nothing (names are off-line).
    assert _py_imported_names("(") == []


def test_current_export_names_multiline_parenthesized():
    from chameleon_mcp.phantom_imports import _python_current_export_names

    content = "from .submod import (\n    PublicClass,\n    public_function,\n)\nDEFAULT = 1\n"
    names, is_open = _python_current_export_names(content)
    assert {"PublicClass", "public_function", "DEFAULT"} <= names
    assert is_open is False


def test_current_export_names_try_block_bindings():
    from chameleon_mcp.phantom_imports import _python_current_export_names

    content = (
        "try:\n    from typing import Self\nexcept ImportError:\n"
        "    from typing_extensions import Self\n\nTIMEOUT = 30\n"
    )
    names, is_open = _python_current_export_names(content)
    assert "Self" in names and "TIMEOUT" in names


def test_current_export_names_init_siblings(tmp_path):
    from chameleon_mcp.phantom_imports import _python_current_export_names

    pkg = tmp_path / "models"
    pkg.mkdir()
    (pkg / "user.py").write_text("class User:\n    pass\n", encoding="utf-8")
    names, _ = _python_current_export_names("VERSION = 1\n", pkg / "__init__.py")
    assert {"VERSION", "user"} <= names


def test_current_export_names_unparseable_opens_set():
    from chameleon_mcp.phantom_imports import _python_current_export_names

    # A mid-edit syntax error must not read as "exports nothing" (removed-export
    # FP storm); the set opens so the existence check is skipped.
    names, is_open = _python_current_export_names("def broken(:\n")
    assert is_open is True and names == frozenset()


def test_live_read_converges_with_dump(tmp_path):
    # The live read and the dump's _module_exports must agree on the same content,
    # or removed-export drifts. Exercises try-block + multi-line + plain bindings.
    from chameleon_mcp.extractors.python import PythonExtractor
    from chameleon_mcp.phantom_imports import _python_current_export_names

    content = (
        "try:\n    from a import X\nexcept ImportError:\n    from b import X\n\n"
        "from .m import (\n    Foo,\n    Bar,\n)\n\nCONST = 1\n\n\ndef helper():\n    pass\n"
    )
    f = tmp_path / "mod.py"
    f.write_text(content, encoding="utf-8")
    pf = PythonExtractor().parse_repo(tmp_path, paths=[f]).files[0]
    producer = set((getattr(pf, "extras", None) or {}).get("named_export_names") or [])
    live, _ = _python_current_export_names(content)
    assert producer == live
