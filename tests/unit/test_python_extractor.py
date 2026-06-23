"""Tests for the Python (libcst) extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.extractors._base import ParseResult
from chameleon_mcp.extractors.python import PythonExtractor, PythonUnavailableError


def _have_libcst() -> bool:
    import importlib.util

    return importlib.util.find_spec("libcst") is not None


pytestmark = pytest.mark.skipif(not _have_libcst(), reason="libcst unavailable")


# --------------------------------------------------------------------------- #
# can_handle
# --------------------------------------------------------------------------- #


def test_can_handle_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert PythonExtractor().can_handle(tmp_path) is True


def test_can_handle_manage_py(tmp_path):
    (tmp_path / "manage.py").write_text("# django\n", encoding="utf-8")
    assert PythonExtractor().can_handle(tmp_path) is True


def test_can_handle_bare_py_files(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert PythonExtractor().can_handle(tmp_path) is True


def test_can_handle_false_for_empty(tmp_path):
    assert PythonExtractor().can_handle(tmp_path) is False


def test_can_handle_false_for_non_python(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "Gemfile").write_text("source 'x'\n", encoding="utf-8")
    assert PythonExtractor().can_handle(tmp_path) is False


# --------------------------------------------------------------------------- #
# parse_repo — normalized fields + extras
# --------------------------------------------------------------------------- #


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "models.py").write_text(
        "from django.db import models\n\n\nclass User(models.Model):\n"
        "    name = models.CharField()\n\n    def display(self):\n        return self.name\n",
        encoding="utf-8",
    )
    (tmp_path / "views.py").write_text(
        "from django.shortcuts import render\n\n\n"
        "def index(request):\n    return render(request, 'i.html')\n",
        encoding="utf-8",
    )
    return tmp_path


def test_parse_repo_returns_parsed_files(tmp_path):
    result = PythonExtractor().parse_repo(_repo(tmp_path))
    assert isinstance(result, ParseResult)
    by_name = {p.path.name: p for p in result.files}
    assert set(by_name) == {"models.py", "views.py"}

    models = by_name["models.py"]
    assert models.default_export_kind == "ClassDef"
    assert models.has_jsx is False
    assert ("django.db", "named") in [tuple(s) for s in models.import_specifiers]
    assert models.sha_hint is not None


def test_parse_repo_carries_extras(tmp_path):
    result = PythonExtractor().parse_repo(_repo(tmp_path))
    by_name = {p.path.name: p for p in result.files}
    models = by_name["models.py"]

    sigs = models.extras["callable_signatures"]
    display = next(s for s in sigs if s["name"] == "display")
    assert display["enclosing_class"] == "User"
    assert display["base_class"] == "models.Model"

    shapes = models.extras["class_shapes"]
    user = next(c for c in shapes if c["name"] == "User")
    assert user["bases"] == ["models.Model"]


def test_parse_repo_empty_when_no_python(tmp_path):
    result = PythonExtractor().parse_repo(tmp_path)
    assert result.files == []


def test_unavailable_when_dump_script_missing(tmp_path):
    ext = PythonExtractor(libcst_dump_script=tmp_path / "nonexistent.py")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(PythonUnavailableError):
        ext.parse_repo(tmp_path)
