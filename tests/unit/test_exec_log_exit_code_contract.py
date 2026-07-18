"""The recorder must read the exit code Claude Code actually sends.

The recorder read `tool_response["returnCode"]`, but the documented Bash
PostToolUse payload carries `exit_code`. Every real test run therefore logged
`exit_code: -1` (the absent-value default), and `session_test_run_seen` -- which
requires a zero exit -- could never return True. Measured on a live session:
1928 rows, 17 correctly classified as test runs, all 17 recorded -1, zero
passing. The turn-end "no passing test run" nudge was unsatisfiable.

Documented schema (code.claude.com/docs/en/hooks.md#posttooluse):
    {"exit_code": 0, "stdout": "...", "stderr": "...", "interrupted": false}

These tests use that real shape, not the shape the implementation happened to
want -- a fixture built from the implementation encodes the same wrong
assumption and passes against the bug.
"""

from __future__ import annotations

import json

from chameleon_mcp import hook_helper as hh
from chameleon_mcp.exec_log import _exec_log_dir, session_test_run_seen

PASSING_TEST_CMD = "PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/unit/ -q"


def _payload(session_id: str, cwd: str, *, command: str, response: dict) -> str:
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": response,
            "cwd": cwd,
            "session_id": session_id,
        }
    )


def _rows(repo_id: str, session_id: str) -> list[dict]:
    from chameleon_mcp.optouts import _safe_session_marker

    p = _exec_log_dir(repo_id) / f"{_safe_session_marker(session_id)}.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _record(monkeypatch, capsys, tmp_path, session_id: str, response: dict) -> str:
    """Drive the real recorder with a real-shaped payload; return the repo_id."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        "sys.stdin",
        type(
            "S",
            (),
            {
                "read": staticmethod(
                    lambda: _payload(
                        session_id, str(repo), command=PASSING_TEST_CMD, response=response
                    )
                )
            },
        )(),
    )
    hh.posttool_recorder()
    capsys.readouterr()
    from chameleon_mcp.tools import _compute_repo_id

    return _compute_repo_id(repo)


def test_documented_exit_code_field_is_recorded(tmp_path, monkeypatch, capsys):
    sid = "exitcode-doc-shape"
    repo_id = _record(
        monkeypatch,
        capsys,
        tmp_path,
        sid,
        {"exit_code": 0, "stdout": "6137 passed", "stderr": "", "interrupted": False},
    )
    rows = _rows(repo_id, sid)
    assert rows, "the recorder wrote no exec-log row for a Bash invocation"
    assert rows[-1]["test_command_seen"] is True
    assert rows[-1]["exit_code"] == 0, (
        f"documented exit_code=0 was recorded as {rows[-1]['exit_code']!r}; "
        "the advisory can never be satisfied"
    )
    assert session_test_run_seen(repo_id, sid) is True


def test_failing_run_is_not_counted_as_passing(tmp_path, monkeypatch, capsys):
    sid = "exitcode-failing"
    repo_id = _record(
        monkeypatch,
        capsys,
        tmp_path,
        sid,
        {"exit_code": 1, "stdout": "1 failed", "stderr": "", "interrupted": False},
    )
    rows = _rows(repo_id, sid)
    assert rows[-1]["exit_code"] == 1
    assert session_test_run_seen(repo_id, sid) is False


def test_absent_exit_code_still_degrades_to_not_passing(tmp_path, monkeypatch, capsys):
    # A payload with no exit code at all must not be read as success.
    sid = "exitcode-absent"
    repo_id = _record(monkeypatch, capsys, tmp_path, sid, {"stdout": "", "stderr": ""})
    rows = _rows(repo_id, sid)
    assert rows[-1]["exit_code"] != 0
    assert session_test_run_seen(repo_id, sid) is False


def test_interrupted_run_is_not_counted_as_passing(tmp_path, monkeypatch, capsys):
    # A command killed by timeout or the user can carry exit_code 0 while having
    # run none of the suite; counting it would re-open the same false signal from
    # the other side.
    sid = "exitcode-interrupted"
    repo_id = _record(
        monkeypatch,
        capsys,
        tmp_path,
        sid,
        {"exit_code": 0, "stdout": "", "stderr": "", "interrupted": True},
    )
    assert session_test_run_seen(repo_id, sid) is False
