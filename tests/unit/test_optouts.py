"""Unit tests for chameleon_mcp.optouts — opt-out hierarchy enforcement."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


def _setup_hmac_key(tmp_path: Path) -> Path:
    """Write a deterministic HMAC key and return the key file path."""
    key_file = tmp_path / "test_hmac.key"
    key_file.write_bytes(b"test-key-32-bytes-exactly-here!!")
    key_file.chmod(0o600)
    return key_file


def test_skip_file_triggers_repo_skip(tmp_path: Path):
    repo_root = tmp_path / "repo"
    chameleon_dir = repo_root / ".chameleon"
    chameleon_dir.mkdir(parents=True)
    (chameleon_dir / ".skip").touch()

    from chameleon_mcp.optouts import is_chameleon_suppressed

    result = is_chameleon_suppressed(repo_root, repo_id="r1")
    assert result == "repo_skip"


def test_no_skip_file_does_not_trigger(tmp_path: Path):
    repo_root = tmp_path / "repo"
    (repo_root / ".chameleon").mkdir(parents=True)

    from chameleon_mcp.optouts import is_chameleon_suppressed

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHAMELEON_DISABLE", None)
        result = is_chameleon_suppressed(repo_root, repo_id=None)
    assert result is None


def test_env_disable_triggers_user_disable(tmp_path: Path):
    from chameleon_mcp.optouts import is_chameleon_suppressed

    with patch.dict(os.environ, {"CHAMELEON_DISABLE": "1"}):
        result = is_chameleon_suppressed(repo_root=None, repo_id=None)
    assert result == "user_disable"


def test_env_disable_other_value_ignored():
    from chameleon_mcp.optouts import is_chameleon_suppressed

    with patch.dict(os.environ, {"CHAMELEON_DISABLE": "0"}):
        result = is_chameleon_suppressed(repo_root=None, repo_id=None)
    assert result is None


def test_session_disable_valid_signature(tmp_path: Path):
    key_file = _setup_hmac_key(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    repo_id = "test-repo"
    session_id = "session-abc-123"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "CHAMELEON_DISABLE": "",
        },
    ):
        from chameleon_mcp.optouts import is_chameleon_suppressed, write_session_disable

        write_session_disable(repo_id, session_id)
        result = is_chameleon_suppressed(repo_root=None, repo_id=repo_id, session_id=session_id)
    assert result == "session_disable"


def test_session_disable_invalid_signature(tmp_path: Path):
    key_file = _setup_hmac_key(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    repo_id = "test-repo"
    session_id = "session-abc-123"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
            "CHAMELEON_DISABLE": "",
        },
    ):
        from chameleon_mcp.optouts import (
            _safe_session_marker,
            is_chameleon_suppressed,
            write_session_disable,
        )
        from chameleon_mcp.profile.trust import repo_data_dir

        write_session_disable(repo_id, session_id)

        marker = repo_data_dir(repo_id) / f".session_disabled.{_safe_session_marker(session_id)}"
        text = marker.read_text(encoding="utf-8")
        tampered = text.replace("sig=", "sig=deadbeef")
        marker.write_text(tampered, encoding="utf-8")

        result = is_chameleon_suppressed(repo_root=None, repo_id=repo_id, session_id=session_id)
    assert result is None


def test_pause_future_timestamp(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_id = "test-repo"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_DISABLE": "",
        },
    ):
        from chameleon_mcp.optouts import is_chameleon_suppressed, write_pause

        write_pause(repo_id, minutes=15)
        result = is_chameleon_suppressed(repo_root=None, repo_id=repo_id, session_id=None)
    assert result == "pause"


def test_pause_expired_cleans_up(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_id = "test-repo"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_DISABLE": "",
        },
    ):
        from chameleon_mcp.profile.trust import repo_data_dir

        pause_path = repo_data_dir(repo_id) / ".pause_until"
        past = datetime.fromtimestamp(time.time() - 3600, tz=UTC)
        pause_path.write_text(past.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")

        assert pause_path.is_file()

        from chameleon_mcp.optouts import is_chameleon_suppressed

        result = is_chameleon_suppressed(repo_root=None, repo_id=repo_id, session_id=None)

    assert result is None
    assert not pause_path.is_file(), "expired pause file should be deleted"


def test_pause_malformed_timestamp(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_id = "test-repo"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_DISABLE": "",
        },
    ):
        from chameleon_mcp.profile.trust import repo_data_dir

        pause_path = repo_data_dir(repo_id) / ".pause_until"
        pause_path.write_text("not-a-timestamp\n", encoding="utf-8")

        from chameleon_mcp.optouts import is_chameleon_suppressed

        result = is_chameleon_suppressed(repo_root=None, repo_id=repo_id, session_id=None)
    assert result is None


def test_safe_session_marker_deterministic():
    from chameleon_mcp.optouts import _safe_session_marker

    sid = "abc-def-123"
    m1 = _safe_session_marker(sid)
    m2 = _safe_session_marker(sid)
    assert m1 == m2


def test_safe_session_marker_is_hex():
    from chameleon_mcp.optouts import _safe_session_marker

    marker = _safe_session_marker("test-session")
    assert len(marker) == 16
    int(marker, 16)


def test_safe_session_marker_matches_sha256():
    from chameleon_mcp.optouts import _safe_session_marker

    sid = "session-xyz"
    expected = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
    assert _safe_session_marker(sid) == expected


def test_safe_session_marker_none_returns_unknown():
    from chameleon_mcp.optouts import _safe_session_marker

    assert _safe_session_marker(None) == "unknown"
    assert _safe_session_marker("") == "unknown"


def test_write_session_disable_creates_marker(tmp_path: Path):
    key_file = _setup_hmac_key(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    repo_id = "test-repo"
    session_id = "sess-001"

    with patch.dict(
        os.environ,
        {
            "CHAMELEON_PLUGIN_DATA": str(data_dir),
            "CHAMELEON_HMAC_KEY_PATH": str(key_file),
        },
    ):
        from chameleon_mcp.optouts import _safe_session_marker, write_session_disable

        marker_path = write_session_disable(repo_id, session_id)

    assert marker_path.is_file()
    assert marker_path.name == f".session_disabled.{_safe_session_marker(session_id)}"

    text = marker_path.read_text(encoding="utf-8")
    assert "disabled-at=" in text
    assert f"session_id={session_id}" in text
    assert "sig=" in text

    assert not (marker_path.parent / (marker_path.name + ".tmp")).exists()


def test_write_pause_creates_file(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_id = "test-repo"

    with patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(data_dir)}):
        from chameleon_mcp.optouts import write_pause
        from chameleon_mcp.profile.trust import repo_data_dir

        expiry_iso = write_pause(repo_id, minutes=10)
        pause_path = repo_data_dir(repo_id) / ".pause_until"

    assert pause_path.is_file()
    content = pause_path.read_text(encoding="utf-8")
    assert content == expiry_iso

    parsed = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
    assert parsed.timestamp() > time.time()

    assert not pause_path.with_suffix(".tmp").exists()
