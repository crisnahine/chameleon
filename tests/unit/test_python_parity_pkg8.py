"""PKG-8: Python counterexample capture (off-pattern import witness).

teach_competing_import already writes the convention and the import-preference
lint fires for Python, but the per-edit counterexample ("do NOT write it this
way") was a no-op because the capture matcher assumed a quoted module specifier.
Python imports are unquoted.
"""

from __future__ import annotations

from chameleon_mcp.counterexamples import (
    _find_import_line,
    capture_counterexamples_in_repo,
)


def test_find_import_line_python_forms():
    assert _find_import_line("from requests import get\n", "requests") == "from requests import get"
    assert _find_import_line("import requests\n", "requests") == "import requests"
    assert _find_import_line("import requests.adapters\n", "requests") == "import requests.adapters"


def test_find_import_line_not_fooled_by_substring():
    assert _find_import_line("import requests_oauthlib\n", "requests") is None


def test_find_import_line_skips_comment_and_string():
    assert _find_import_line("# import requests here\n", "requests") is None
    assert _find_import_line('x = "import requests"\n', "requests") is None


def test_quoted_form_still_works_for_ts():
    assert _find_import_line('import x from "moment"\n', "moment") == 'import x from "moment"'


def test_capture_in_python_repo(tmp_path):
    (tmp_path / "client.py").write_text(
        "import requests\n\n\ndef get(url):\n    return requests.get(url)\n", encoding="utf-8"
    )
    rows = capture_counterexamples_in_repo(tmp_path, [{"over": "requests", "preferred": "httpx"}])
    assert len(rows) == 1
    assert rows[0]["over"] == "requests"
    assert "import requests" in rows[0].get("snippet", rows[0].get("line", ""))


# --------------------------------------------------------------------------- #
# string-embedded-import false-positive guard + inline-ignore string blanker.
# --------------------------------------------------------------------------- #


def _py_pkg(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    return pkg / "views.py"


def test_phantom_import_python_not_fooled_by_docstring(tmp_path):
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    editing = _py_pkg(tmp_path)
    # The bogus relative import lives inside a docstring -> not an import.
    content = '"""\nfrom .ghost import thing\n"""\nx = 1\n'
    v = lint_phantom_imports(content, file_path=str(editing), repo_root=tmp_path, language="python")
    assert not any(x.rule == "phantom-import" for x in v)


def test_phantom_import_python_ignore_in_string_does_not_suppress(tmp_path):
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    editing = _py_pkg(tmp_path)
    # A directive inside a string must NOT switch off the real phantom-import.
    content = 'from .ghost import thing\nx = "# chameleon-ignore phantom-import"\n'
    v = lint_phantom_imports(content, file_path=str(editing), repo_root=tmp_path, language="python")
    assert any(x.rule == "phantom-import" for x in v)


def test_phantom_import_python_real_comment_directive_suppresses(tmp_path):
    from chameleon_mcp.phantom_imports import lint_phantom_imports

    editing = _py_pkg(tmp_path)
    content = "from .ghost import thing  # chameleon-ignore phantom-import\n"
    v = lint_phantom_imports(content, file_path=str(editing), repo_root=tmp_path, language="python")
    assert not any(x.rule == "phantom-import" for x in v)


def test_blank_string_literals_python_docstring():
    from chameleon_mcp.violation_class import _blank_string_literals

    content = '"""\n# chameleon-ignore secret\n"""\nx = 1  # chameleon-ignore real\n'
    out = _blank_string_literals(content, "f.py", "python")
    # The directive inside the docstring is neutralized; the real comment stays.
    assert "secret" not in out
    assert "real" in out
    # Length-preserving so line numbers stay truthful.
    assert len(out) == len(content)
