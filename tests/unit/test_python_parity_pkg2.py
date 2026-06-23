"""PKG-2: Python conventions derivation (conventions.py).

doc_coverage (docstrings), error_handling (try:), key_exports (top-level public
names), test-pairing (pytest layouts), and the class-contract method-kind set
(staticmethod/classmethod).
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.conventions import (
    _candidate_test_paths,
    _is_test_path,
    compute_doc_coverage_from_content,
    extract_class_contract_conventions,
    extract_error_handling_conventions,
    extract_key_exports,
)
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: str, *, extras: dict | None = None) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras=extras or {},
    )


# --------------------------------------------------------------------------- #
# doc_coverage — Python docstrings are the line AFTER the def/class, not before
# --------------------------------------------------------------------------- #


def test_doc_coverage_counts_docstrings():
    src = (
        "def documented(x):\n"
        '    """Does a thing."""\n'
        "    return x\n"
        "\n\n"
        "def undocumented(y):\n"
        "    return y\n"
        "\n\n"
        "class Widget:\n"
        '    """A widget."""\n'
        "    pass\n"
    )
    documented, public = compute_doc_coverage_from_content(src, language="python")
    assert public == 3  # two functions + one class
    assert documented == 2  # documented() + Widget


def test_doc_coverage_skips_private():
    src = 'def _helper():\n    """doc"""\n    pass\n'
    documented, public = compute_doc_coverage_from_content(src, language="python")
    assert public == 0  # underscore-prefixed is not public


# --------------------------------------------------------------------------- #
# error_handling — try: shape
# --------------------------------------------------------------------------- #


def test_error_handling_try(tmp_path):
    files = []
    for i in range(10):
        p = tmp_path / f"v{i}.py"
        p.write_text("def f():\n    try:\n        go()\n    except Exception:\n        pass\n")
        files.append(_pf(str(p)))
    out = extract_error_handling_conventions(files, language="python")
    assert out.get("try_catch", 0) >= 0.6
    assert out["sample_size"] == 10


# --------------------------------------------------------------------------- #
# key_exports — top-level public names from named_export_names (PKG-1)
# --------------------------------------------------------------------------- #


def test_key_exports_public_names():
    files = [
        _pf(
            f"app/m{i}.py",
            extras={"named_export_names": ["User", "Widget", "_private", "CONST"]},
        )
        for i in range(10)
    ]
    out = extract_key_exports(files, language="python")
    assert "User" in out and "Widget" in out and "CONST" in out
    assert "_private" not in out  # underscore-prefixed excluded


# --------------------------------------------------------------------------- #
# test-pairing — pytest layouts (test_*.py / *_test.py / conftest.py)
# --------------------------------------------------------------------------- #


def test_is_test_path_python():
    assert _is_test_path("app/test_views.py", language="python") is True
    assert _is_test_path("app/views_test.py", language="python") is True
    assert _is_test_path("app/conftest.py", language="python") is True
    assert _is_test_path("app/views.py", language="python") is False


def test_candidate_test_paths_python():
    cands = dict(
        (label, path)
        for label, path in _candidate_test_paths(
            "readthedocs/projects/models.py", language="python"
        )
    )
    paths = set(cands.values())
    # co-located test_models.py and a mirrored tests/ candidate
    assert any(p.endswith("readthedocs/projects/test_models.py") for p in paths)
    assert any("tests/" in p and p.endswith("test_models.py") for p in paths)


# --------------------------------------------------------------------------- #
# class-contract — staticmethod/classmethod count toward required_methods
# --------------------------------------------------------------------------- #


def test_class_contract_accepts_staticmethod():
    def f(i):
        cls = f"Svc{i}"
        return _pf(
            f"app/s{i}.py",
            extras={
                "class_shapes": [{"name": cls, "decorators": ["dataclass"], "bases": ["Base"]}],
                "callable_signatures": [
                    {"name": "build", "kind": "staticmethod", "enclosing_class": cls},
                ],
            },
        )

    out = extract_class_contract_conventions([f(i) for i in range(10)], language="python")
    assert "build" in out.get("required_methods", [])


# --------------------------------------------------------------------------- #
# layering — _resolve_python resolves relative + absolute intra-repo imports
# --------------------------------------------------------------------------- #


def test_resolve_python_relative_and_absolute(tmp_path):
    from chameleon_mcp.bootstrap.import_graph import _resolve_python

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "models.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "views.py").write_text("y = 2\n")
    views = tmp_path / "pkg" / "views.py"

    # relative: from .models import X  (spec ".models")
    assert _resolve_python(".models", views, tmp_path) == (tmp_path / "pkg" / "models.py")
    # absolute repo-rooted: from pkg.models import X
    assert _resolve_python("pkg.models", views, tmp_path) == (tmp_path / "pkg" / "models.py")
    # external package -> None
    assert _resolve_python("django.db", views, tmp_path) is None
    # package __init__ form
    assert _resolve_python("pkg", views, tmp_path) == (tmp_path / "pkg" / "__init__.py")


# --------------------------------------------------------------------------- #
# inheritance derivation (dominant_base / known_bases from class_shapes) + lint
# --------------------------------------------------------------------------- #


def _model(i, bases):
    return _pf(f"app/models/m{i}.py", extras={"class_shapes": [{"name": f"M{i}", "bases": bases}]})


def test_inheritance_derivation_python_dominant_base():
    from chameleon_mcp.conventions import extract_inheritance_conventions

    # 9 of 10 model files inherit models.Model -> dominant base.
    files = [_model(i, ["models.Model"]) for i in range(9)] + [_model(9, ["object"])]
    out = extract_inheritance_conventions(files, language="python")
    assert out.get("dominant_base") == "models.Model"
    assert out.get("frequency", 0) >= 0.6


def test_inheritance_derivation_python_known_bases_includes_mixins():
    from chameleon_mcp.conventions import extract_inheritance_conventions

    # APIView dominant; LoginRequiredMixin recurs (>=2) so it's an accepted base.
    files = (
        [_model(i, ["LoginRequiredMixin", "APIView"]) for i in range(3)]
        + [_model(i, ["APIView"]) for i in range(3, 9)]
        + [_model(9, ["APIView"])]
    )
    out = extract_inheritance_conventions(files, language="python")
    assert out.get("dominant_base") == "APIView"
    assert "LoginRequiredMixin" in out.get("known_bases", [])


def test_inheritance_derivation_skips_object_only():
    from chameleon_mcp.conventions import extract_inheritance_conventions

    # All plain classes -> nothing dominant (object is filtered).
    files = [_model(i, ["object"]) for i in range(10)]
    out = extract_inheritance_conventions(files, language="python")
    assert out == {}


def test_inheritance_lint_flags_wrong_base():
    from chameleon_mcp.lint_engine import lint_conventions

    conv = {"inheritance": {"dominant_base": "models.Model", "frequency": 0.9}}
    v = lint_conventions(
        "class Widget(SomethingElse):\n    pass\n", conv, language="python", archetype_name="model"
    )
    assert any(
        x.rule == "inheritance-convention-violation" and x.expected == "models.Model" for x in v
    )


def test_inheritance_lint_clean_for_known_base():
    from chameleon_mcp.lint_engine import lint_conventions

    conv = {"inheritance": {"dominant_base": "models.Model", "frequency": 0.9}}
    v = lint_conventions(
        "class Widget(models.Model):\n    pass\n", conv, language="python", archetype_name="model"
    )
    assert not any(x.rule == "inheritance-convention-violation" for x in v)


def test_inheritance_lint_ignores_plain_class():
    from chameleon_mcp.lint_engine import lint_conventions

    # A bare `class Foo:` is valid Python, not a missed inheritance.
    conv = {"inheritance": {"dominant_base": "models.Model", "frequency": 0.9}}
    v = lint_conventions("class Helper:\n    pass\n", conv, language="python")
    assert not any(x.rule == "inheritance-convention-violation" for x in v)


def test_inheritance_lint_not_fooled_by_docstring():
    from chameleon_mcp.lint_engine import lint_conventions

    conv = {"inheritance": {"dominant_base": "models.Model", "frequency": 0.9}}
    content = '"""\nclass Widget(SomethingElse):\n    pass\n"""\nx = 1\n'
    v = lint_conventions(content, conv, language="python")
    assert not any(x.rule == "inheritance-convention-violation" for x in v)
