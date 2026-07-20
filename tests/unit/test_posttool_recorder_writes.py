"""Unit tests for posttool_recorder() in hook_helper.py + exec_log integration.

These exercise hook_helper.posttool_recorder for REAL — append_exec_log is NOT
mocked. A Bash PostToolUse payload is fed through stdin and we assert that a real
HMAC-signed exec-log entry lands on disk with the correct repo_id (derived from
the payload cwd), command sha256, and exit_code, and that the line verifies.

Isolation: no conftest.py exists in this tree, so each test pins
CHAMELEON_PLUGIN_DATA, CHAMELEON_HMAC_KEY_PATH, and TMPDIR to tmp_path inline
(mirroring the autouse-fixture pattern other suites use), and resets the
tools._REPO_ID_CACHE so a git-remote lookup from one test can't bleed into the
repo_id of another.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch


def _make_non_git_repo(tmp_path: Path) -> Path:
    """A real repo dir with NO git remote, so _compute_repo_id falls back to the
    path-hash branch and the resulting repo_id is fully deterministic.

    ``git init`` with no ``origin`` guarantees the path-based fallback even if
    tmp_path happened to live under another checkout.
    """
    repo = tmp_path / "myrepo"
    repo.mkdir()
    try:
        subprocess.run(
            ["git", "-C", str(repo), "init", "-q"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return repo


def _expected_repo_id(repo_root: Path) -> str:
    """The path-hash repo_id for a repo with no git remote.

    Mirrors _compute_repo_id's case-normalized path fallback (lower-cased
    resolved path) so the expectation holds even when the tmp path carries
    uppercase segments.
    """
    return hashlib.sha256(str(repo_root.resolve()).lower().encode("utf-8")).hexdigest()


def _run_recorder(payload: dict, *, env: dict, reset_repo_cache: bool = True) -> dict:
    """Run posttool_recorder() with a mocked stdin payload; return emitted JSON.

    append_exec_log is intentionally NOT mocked — the whole point is the real
    write path. Only stdin/stdout and os.environ are patched.
    """
    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, env, clear=False),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp import tools as _tools

        if reset_repo_cache:
            _tools._REPO_ID_CACHE.clear()
        from chameleon_mcp.hook_helper import posttool_recorder

        ret = posttool_recorder()
        assert ret == 0

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def _read_only_log_record(tmp_path: Path, repo_id: str) -> dict:
    """Return the single parsed log record written for repo_id (asserts exactly one)."""
    log_dir = tmp_path / ".chameleon_exec_log" / repo_id
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1, f"expected one log file, got {log_files}"
    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, f"expected one line, got {lines}"
    return json.loads(lines[0])


def _base_env(tmp_path: Path) -> dict:
    return {
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
    }


def test_real_entry_written_with_correct_fields(tmp_path: Path):
    """End-to-end: a Bash PostToolUse payload writes one signed record whose
    repo_id is derived from cwd, command sha256 matches, and exit_code is exact."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)
    command = "pytest -q tests/"

    result = _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"returnCode": 0},
            "session_id": "sess-42",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    # The hook always emits an empty PostToolUse envelope (no advisory).
    assert result == {}

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["session_id"] == "sess-42"
    assert record["command_sha256"] == hashlib.sha256(command.encode("utf-8")).hexdigest()
    assert "command" not in record  # raw command body never persisted
    assert record["exit_code"] == 0
    assert record["duration_ms"] is None  # recorder never passes duration
    assert isinstance(record["ts"], float)


def test_repo_id_directory_matches_cwd_derivation(tmp_path: Path):
    """The log lands under <TMPDIR>/.chameleon_exec_log/<repo_id>/ keyed by cwd."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": {"returnCode": 0},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    expected_dir = tmp_path / ".chameleon_exec_log" / repo_id
    assert expected_dir.is_dir()
    assert list(expected_dir.glob("*.jsonl"))
    # A different (wrong) repo_id dir must NOT exist.
    bogus = tmp_path / ".chameleon_exec_log" / ("0" * 64)
    assert not bogus.exists()


def test_hmac_signature_verifies(tmp_path: Path):
    """The written line verifies under the same key (real HMAC roundtrip)."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    with patch.dict(os.environ, _base_env(tmp_path), clear=False):
        _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo signed"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": str(repo),
            },
            env=_base_env(tmp_path),
        )

        log_dir = tmp_path / ".chameleon_exec_log" / repo_id
        line = list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip()

        from chameleon_mcp.exec_log import verify_exec_log_line

        assert verify_exec_log_line(line) is True

        # Tampering the persisted sha256 breaks verification.
        record = json.loads(line)
        record["command_sha256"] = "0" * 64
        tampered = json.dumps(record, sort_keys=True, separators=(",", ":"))
        assert verify_exec_log_line(tampered) is False


def test_nonzero_exit_code_preserved(tmp_path: Path):
    """A failing Bash command records its exact returnCode, not -1."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"returnCode": 127},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["exit_code"] == 127


def test_status_free_response_records_success(tmp_path: Path):
    """No status key -> exit_code 0: PostToolUse fires only on a successful call.

    This is the shape a live session sends for every Bash command, so treating it
    as unknown left the whole exec log at the -1 sentinel.
    """
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": {"stdout": "hi", "stderr": "", "interrupted": False},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["exit_code"] == 0


def test_missing_tool_response_entirely(tmp_path: Path):
    """No tool_response at all -> graceful: entry still written with exit_code -1."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    result = _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    assert result == {}
    record = _read_only_log_record(tmp_path, repo_id)
    assert record["exit_code"] == -1
    assert record["command_sha256"] == hashlib.sha256(b"echo hi").hexdigest()


def test_zero_return_code_not_coerced_to_minus_one(tmp_path: Path):
    """returnCode 0 must record 0, not -1 (guards an `or` falsy-coercion bug)."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": {"returnCode": 0},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["exit_code"] == 0


def test_missing_command_records_empty_string_sha(tmp_path: Path):
    """No command in tool_input -> command defaults to '' (sha of empty string)."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": {"returnCode": 0},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["command_sha256"] == hashlib.sha256(b"").hexdigest()


def test_missing_session_id_defaults_to_unknown(tmp_path: Path):
    """No session_id -> stored session_id is the literal 'unknown'."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"returnCode": 0},
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    record = _read_only_log_record(tmp_path, repo_id)
    assert record["session_id"] == "unknown"


def test_cwd_absent_falls_back_to_getcwd(tmp_path: Path):
    """No cwd key in payload -> repo_id derived from os.getcwd(); entry still written."""
    repo = _make_non_git_repo(tmp_path)
    cwd_repo_id = _expected_repo_id(repo)

    real_getcwd = os.getcwd

    with patch("os.getcwd", return_value=str(repo)):
        # Sanity: posttool_recorder will resolve cwd via os.getcwd() -> repo.
        _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
            },
            env=_base_env(tmp_path),
        )

    assert os.getcwd is real_getcwd  # patch fully unwound
    record = _read_only_log_record(tmp_path, cwd_repo_id)
    assert record["session_id"] == "s1"
    assert record["command_sha256"] == hashlib.sha256(b"pwd").hexdigest()


def test_blank_cwd_string_falls_back_to_getcwd(tmp_path: Path):
    """An empty-string cwd is treated as absent and falls back to os.getcwd()."""
    repo = _make_non_git_repo(tmp_path)
    cwd_repo_id = _expected_repo_id(repo)

    with patch("os.getcwd", return_value=str(repo)):
        _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": "",
            },
            env=_base_env(tmp_path),
        )

    record = _read_only_log_record(tmp_path, cwd_repo_id)
    assert record["session_id"] == "s1"


def test_non_dict_tool_response_handled(tmp_path: Path):
    """tool_response as a string (malformed) -> coerced to {}, exit_code -1."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)

    result = _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": "not-a-dict",
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    assert result == {}
    record = _read_only_log_record(tmp_path, repo_id)
    assert record["exit_code"] == -1


def test_malformed_stdin_emits_empty_and_writes_nothing(tmp_path: Path):
    """Non-JSON stdin -> fail open with {} and no exec log directory created."""
    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO("{ not json")),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, _base_env(tmp_path), clear=False),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp.hook_helper import posttool_recorder

        ret = posttool_recorder()

    assert ret == 0
    assert json.loads("".join(captured).strip()) == {}
    assert not (tmp_path / ".chameleon_exec_log").exists()


def test_non_object_json_stdin_fails_open(tmp_path: Path):
    """Valid JSON that isn't an object (a list) -> {} and no write."""
    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO("[1, 2, 3]")),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, _base_env(tmp_path), clear=False),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp.hook_helper import posttool_recorder

        ret = posttool_recorder()

    assert ret == 0
    assert json.loads("".join(captured).strip()) == {}
    assert not (tmp_path / ".chameleon_exec_log").exists()


def test_secret_command_body_never_hits_disk(tmp_path: Path):
    """A command carrying a secret is hashed, never stored verbatim in the log."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)
    secret_cmd = "curl -H 'Authorization: Bearer sk-TOPSECRET' https://api.example.com"

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": secret_cmd},
            "tool_response": {"returnCode": 0},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    log_dir = tmp_path / ".chameleon_exec_log" / repo_id
    raw_text = list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8")
    assert "TOPSECRET" not in raw_text
    record = json.loads(raw_text.strip())
    assert record["command_sha256"] == hashlib.sha256(secret_cmd.encode("utf-8")).hexdigest()


def test_hmac_key_generated_with_mode_0600(tmp_path: Path):
    """First recorder run generates the HMAC key at 0600 (no pre-seeded key)."""
    repo = _make_non_git_repo(tmp_path)
    key_path = tmp_path / "hmac.key"
    assert not key_path.exists()

    _run_recorder(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"returnCode": 0},
            "session_id": "s1",
            "cwd": str(repo),
        },
        env=_base_env(tmp_path),
    )

    assert key_path.is_file()
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_two_commands_same_session_append_two_lines(tmp_path: Path):
    """Two Bash invocations in one session append to the SAME session log file."""
    repo = _make_non_git_repo(tmp_path)
    repo_id = _expected_repo_id(repo)
    env = _base_env(tmp_path)

    for cmd in ("echo one", "echo two"):
        _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
                "tool_response": {"returnCode": 0},
                "session_id": "shared",
                "cwd": str(repo),
            },
            env=env,
        )

    log_dir = tmp_path / ".chameleon_exec_log" / repo_id
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1  # one session -> one file
    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    shas = {json.loads(line)["command_sha256"] for line in lines}
    assert shas == {
        hashlib.sha256(b"echo one").hexdigest(),
        hashlib.sha256(b"echo two").hexdigest(),
    }


def test_hmac_key_failure_swallowed_no_crash(tmp_path: Path):
    """If append_exec_log raises (HMAC key unreadable), posttool_recorder still
    fails open with {} — the broad try/except in the recorder swallows it."""
    repo = _make_non_git_repo(tmp_path)

    with patch(
        "chameleon_mcp.exec_log.append_exec_log",
        side_effect=RuntimeError("hmac boom"),
    ):
        result = _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": str(repo),
            },
            env=_base_env(tmp_path),
        )

    assert result == {}
