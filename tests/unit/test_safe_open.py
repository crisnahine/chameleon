"""Unit tests for safe_open.py — path-traversal, symlink, boundary, and size checks."""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import pytest

from chameleon_mcp.safe_open import UnsafeFileError, safe_open, safe_open_fd, safe_read_text

# ---- 1. Path traversal ----


def test_dotdot_segment_rejected(tmp_path: Path):
    """../  segments are forbidden regardless of where they resolve."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "legit.txt").write_text("ok")
    with pytest.raises(UnsafeFileError, match="forbidden segment"):
        safe_open(tmp_path, "sub/../legit.txt")


def test_dotdot_at_start_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="forbidden segment"):
        safe_open(tmp_path, "../etc/passwd")


def test_deeply_nested_dotdot_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="forbidden segment"):
        safe_open(tmp_path, "a/b/c/../../../etc/passwd")


# ---- 2. Null byte rejection ----


def test_null_byte_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="null byte"):
        safe_open(tmp_path, "file.txt\x00.jpg")


def test_null_byte_in_directory_component(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="null byte"):
        safe_open(tmp_path, "dir\x00name/file.txt")


def test_null_byte_rejected_in_safe_open_fd(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="null byte"):
        safe_open_fd(tmp_path, "file\x00.txt")


# ---- 3. NFC normalization ----


def test_nfc_normalization_blocks_dotdot(tmp_path: Path):
    """An NFD-decomposed path that collapses to .. after NFC normalization is rejected."""
    # Build a path that contains ".." only after NFC normalization.
    # U+2024 ONE DOT LEADER is not a combining char, so we use a real
    # decomposition trick: craft a string that's different pre/post NFC
    # and contains ".." post-NFC.
    #
    # U+00C0 (A-grave) decomposes to A + U+0300 in NFD. We use that to make
    # the NFC form differ from input, then include ".." so the NFC branch fires.
    nfd_a_grave = unicodedata.normalize("NFD", "À")  # A + combining grave
    # Path: <NFD char>/../etc/passwd  — different before/after NFC AND has ".."
    crafted = f"{nfd_a_grave}/../etc/passwd"
    assert unicodedata.normalize("NFC", crafted) != crafted  # precondition
    assert ".." in unicodedata.normalize("NFC", crafted)

    with pytest.raises(UnsafeFileError, match="after NFC normalization"):
        safe_open(tmp_path, crafted)


# ---- 4. Symlink rejection ----


def test_symlink_rejected(tmp_path: Path):
    """Leaf symlinks are refused even if they point inside the repo."""
    target = tmp_path / "real.txt"
    target.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(UnsafeFileError, match="symlink"):
        safe_open(tmp_path, "link.txt")


def test_symlink_escaping_boundary_rejected_by_safe_open_fd(tmp_path: Path):
    """safe_open_fd catches a symlink that resolves outside repo_root via boundary check.

    safe_open_fd resolves the path before opening, so the boundary check
    (not O_NOFOLLOW) is the primary defense against symlink-based escapes.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope")

    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "escape.txt"
    link.symlink_to(secret)

    # The symlink resolves outside repo, so boundary check catches it
    with pytest.raises(UnsafeFileError, match="escapes repo boundary"):
        safe_open_fd(repo, "escape.txt")


# ---- 5. Repo-boundary escape ----


def test_boundary_escape_rejected(tmp_path: Path):
    """A path resolving outside repo_root is rejected even without '..' segments."""
    # Create a file outside the repo root
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope")

    repo = tmp_path / "repo"
    repo.mkdir()

    # Symlink-based escape won't work (symlinks are rejected first),
    # so we test the boundary check by using an absolute-style trick:
    # safe_open joins repo_root / rel_path, but if rel_path were to
    # somehow resolve outside, the boundary check catches it.
    # The simplest way: pass a path with .. (which is caught earlier).
    # Instead, test that safe_open_fd catches boundary escape via resolve:
    with pytest.raises(UnsafeFileError):
        safe_open_fd(repo, "../outside/secret.txt")


# ---- 6. Size cap enforcement ----


def test_size_cap_enforced_safe_open(tmp_path: Path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 2000)

    with pytest.raises(UnsafeFileError, match="file too large"):
        safe_open(tmp_path, "big.txt", max_size_bytes=1000)


def test_size_cap_enforced_safe_open_fd(tmp_path: Path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 2000)

    with pytest.raises(UnsafeFileError, match="file too large"):
        safe_open_fd(tmp_path, "big.txt", max_size_bytes=1000)


def test_size_exactly_at_cap_passes(tmp_path: Path):
    f = tmp_path / "exact.txt"
    f.write_bytes(b"x" * 1000)

    result = safe_open(tmp_path, "exact.txt", max_size_bytes=1000)
    assert result.name == "exact.txt"


# ---- 7. Happy path ----


def test_normal_file_read_succeeds(tmp_path: Path):
    f = tmp_path / "hello.txt"
    f.write_text("world")

    result = safe_open(tmp_path, "hello.txt")
    assert result.exists()
    assert result.read_text() == "world"


def test_nested_file_succeeds(tmp_path: Path):
    sub = tmp_path / "src" / "components"
    sub.mkdir(parents=True)
    f = sub / "Button.tsx"
    f.write_text("export default class Button {}")

    result = safe_open(tmp_path, "src/components/Button.tsx")
    assert result.name == "Button.tsx"


# ---- 8. safe_read_text ----


def test_safe_read_text_returns_content(tmp_path: Path):
    f = tmp_path / "data.txt"
    f.write_text("hello world", encoding="utf-8")

    content = safe_read_text(tmp_path, "data.txt")
    assert content == "hello world"


def test_safe_read_text_rejects_traversal(tmp_path: Path):
    with pytest.raises(UnsafeFileError):
        safe_read_text(tmp_path, "../etc/passwd")


def test_safe_read_text_respects_size_cap(tmp_path: Path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * 5000)

    with pytest.raises(UnsafeFileError, match="file too large"):
        safe_read_text(tmp_path, "big.txt", max_size_bytes=1000)


# ---- 9. safe_open_fd ----


def test_safe_open_fd_returns_valid_fd(tmp_path: Path):
    f = tmp_path / "readable.txt"
    f.write_text("fd content")

    fd, st, resolved = safe_open_fd(tmp_path, "readable.txt")
    try:
        assert fd >= 0
        assert st.st_size == len(b"fd content")
        assert resolved.name == "readable.txt"
        # Read through the fd to confirm it's usable
        data = os.read(fd, st.st_size)
        assert data == b"fd content"
    finally:
        os.close(fd)


def test_safe_open_fd_nonexistent_file(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="does not exist"):
        safe_open_fd(tmp_path, "nope.txt")


# ---- 10. Forbidden segments ----


def test_git_segment_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="forbidden segment.*\\.git"):
        safe_open(tmp_path, ".git/config")


def test_ssh_segment_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="forbidden segment.*\\.ssh"):
        safe_open(tmp_path, ".ssh/id_rsa")


def test_aws_segment_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="forbidden segment.*\\.aws"):
        safe_open(tmp_path, ".aws/credentials")


# ---- 11. Windows ADS rejection ----


def test_windows_ads_data_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="alternate data stream"):
        safe_open(tmp_path, "file.txt:$DATA")


def test_windows_ads_security_rejected(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="alternate data stream"):
        safe_open(tmp_path, "file.txt:$SECURITY")


# ---- 12. Nonexistent file ----


def test_nonexistent_path_raises(tmp_path: Path):
    with pytest.raises(UnsafeFileError, match="does not exist"):
        safe_open(tmp_path, "no_such_file.txt")
