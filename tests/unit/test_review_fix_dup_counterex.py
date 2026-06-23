"""Regression tests for two code-review fixes.

1. Counterexample capture: the unquoted Python import form must not fire on a
   TypeScript default import that aliases the preferred module to the
   discouraged module's name (``import moment from "dayjs"``).
2. Duplication review: two confirmed duplicates that share a new_name (in
   different files) must both be reported, not collapsed to one.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import counterexamples as ce
from chameleon_mcp.duplication_review import Finding, _coerce_confirmed, build_duplication_prompt

# --- counterexample cross-language gating -------------------------------------


def test_ts_default_import_alias_not_captured_as_python_off_pattern():
    # Team taught "prefer dayjs over moment"; over == "moment". A TS file that
    # aliases the preferred module to the binding name `moment` imports the RIGHT
    # module and must NOT be captured as an off-pattern.
    content = 'import moment from "dayjs";\n'
    assert ce._find_import_line(content, "moment", "typescript") is None


def test_ruby_alias_binding_not_captured_as_python_off_pattern():
    # Ruby has no unquoted `import`; a bare `import moment` line must not match
    # under the ruby language gate.
    content = "import moment\n"
    assert ce._find_import_line(content, "moment", "ruby") is None


def test_python_bare_import_still_captured():
    # The Python branch must still fire on a genuine bare `import moment`.
    content = "import moment\n"
    assert ce._find_import_line(content, "moment", "python") == "import moment"


def test_python_from_import_still_captured():
    content = "from moment import now\n"
    assert ce._find_import_line(content, "moment", "python") == "from moment import now"


def test_ts_quoted_import_of_over_still_captured():
    # The quoted form is language-agnostic; a real TS import of the discouraged
    # module is still a valid off-pattern.
    content = 'import moment from "moment";\n'
    assert ce._find_import_line(content, "moment", "typescript") == 'import moment from "moment";'


def test_capture_in_repo_gates_per_file_language(tmp_path: Path):
    # A repo with a TS alias-shim and a Python bare import of the same `over`.
    # The capture must select the Python line, never the TS alias.
    (tmp_path / "a.ts").write_text('import moment from "dayjs";\n', encoding="utf-8")
    (tmp_path / "b.py").write_text("import moment\n", encoding="utf-8")
    rows = ce.capture_counterexamples_in_repo(tmp_path, [{"preferred": "dayjs", "over": "moment"}])
    assert len(rows) == 1
    assert rows[0]["snippet"] == "import moment"


def test_capture_in_repo_does_not_capture_js_family_alias(tmp_path: Path):
    # A JS-family file aliasing the preferred module to the discouraged name must
    # not be captured. The caller resolves an unrecognized extension to "" so the
    # Python-only unquoted form never runs against non-Python sources.
    (tmp_path / "a.jsx").write_text('import moment from "dayjs";\n', encoding="utf-8")
    rows = ce.capture_counterexamples_in_repo(tmp_path, [{"preferred": "dayjs", "over": "moment"}])
    assert rows == []


def test_default_no_language_preserves_legacy_both_forms():
    # The fuzz test and other existing callers invoke _import_of/_find_import_line
    # with no language; that default must still recognize both forms so the two
    # helpers stay mutually consistent.
    assert ce._import_of("moment").search("import moment")
    assert ce._find_import_line("import moment\n", "moment") == "import moment"


# --- duplication confirmation keyed by id -------------------------------------


def _finding(name: str, file: str) -> Finding:
    return Finding(name, file, 1, "body", f"orig_{name}", "existing.py")


def test_same_named_findings_both_confirmed_by_id():
    findings = [_finding("save", "a.py"), _finding("save", "b.py")]
    arr = [
        {"id": 0, "is_duplicate": True},
        {"id": 1, "is_duplicate": True},
    ]
    confirmed = _coerce_confirmed(arr, findings)
    assert len(confirmed) == 2
    assert {f.new_file for f in confirmed} == {"a.py", "b.py"}


def test_id_echo_disambiguates_single_confirmation():
    findings = [_finding("save", "a.py"), _finding("save", "b.py")]
    confirmed = _coerce_confirmed([{"id": 1, "is_duplicate": True}], findings)
    assert len(confirmed) == 1
    assert confirmed[0].new_file == "b.py"


def test_string_id_echo_is_accepted():
    findings = [_finding("save", "a.py"), _finding("save", "b.py")]
    confirmed = _coerce_confirmed([{"id": "1", "is_duplicate": True}], findings)
    assert len(confirmed) == 1 and confirmed[0].new_file == "b.py"


def test_name_echo_fallback_still_works():
    # A judge that omits id and echoes new_name is still honored.
    findings = [_finding("renamed", "a.py")]
    confirmed = _coerce_confirmed([{"new_name": "renamed", "is_duplicate": True}], findings)
    assert len(confirmed) == 1 and confirmed[0].new_name == "renamed"


def test_name_echo_fallback_does_not_collapse_same_named():
    # Two name echoes for the same name confirm two distinct findings, not one.
    findings = [_finding("save", "a.py"), _finding("save", "b.py")]
    arr = [
        {"new_name": "save", "is_duplicate": True},
        {"new_name": "save", "is_duplicate": True},
    ]
    confirmed = _coerce_confirmed(arr, findings)
    assert len(confirmed) == 2
    assert {f.new_file for f in confirmed} == {"a.py", "b.py"}


def test_is_duplicate_false_and_unknown_id_ignored():
    findings = [_finding("save", "a.py")]
    arr = [
        {"id": 0, "is_duplicate": False},
        {"id": 99, "is_duplicate": True},
        {"id": True, "is_duplicate": True},
        "garbage",
    ]
    assert _coerce_confirmed(arr, findings) == []


def test_prompt_renders_integer_ids():
    findings = [_finding("save", "a.py"), _finding("save", "b.py")]
    prompt = build_duplication_prompt(findings)
    assert "id 0:" in prompt and "id 1:" in prompt
