"""Regression tests for fixes the adversarial verification pass surfaced.

- hard-secret PreToolUse deny now fires on config/data files (the real leak
  target) and skips only prose/doc files, instead of skipping every non-code
  file (which had disabled the pre-write block on .env/.yml/.json).
- the auto-pass skip-marker gate recognizes Python pytest/unittest markers so a
  Python diff adding @pytest.mark.xfail over a test is not auto-passed where the
  TS it.skip equivalent routes to a human.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.autopass import _SKIP_MARKER_PATTERNS
from chameleon_mcp.hook_helper import _proposed_hard_secret_violations

_AKIA = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize(
    "file_path,should_fire",
    [
        ("config/secrets.yml", True),
        ("deploy/.env", True),
        ("creds.json", True),
        ("settings.toml", True),
        ("app/service.py", True),
        ("README.md", False),
        ("docs/guide.rst", False),
        ("notes.txt", False),
    ],
)
def test_secret_deny_fires_on_config_skips_prose(file_path, should_fire):
    content = f"aws_secret = '{_AKIA}'\n"
    violations, _ = _proposed_hard_secret_violations(
        content, file_path=file_path, tool_name="Write"
    )
    assert bool(violations) is should_fire


def _skip_hit(s: str) -> bool:
    return any(p.search(s) for p in _SKIP_MARKER_PATTERNS)


@pytest.mark.parametrize(
    "line,hit",
    [
        ("@pytest.mark.skip", True),
        ("@pytest.mark.xfail", True),
        ("@pytest.mark.skipif(sys.platform == 'win32')", True),
        ("@unittest.skip('flaky')", True),
        ("@unittest.expectedFailure", True),
        ("it.skip('x', () => {})", True),  # TS arm still works
        ("my_skip_helper()", False),
        ("@app.route('/users')", False),
        ("skip_before_action :authenticate", False),
    ],
)
def test_python_skip_markers_recognized(line, hit):
    assert _skip_hit(line) is hit


def test_ruby_heredoc_token_in_comment_does_not_hide_sinks():
    # Regression: the Ruby stripper must blank a `#` comment (incl a `<<~TOKEN`
    # it merely mentions) BEFORE the heredoc pass, or the comment-embedded token
    # is honored as a heredoc and swallows the eval/exec sink below it to EOF.
    from chameleon_mcp.lint_engine import scan_dangerous_sinks

    rules = [
        v.rule
        for v in scan_dangerous_sinks("a = 1 # see <<~EOF note\nb = eval(x)\n", language="ruby")
    ]
    assert "eval-call" in rules


def test_ruby_quoted_heredoc_delimiter_body_blanked():
    # A quoted heredoc delimiter (<<~"EOF" / <<-'EOF') opens a heredoc, not a
    # string: its body must be blanked, not scanned as code.
    from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments as strip

    assert "secret_token" not in strip('x = <<~"EOF"\n  secret_token = 1\nEOF\ny = 2\n')
    assert "body_line" not in strip("x = <<-'EOF'\n  body_line\nEOF\nz = 3\n")


def test_init_export_set_opens_on_getattr_and_enumerates_compiled(tmp_path):
    # __init__ with PEP 562 __getattr__ -> open set (lazy exports unenumerable);
    # a compiled .so submodule is enumerated so importing it is not phantom.
    from chameleon_mcp.extractors.python import PythonExtractor

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("def __getattr__(name):\n    return None\n", encoding="utf-8")
    ex = (
        getattr(
            PythonExtractor().parse_repo(tmp_path, paths=[pkg / "__init__.py"]).files[0],
            "extras",
            {},
        )
        or {}
    )
    assert ex.get("export_set_open") is True

    pkg2 = tmp_path / "pkg2"
    pkg2.mkdir()
    (pkg2 / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (pkg2 / "_speedups.cpython-311-darwin.so").write_text("", encoding="utf-8")
    ex2 = (
        getattr(
            PythonExtractor().parse_repo(tmp_path, paths=[pkg2 / "__init__.py"]).files[0],
            "extras",
            {},
        )
        or {}
    )
    assert "_speedups" in set(ex2.get("named_export_names") or [])
    assert not ex2.get("export_set_open")  # still closed (no __getattr__)


def test_counterexample_find_import_line_respects_language():
    # The witness-suppression check must use the archetype language: the Python
    # unquoted import form must not match a TypeScript witness.
    from chameleon_mcp.counterexamples import _find_import_line

    ts_witness = "import foo from 'bar'\nconst x = useThing()\n"
    assert _find_import_line(ts_witness, "useThing", language="typescript") is None
    # the Python form still matches a real Python import when so scoped
    assert _find_import_line("import useThing\n", "useThing", language="python")
