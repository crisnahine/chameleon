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
