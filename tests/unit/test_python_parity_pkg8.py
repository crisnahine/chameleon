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
