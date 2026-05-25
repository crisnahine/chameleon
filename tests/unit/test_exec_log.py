"""Unit tests for chameleon_mcp.exec_log — HMAC-signed execution log."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 1. HMAC key generation -> mode 0600
# ---------------------------------------------------------------------------

def test_hmac_key_created_with_mode_0600(tmp_path: Path):
    key_file = tmp_path / "hmac.key"

    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        from chameleon_mcp.exec_log import _ensure_hmac_key

        key = _ensure_hmac_key()

    assert key_file.is_file()
    assert len(key) == 32
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"expected 0600 but got {oct(mode)}"


def test_hmac_key_reuse_on_second_call(tmp_path: Path):
    key_file = tmp_path / "hmac.key"

    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        from chameleon_mcp.exec_log import _ensure_hmac_key

        k1 = _ensure_hmac_key()
        k2 = _ensure_hmac_key()

    assert k1 == k2


# ---------------------------------------------------------------------------
# 2. append_exec_log writes NDJSON with hmac field
# ---------------------------------------------------------------------------

def test_append_writes_ndjson_with_hmac(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {
        "CHAMELEON_HMAC_KEY_PATH": str(key_file),
        "TMPDIR": str(tmp_path),
    }):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(
            "repo-1",
            session_id="s1",
            command="echo hello",
            exit_code=0,
            duration_ms=42,
        )

    # Find the written log file
    log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1

    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert "hmac" in record
    assert record["session_id"] == "s1"
    assert record["command"] == "echo hello"
    assert record["exit_code"] == 0
    assert record["duration_ms"] == 42
    assert "ts" in record


# ---------------------------------------------------------------------------
# 3. Round-trip: write then verify = True
# ---------------------------------------------------------------------------

def test_verify_roundtrip(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {
        "CHAMELEON_HMAC_KEY_PATH": str(key_file),
        "TMPDIR": str(tmp_path),
    }):
        from chameleon_mcp.exec_log import append_exec_log, verify_exec_log_line

        append_exec_log(
            "repo-1",
            session_id="s1",
            command="ls -la",
            exit_code=0,
        )

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        log_file = list(log_dir.glob("*.jsonl"))[0]
        line = log_file.read_text(encoding="utf-8").strip()

        assert verify_exec_log_line(line) is True


# ---------------------------------------------------------------------------
# 4. Tampered command -> verify = False
# ---------------------------------------------------------------------------

def test_verify_tampered_command(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {
        "CHAMELEON_HMAC_KEY_PATH": str(key_file),
        "TMPDIR": str(tmp_path),
    }):
        from chameleon_mcp.exec_log import append_exec_log, verify_exec_log_line

        append_exec_log(
            "repo-1",
            session_id="s1",
            command="echo safe",
            exit_code=0,
        )

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        log_file = list(log_dir.glob("*.jsonl"))[0]
        line = log_file.read_text(encoding="utf-8").strip()

        record = json.loads(line)
        record["command"] = "rm -rf /"
        tampered = json.dumps(record, sort_keys=True, separators=(",", ":"))

        assert verify_exec_log_line(tampered) is False


# ---------------------------------------------------------------------------
# 5. Malformed JSON -> verify = False
# ---------------------------------------------------------------------------

def test_verify_malformed_json(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        from chameleon_mcp.exec_log import verify_exec_log_line

        assert verify_exec_log_line("{not json at all") is False
        assert verify_exec_log_line("") is False


# ---------------------------------------------------------------------------
# 6. Command truncation at 1KB
# ---------------------------------------------------------------------------

def test_command_truncated_at_1kb(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {
        "CHAMELEON_HMAC_KEY_PATH": str(key_file),
        "TMPDIR": str(tmp_path),
    }):
        from chameleon_mcp.exec_log import append_exec_log

        long_command = "x" * 2048
        append_exec_log(
            "repo-1",
            session_id="s1",
            command=long_command,
            exit_code=0,
        )

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        log_file = list(log_dir.glob("*.jsonl"))[0]
        line = log_file.read_text(encoding="utf-8").strip()
        record = json.loads(line)

        assert len(record["command"]) == 1024
        assert record["command"] == "x" * 1024
