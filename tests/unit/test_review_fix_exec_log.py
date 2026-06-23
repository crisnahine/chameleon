"""Regression tests for exec_log security/robustness fixes.

Covers three findings:
- the exec-log directory is owner-checked (not only the HMAC key file),
- the read paths refuse a symlinked leaf like the write path does,
- append_exec_log swallows HMACKeyError internally instead of leaning on the
  caller for fail-open.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chameleon_mcp import exec_log


@pytest.fixture
def exec_env(tmp_path, monkeypatch):
    """Point TMPDIR and the HMAC key at an isolated temp tree."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(tmp_path / "exec_hmac.key"))
    return tmp_path


def test_mkdir_checked_refuses_foreign_owned_dir(exec_env, monkeypatch):
    """A real exec-log dir owned by another uid is refused, not written into."""
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX-only owner check")

    target = exec_env / ".chameleon_exec_log" / "repo"
    target.mkdir(parents=True)

    euid = os.geteuid()
    real_stat = os.stat

    class _ForeignStat:
        st_uid = euid + 1
        st_mode = 0o40700

    def fake_stat(path, *a, **kw):
        if Path(path) == target:
            return _ForeignStat()
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(exec_log.os, "stat", fake_stat)

    with pytest.raises(exec_log.ExecLogUnsafeError):
        exec_log._mkdir_checked(target, parents=False)


def test_mkdir_checked_accepts_own_dir(exec_env):
    """The owner check passes for a dir the calling process owns."""
    target = exec_env / ".chameleon_exec_log" / "repo"
    # Two calls (creation then re-resolve) must both succeed.
    exec_log._mkdir_checked(target, parents=True)
    exec_log._mkdir_checked(target, parents=True)
    assert target.is_dir()


def test_read_check_events_refuses_symlinked_leaf(exec_env):
    """A planted checks.jsonl symlink is not followed on the read path."""
    from chameleon_mcp.optouts import _safe_session_marker

    repo_id = "repo"
    session = "sess"
    log_dir = exec_log._exec_log_dir(repo_id)
    outside = exec_env / "secret.jsonl"
    outside.write_text('{"check":"x","status":"ran","hmac":"deadbeef"}\n', encoding="utf-8")

    leaf = log_dir / f"{_safe_session_marker(session)}.checks.jsonl"
    leaf.symlink_to(outside)

    result = exec_log.read_check_events(repo_id, session, limit=10)
    assert result == {"events": [], "unverified": 0}


def test_session_test_run_seen_refuses_symlinked_leaf(exec_env):
    """A planted session.jsonl symlink does not leak a 'tests passed' signal."""
    from chameleon_mcp.optouts import _safe_session_marker

    repo_id = "repo"
    session = "sess"
    log_dir = exec_log._exec_log_dir(repo_id)
    outside = exec_env / "fake.jsonl"
    # A forged passing-test record the attacker would want followed.
    outside.write_text(
        '{"test_command_seen":true,"exit_code":0}\n',
        encoding="utf-8",
    )

    leaf = log_dir / f"{_safe_session_marker(session)}.jsonl"
    leaf.symlink_to(outside)

    assert exec_log.session_test_run_seen(repo_id, session) is False


def test_read_paths_still_read_a_regular_file(exec_env):
    """The symlink-safe read open still serves a normal, HMAC-valid log."""
    repo_id = "repo"
    session = "sess"
    exec_log.append_exec_log(
        repo_id,
        session_id=session,
        command="pytest -q",
        exit_code=0,
    )
    assert exec_log.session_test_run_seen(repo_id, session) is True


def test_append_exec_log_swallows_hmac_key_error(exec_env, monkeypatch):
    """append_exec_log fails open internally when the HMAC key is unavailable."""

    def boom():
        raise exec_log.HMACKeyError("no key")

    monkeypatch.setattr(exec_log, "_ensure_hmac_key", boom)

    # Must not raise even though HMACKeyError is not an OSError.
    exec_log.append_exec_log(
        repo_id="repo",
        session_id="sess",
        command="pytest -q",
        exit_code=0,
    )

    # And nothing was written: no key means no signable, trustworthy entry.
    from chameleon_mcp.optouts import _safe_session_marker

    log_path = exec_env / ".chameleon_exec_log" / "repo" / f"{_safe_session_marker('sess')}.jsonl"
    assert not log_path.exists()
