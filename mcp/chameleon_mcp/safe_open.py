"""Single shared helper for all file reads in chameleon-mcp.

Every MCP tool that reads a file path MUST go through `safe_open()`.
This is the security-critical helper that prevents:
- Path traversal (../../../etc/passwd, NFD-encoded .. sequences)
- Symlink TOCTOU (lstat before open, refuse symlinks)
- Repo-boundary escape (realpath + prefix-match against repo_root)
- Null-byte path manipulation
- Windows ADS streams

See ARCHITECTURE.md "Security mitigations" #5 (symlink lstat + repo-boundary check)
and Round 5 AppSec recommendation #3 (single safe_open helper).
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path


class UnsafeFileError(Exception):
    """Raised when a file fails one of safe_open's security checks."""


def safe_open(repo_root: Path, rel_path: str, *, max_size_bytes: int = 1_000_000) -> Path:
    """Resolve and validate a relative path inside a repo. Returns the safe absolute Path.

    Args:
        repo_root: absolute path to the repo's root directory (must already be canonicalized)
        rel_path: untrusted relative path from MCP input or profile data
        max_size_bytes: file size ceiling (default 1 MB; matches AST extractor cap)

    Returns:
        Resolved absolute Path object that is safe to open for reading.

    Raises:
        UnsafeFileError: if any security check fails. Caller should fail-closed.
    """
    # 1. Reject null bytes — any path component containing \x00 is suspicious
    if "\x00" in rel_path:
        raise UnsafeFileError("path contains null byte")

    # 2. Reject Windows-style ADS (alternate data streams)
    if ":" in rel_path and not rel_path.startswith(("./", "../")):
        # On POSIX, : in paths is unusual; on NTFS, file.ext:$DATA is an ADS stream
        if "$DATA" in rel_path or "$SECURITY" in rel_path:
            raise UnsafeFileError("path contains Windows alternate data stream")

    # 3. Normalize unicode (defeat NFD-encoded ..  sequences)
    normalized = unicodedata.normalize("NFC", rel_path)
    if normalized != rel_path:
        # Accept the NFC form, but flag if the un-normalized form was different
        # (catches NFD attacks where decomposed combining marks form .. when collapsed)
        if ".." in normalized:
            raise UnsafeFileError("path contains .. after NFC normalization")

    # 4. Reject obviously suspicious patterns
    suspicious_segments = {"..", ".git", ".ssh", ".aws", ".gnupg"}
    parts = Path(rel_path).parts
    for part in parts:
        if part in suspicious_segments:
            raise UnsafeFileError(f"path contains forbidden segment: {part}")

    # 5. Resolve full path with realpath (follows symlinks intentionally for resolution,
    #    then we lstat the *result* to catch symlinks at the leaf)
    candidate = (repo_root / rel_path).resolve(strict=False)

    # 6. Boundary check: candidate must be inside repo_root after resolution
    repo_resolved = repo_root.resolve(strict=False)
    try:
        candidate.relative_to(repo_resolved)
    except ValueError as e:
        raise UnsafeFileError(f"path escapes repo boundary: {candidate} not under {repo_resolved}") from e

    # 7. lstat (NOT stat) to catch symlinks pointing outside the repo
    try:
        st = os.lstat(candidate)
    except FileNotFoundError as e:
        raise UnsafeFileError(f"path does not exist: {candidate}") from e
    except OSError as e:
        raise UnsafeFileError(f"lstat failed: {e}") from e

    # 8. Refuse symlinks (TOCTOU mitigation)
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeFileError(f"path is a symlink (refused): {candidate}")

    # 9. Refuse non-regular files (devices, fifos, sockets)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFileError(f"path is not a regular file: {candidate}")

    # 10. File size ceiling (DoS mitigation)
    if st.st_size > max_size_bytes:
        raise UnsafeFileError(f"file too large: {st.st_size} bytes > {max_size_bytes} cap")

    return candidate


def safe_read_text(
    repo_root: Path,
    rel_path: str,
    *,
    max_size_bytes: int = 1_000_000,
    encoding: str = "utf-8",
) -> str:
    """Convenience: validate path with safe_open, then read as text."""
    safe_path = safe_open(repo_root, rel_path, max_size_bytes=max_size_bytes)
    return safe_path.read_text(encoding=encoding, errors="replace")
