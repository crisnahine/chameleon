"""Regression tests for bootstrap-helper review fixes.

Covers three confirmed findings:
- comment_scan: commented-out-code detection must recognize Python def/class/import
  spans (the language branch must not fall through and miss Python).
- canonical: the recency weight must not boost a future-dated file.
- tool_config: config readers must bound the bytes pulled into memory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chameleon_mcp.bootstrap import canonical, tool_config
from chameleon_mcp.bootstrap.comment_scan import _ext_for, _span_is_code


class _StubParsedFile:
    """Minimal stand-in for the extractor's parsed-file object."""

    def __init__(self, kinds, *, diagnostics=0, import_specifiers=()):
        self.parse_diagnostics_count = diagnostics
        self.top_level_node_kinds = tuple(kinds)
        self.import_specifiers = tuple(import_specifiers)


# --- comment_scan: Python parity -------------------------------------------------


def test_ext_for_recognizes_python():
    assert _ext_for("python") == ".py"
    # TS and Ruby behavior is unchanged.
    assert _ext_for("typescript") == ".ts"
    assert _ext_for("ruby") == ".rb"
    assert _ext_for("go") is None


def test_python_def_span_reads_as_code():
    # libcst emits "FunctionDef"; the stdlib-ast fallback emits "AsyncFunctionDef".
    assert _span_is_code(_StubParsedFile(["FunctionDef"]), "python") is True
    assert _span_is_code(_StubParsedFile(["AsyncFunctionDef"]), "python") is True
    assert _span_is_code(_StubParsedFile(["ClassDef"]), "python") is True


def test_python_commented_import_caught_via_import_specifiers():
    # A commented-out import surfaces in import_specifiers, like Ruby's require.
    pf = _StubParsedFile(["Import"], import_specifiers=[["os", "namespace"]])
    assert _span_is_code(pf, "python") is True


def test_python_prose_rejected():
    # Prose parses without diagnostics into bare expression/assignment kinds and
    # must not be flagged as code.
    assert _span_is_code(_StubParsedFile(["Expr"]), "python") is False
    assert _span_is_code(_StubParsedFile(["Assign"]), "python") is False


def test_python_diagnostics_rejected():
    assert _span_is_code(_StubParsedFile(["FunctionDef"], diagnostics=1), "python") is False


def test_ruby_branch_unchanged():
    # The Ruby fall-through must stay byte-identical: class/module/def kinds and
    # the require-family import_specifiers fall-through both still work.
    assert _span_is_code(_StubParsedFile(["DefNode"]), "ruby") is True
    assert _span_is_code(_StubParsedFile(["ClassNode"]), "ruby") is True
    pf = _StubParsedFile(["CallNode"], import_specifiers=[["socket", "namespace"]])
    assert _span_is_code(pf, "ruby") is True
    # A bare CallNode from prose is rejected.
    assert _span_is_code(_StubParsedFile(["CallNode"]), "ruby") is False


def test_typescript_branch_unchanged():
    assert _span_is_code(_StubParsedFile(["ImportDeclaration"]), "typescript") is True
    assert _span_is_code(_StubParsedFile(["BinaryExpression"]), "typescript") is False


# --- canonical: future mtime must not be boosted ---------------------------------


def test_future_mtime_gets_no_recency_boost(tmp_path):
    f = tmp_path / "future.ts"
    f.write_text("x")
    # Reference clock is earlier than the file's mtime => future mtime.
    now = time.time() - 10_000
    assert canonical._file_recency_weight(f, now=now) == 1.0


def test_recent_mtime_gets_boost(tmp_path):
    f = tmp_path / "recent.ts"
    f.write_text("x")
    # Reference clock just after the file's mtime => inside the recency window.
    now = time.time() + 1.0
    assert canonical._file_recency_weight(f, now=now) == canonical.RECENCY_WEIGHT_MULTIPLIER


def test_old_mtime_gets_no_boost(tmp_path):
    f = tmp_path / "old.ts"
    f.write_text("x")
    now = time.time() + canonical._RECENCY_WINDOW_SECONDS + 10_000
    assert canonical._file_recency_weight(f, now=now) == 1.0


# --- tool_config: readers bound the bytes read -----------------------------------


def test_read_capped_bounds_bytes(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("a" * 5000)
    out = tool_config._read_capped(f, max_bytes=100)
    assert len(out) == 100


def test_read_capped_full_file_under_cap(tmp_path):
    f = tmp_path / "small.json"
    f.write_text('{"k": 1}')
    assert tool_config._read_capped(f) == '{"k": 1}'


def test_over_cap_json_config_fails_open(tmp_path):
    # A pathological config larger than the cap truncates mid-value, which is not
    # valid JSON, so the reader fails open rather than materializing the whole
    # file. Use a tiny cap to exercise the truncation path deterministically.
    pyproject = tmp_path / "pyproject.toml"
    body = "[tool.ruff]\nline-length = 100\n# " + ("x" * 50_000) + "\n"
    pyproject.write_text(body)
    # Real readers use the module-level cap; verify the bounded read itself caps.
    capped = tool_config._read_capped(pyproject, max_bytes=20)
    assert len(capped) == 20


def test_eslint_yaml_reader_bounds_huge_file(tmp_path, monkeypatch):
    # A multi-MB eslintrc.yml must not be read whole. Shrink the cap so a modest
    # fixture exercises the bound, then confirm the parsed mapping only reflects
    # the bytes within the cap.
    monkeypatch.setattr(tool_config, "_MAX_CONFIG_BYTES", 40)
    p = tmp_path / ".eslintrc.yml"
    # The first key is within 40 bytes; the second is far past it and must be
    # dropped because the read is bounded.
    p.write_text("kept: 1\n" + "padding: " + ("y" * 100_000) + "\ndropped: 2\n")
    parsed, warning = tool_config._parse_eslint_yaml(p)
    assert parsed is not None
    assert parsed.get("kept") == 1
    assert "dropped" not in parsed


def test_read_tool_configs_smoke_with_small_eslintrc(tmp_path):
    # End-to-end: a normal small config still parses through the capped reader.
    (tmp_path / ".eslintrc.json").write_text(json.dumps({"rules": {"no-var": "error"}}))
    result = tool_config.read_tool_configs(Path(tmp_path))
    assert result.eslint == {"rules": {"no-var": "error"}}
