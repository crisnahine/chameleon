"""Unit tests for the test-run signal in chameleon_mcp.exec_log.

Covers the privacy-preserving runner classifier (classify_test_command), the
persisted test_command_seen field on the NDJSON line, and the turn-end reader
(session_test_run_seen) that ORs a "passing test run was observed" bit out of
the HMAC-verified log without ever storing a command body.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp.exec_log import classify_test_command


@pytest.mark.parametrize(
    "command",
    [
        "jest",
        "npx jest --runInBand",
        "node_modules/.bin/vitest run",
        "mocha test/",
        "pytest",
        "pytest -q tests/unit",
        "python -m pytest tests/",
        "python3.12 -m unittest discover",
        "rspec spec/models/user_spec.rb",
        "bundle exec rspec",
        "bin/rails test",
        "rails test test/models",
        "bundle exec rake test",
        "rake spec",
        "go test ./...",
        "cargo test",
        "cargo nextest run",
        "mix test",
        "make test",
        "make test-unit",
        "pnpm test",
        "pnpm run test:unit",
        "yarn test",
        "npm test",
        "npm run test",
        "npm t",
        "bun test",
        "nx run app:test",
        "turbo run test",
        "bazel test //...",
        "tox",
        "CI=1 pytest",
        "yarn build && yarn test",
        "cd web; pnpm test",
    ],
)
def test_classify_recognizes_runners(command: str):
    assert classify_test_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "",
        "ls -la",
        "git status",
        "npm install",
        "npm run build",
        "echo running tests",  # mere mention, not an invocation
        "pip install pytest",  # installs the runner, does not run it
        "cat test.py",
        "rm -rf tests/",
        "docker build -t app .",
        "grep -r testfoo src/",
    ],
)
def test_classify_rejects_non_runners(command: str):
    assert classify_test_command(command) is False


def _env(tmp_path: Path) -> dict:
    return {
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
    }


def test_append_persists_test_command_seen_bool(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(
            "repo-1", session_id="s1", command="pytest -q", exit_code=0, test_command_seen=True
        )

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        record = json.loads(list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip())
        assert record["test_command_seen"] is True
        assert "command" not in record  # body still never stored


def test_append_derives_bit_when_omitted(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log("repo-1", session_id="s1", command="rspec", exit_code=0)
        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        record = json.loads(list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip())
        assert record["test_command_seen"] is True


def test_test_command_seen_field_is_hmac_signed(tmp_path: Path):
    """The added field is inside the signed canonical payload, so flipping it
    after the fact breaks verification."""
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log, verify_exec_log_line

        append_exec_log(
            "repo-1", session_id="s1", command="pytest", exit_code=0, test_command_seen=True
        )
        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        line = list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip()
        assert verify_exec_log_line(line) is True

        record = json.loads(line)
        record["test_command_seen"] = False
        tampered = json.dumps(record, sort_keys=True, separators=(",", ":"))
        assert verify_exec_log_line(tampered) is False


def test_session_test_run_seen_true_on_passing_run(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log, session_test_run_seen

        append_exec_log("repo-1", session_id="s1", command="ls", exit_code=0)
        append_exec_log("repo-1", session_id="s1", command="pytest", exit_code=0)
        assert session_test_run_seen("repo-1", "s1") is True


def test_session_test_run_seen_false_when_no_test_ran(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log, session_test_run_seen

        append_exec_log("repo-1", session_id="s1", command="ls", exit_code=0)
        append_exec_log("repo-1", session_id="s1", command="git status", exit_code=0)
        assert session_test_run_seen("repo-1", "s1") is False


def test_session_test_run_seen_false_when_test_failed(tmp_path: Path):
    """A test runner that exited non-zero does not satisfy the passing-run bit."""
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log, session_test_run_seen

        append_exec_log("repo-1", session_id="s1", command="pytest", exit_code=1)
        assert session_test_run_seen("repo-1", "s1") is False


def test_session_test_run_seen_ignores_tampered_line(tmp_path: Path):
    """A forged 'test passed' line that fails HMAC verification is not trusted."""
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import append_exec_log, session_test_run_seen

        append_exec_log("repo-1", session_id="s1", command="ls", exit_code=0)
        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        log_file = list(log_dir.glob("*.jsonl"))[0]
        forged = {
            "ts": 1.0,
            "session_id": "s1",
            "command_sha256": "0" * 64,
            "exit_code": 0,
            "duration_ms": None,
            "test_command_seen": True,
            "hmac": "deadbeef",
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(forged) + "\n")

        assert session_test_run_seen("repo-1", "s1") is False


def test_session_test_run_seen_missing_log_returns_false(tmp_path: Path):
    with patch.dict(os.environ, _env(tmp_path), clear=False):
        from chameleon_mcp.exec_log import session_test_run_seen

        assert session_test_run_seen("repo-never", "s-never") is False
