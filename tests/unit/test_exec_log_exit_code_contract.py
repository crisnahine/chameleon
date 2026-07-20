"""The recorder must classify a Bash run the way Claude Code actually reports it.

The Bash PostToolUse `tool_response` carries NO exit status. Captured from a live
session:

    {"stdout": "ok-one", "stderr": "", "interrupted": false,
     "isImage": false, "noOutputExpected": false}

Reading a status key -- `returnCode` first, then `exit_code` -- therefore always
missed, so every command logged `exit_code: -1` (the absent-value default) and
`session_test_run_seen`, which requires a zero exit, could never return True.
Measured on a live session afterwards: 37,293 rows, 37,291 recorded -1. The
turn-end "no passing test run" nudge stayed unsatisfiable no matter how much the
user tested.

The event is the status. PostToolUse fires only after a tool call SUCCEEDS
(a failed call raises PostToolUseFailure, which this hook is not registered for),
so a Bash command that reaches the recorder uninterrupted exited zero. Confirmed
by capture: `sh -c 'exit 3'` produced no PostToolUse invocation at all, while
`echo` did.

These tests use the CAPTURED payload shape. A fixture built from the documented
shape encodes an assumption the runtime does not honor and passes against the
bug -- which is exactly how the previous version of this file stayed green while
production recorded nothing but -1.
"""

from __future__ import annotations

import json

from chameleon_mcp import hook_helper as hh
from chameleon_mcp.exec_log import _exec_log_dir, session_test_run_seen

PASSING_TEST_CMD = "PYTHONPATH=. plugin/mcp/.venv/bin/python -m pytest tests/unit/ -q"

# The exact keys a live Claude Code session sends for a successful Bash call.
CAPTURED_BASH_RESPONSE = {
    "stdout": "6137 passed",
    "stderr": "",
    "interrupted": False,
    "isImage": False,
    "noOutputExpected": False,
}


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


def test_captured_payload_records_a_passing_run(tmp_path, monkeypatch, capsys):
    # The shape production actually sees: no status key anywhere.
    sid = "exitcode-captured-shape"
    repo_id = _record(monkeypatch, capsys, tmp_path, sid, dict(CAPTURED_BASH_RESPONSE))
    rows = _rows(repo_id, sid)
    assert rows, "the recorder wrote no exec-log row for a Bash invocation"
    assert rows[-1]["test_command_seen"] is True
    assert rows[-1]["exit_code"] == 0, (
        f"a successful Bash call was recorded as {rows[-1]['exit_code']!r}; "
        "PostToolUse only fires on success, so the advisory can never be satisfied"
    )
    assert session_test_run_seen(repo_id, sid) is True


def test_explicit_exit_code_is_preferred_when_a_host_sends_one(tmp_path, monkeypatch, capsys):
    sid = "exitcode-explicit"
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


def test_legacy_return_code_key_still_honored(tmp_path, monkeypatch, capsys):
    # A harness that predates the captured shape keeps working.
    sid = "exitcode-legacy"
    repo_id = _record(
        monkeypatch, capsys, tmp_path, sid, {"returnCode": 2, "stdout": "", "stderr": ""}
    )
    assert _rows(repo_id, sid)[-1]["exit_code"] == 2
    assert session_test_run_seen(repo_id, sid) is False


def test_interrupted_run_is_not_counted_as_passing(tmp_path, monkeypatch, capsys):
    # A command killed by timeout or the user may have run none of the suite;
    # counting it would re-open the same false signal from the other side.
    sid = "exitcode-interrupted"
    repo_id = _record(
        monkeypatch,
        capsys,
        tmp_path,
        sid,
        {"stdout": "", "stderr": "", "interrupted": True},
    )
    assert _rows(repo_id, sid)[-1]["exit_code"] != 0
    assert session_test_run_seen(repo_id, sid) is False
