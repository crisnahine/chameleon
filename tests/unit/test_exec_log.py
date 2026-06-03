"""Unit tests for chameleon_mcp.exec_log — HMAC-signed execution log."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch


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


def test_append_writes_ndjson_with_hmac(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "TMPDIR": str(tmp_path),
        },
    ):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(
            "repo-1",
            session_id="s1",
            command="echo hello",
            exit_code=0,
            duration_ms=42,
        )

    log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
    log_files = list(log_dir.glob("*.jsonl"))
    assert len(log_files) == 1

    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert "hmac" in record
    assert record["session_id"] == "s1"
    import hashlib

    assert record["command_sha256"] == hashlib.sha256(b"echo hello").hexdigest()
    assert "command" not in record  # the raw command body is never stored
    assert record["exit_code"] == 0
    assert record["duration_ms"] == 42
    assert "ts" in record


def test_verify_roundtrip(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "TMPDIR": str(tmp_path),
        },
    ):
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


def test_verify_tampered_command(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "TMPDIR": str(tmp_path),
        },
    ):
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
        record["command_sha256"] = "0" * 64
        tampered = json.dumps(record, sort_keys=True, separators=(",", ":"))

        assert verify_exec_log_line(tampered) is False


def test_verify_malformed_json(tmp_path: Path):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(os.environ, {"CHAMELEON_HMAC_KEY_PATH": str(key_file)}):
        from chameleon_mcp.exec_log import verify_exec_log_line

        assert verify_exec_log_line("{not json at all") is False
        assert verify_exec_log_line("") is False


def test_command_body_not_stored(tmp_path: Path):
    """The raw command (which can carry secrets) is never persisted — only a
    fixed-length sha256."""
    import hashlib

    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "TMPDIR": str(tmp_path),
        },
    ):
        from chameleon_mcp.exec_log import append_exec_log

        secret_cmd = "curl -H 'Authorization: Bearer sk-SECRETVALUE' https://x"
        append_exec_log("repo-1", session_id="s1", command=secret_cmd, exit_code=0)

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        text = list(log_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8")
        record = json.loads(text.strip())

        assert "command" not in record
        assert "SECRETVALUE" not in text  # the secret never reaches disk
        assert record["command_sha256"] == hashlib.sha256(secret_cmd.encode()).hexdigest()
        assert len(record["command_sha256"]) == 64


def test_exec_log_dir_fallback_uses_platform_temp(monkeypatch):
    """When TMPDIR is unset the base dir falls back to the platform temp dir.

    The fallback must not hardcode the POSIX /tmp path. Windows has no /tmp,
    so the fallback has to route through tempfile.gettempdir() to stay portable.
    """
    import tempfile

    from chameleon_mcp import exec_log

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/sentinel-platform-temp")

    captured: dict[str, Path] = {}

    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        captured.setdefault("first", self)
        return None

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    try:
        result = exec_log._exec_log_dir("repo-x")
    finally:
        monkeypatch.setattr(Path, "mkdir", real_mkdir)

    assert str(result).startswith("/sentinel-platform-temp/")
    assert ".chameleon_exec_log" in str(result)


def test_gc_purges_old_session_logs(tmp_path: Path):
    """A new session's first append purges session logs older than RETENTION_DAYS."""
    import time

    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "TMPDIR": str(tmp_path),
        },
    ):
        from chameleon_mcp.exec_log import RETENTION_DAYS, append_exec_log

        log_dir = tmp_path / ".chameleon_exec_log" / "repo-1"
        log_dir.mkdir(parents=True)
        stale = log_dir / "old-session.jsonl"
        stale.write_text("{}\n", encoding="utf-8")
        old_mtime = time.time() - (RETENTION_DAYS + 5) * 86400
        os.utime(stale, (old_mtime, old_mtime))

        append_exec_log("repo-1", session_id="fresh", command="ls", exit_code=0)

        remaining = list(log_dir.glob("*.jsonl"))
        assert stale not in remaining  # stale log purged
        assert len(remaining) == 1  # only the fresh session's log
