"""ENAMETOOLONG (errno 63 macOS / 36 Linux) must never escape as an uncaught
OSError. A path with a component longer than NAME_MAX (255 bytes) passes the
total-length check but makes is_file()/lstat()/resolve() raise. The guards must
fail closed (graceful envelope / None), not crash.

Covers audit findings SA-BUG-14, SA-BUG-15, SA-BUG-16 and LIVE-BUG-3 (the
unbounded path that bloated .hook_errors.log was this same uncaught error).
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import tools
from chameleon_mcp.profile.loader import find_repo_root

# A single path component over NAME_MAX (255 bytes); total length stays < 4096.
LONG_COMPONENT = "a" * 300
LONG_PATH = f"/tmp/{LONG_COMPONENT}/file.ts"


def test_validate_file_path_arg_rejects_overlong_component():
    # total length is fine (< 4096) but the component is > 255 bytes
    assert len(LONG_PATH) < 4096
    assert tools._validate_file_path_arg(LONG_PATH) is False


def test_validate_file_path_arg_accepts_normal_path():
    assert tools._validate_file_path_arg("/tmp/repo/src/index.ts") is True


def test_validate_file_path_arg_rejects_overlong_multibyte_component():
    # a multibyte component whose UTF-8 encoding exceeds 255 bytes
    multibyte = "é" * 200  # 400 bytes in UTF-8
    assert tools._validate_file_path_arg(f"/tmp/{multibyte}/x.ts") is False


def test_find_repo_root_returns_none_on_overlong_component():
    # must not raise OSError(ENAMETOOLONG); returns None gracefully
    assert find_repo_root(Path(LONG_PATH)) is None


def test_content_signal_for_path_returns_none_on_overlong_component():
    # must not raise; returns the "none" sentinel
    assert tools._content_signal_for_path(Path(LONG_PATH)) == "none"


def test_detect_repo_graceful_on_overlong_component(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    # must return an envelope, not raise
    out = tools.detect_repo(LONG_PATH)
    assert isinstance(out, dict)
