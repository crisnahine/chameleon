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
