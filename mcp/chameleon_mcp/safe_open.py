"""Single shared helper for all file reads in chameleon-mcp.

Every MCP tool that reads a file path MUST go through `safe_open()`.
This is the security-critical helper that prevents:
- Path traversal (../../../etc/passwd, NFD-encoded .. sequences)
- Symlink TOCTOU (lstat before open, refuse symlinks)
- Repo-boundary escape (realpath + prefix-match against repo_root)
- Null-byte path manipulation
- Windows ADS streams

See docs/architecture.md "Security mitigations" #5 (symlink lstat + repo-boundary check)
and Round 5 AppSec recommendation #3 (single safe_open helper).
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path


class UnsafeFileError(Exception):
    """Raised when a file fails one of safe_open's security checks."""


class FileTooLargeError(UnsafeFileError):
    """Raised specifically when a file exceeds the size ceiling.

    A subclass of UnsafeFileError so existing ``except UnsafeFileError``
    handlers still catch it, but callers that want to distinguish "too big"
    (a quality/DoS bound — safe to flag-and-continue) from a security
    rejection (traversal/symlink/ADS — must fail closed) can catch this first
    instead of string-matching the message.
    """


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
    if "\x00" in rel_path:
        raise UnsafeFileError("path contains null byte")

    if ":" in rel_path and not rel_path.startswith(("./", "../")):
        if "$DATA" in rel_path or "$SECURITY" in rel_path:
            raise UnsafeFileError("path contains Windows alternate data stream")

    normalized = unicodedata.normalize("NFC", rel_path)
    if normalized != rel_path:
        if ".." in normalized:
            raise UnsafeFileError("path contains .. after NFC normalization")

    suspicious_segments = {
        "..",
        ".git",
        ".ssh",
        ".aws",
        ".gnupg",
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".dockercfg",
    }
    parts = Path(rel_path).parts
    for part in parts:
        # Block common in-repo secret files (a witness/lint path should never
        # name one). Covers .env and its variants (.env.local, .env.production).
        if part in suspicious_segments or part == ".env" or part.startswith(".env."):
            raise UnsafeFileError(f"path contains forbidden segment: {part}")

    unresolved = repo_root / rel_path

    try:
        st = os.lstat(unresolved)
    except FileNotFoundError as e:
        raise UnsafeFileError(f"path does not exist: {unresolved}") from e
    except OSError as e:
        raise UnsafeFileError(f"lstat failed: {e}") from e

    if stat.S_ISLNK(st.st_mode):
        raise UnsafeFileError(f"path is a symlink (refused): {unresolved}")

    candidate = unresolved.resolve(strict=False)
    repo_resolved = repo_root.resolve(strict=False)
    try:
        candidate.relative_to(repo_resolved)
    except ValueError as e:
        raise UnsafeFileError(
            f"path escapes repo boundary: {candidate} not under {repo_resolved}"
        ) from e

    if not stat.S_ISREG(st.st_mode):
        raise UnsafeFileError(f"path is not a regular file: {unresolved}")

    if st.st_size > max_size_bytes:
        raise FileTooLargeError(f"file too large: {st.st_size} bytes > {max_size_bytes} cap")

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


_DEFAULT_PROFILE_ARTIFACT_MAX_BYTES = 5 * 1024 * 1024


def _open_profile_artifact_fd(path: Path, max_bytes: int) -> tuple[int, os.stat_result]:
    """Atomic O_NOFOLLOW open + fstat for a chameleon profile artifact.

    Used by ``safe_read_profile_artifact`` / ``safe_read_profile_artifact_bytes``.
    The caller is responsible for the file living in a trusted profile_dir;
    this helper enforces only the per-file checks (no symlink, regular file,
    size cap). O_NOFOLLOW closes the lstat-then-open TOCTOU window a
    teammate could exploit by swapping a committed renames.json for a
    symlink between checks.
    """
    # O_NOFOLLOW is POSIX-only (absent on Windows -> 0). The lstat symlink check
    # still rejects symlinks there; only the open()-level TOCTOU close is lost.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    flags |= cloexec
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise UnsafeFileError(f"open failed for {path}: {e}") from e
    try:
        st = os.fstat(fd)
    except OSError as e:
        os.close(fd)
        raise UnsafeFileError(f"fstat failed for {path}: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise UnsafeFileError(f"not a regular file: {path}")
    if st.st_size > max_bytes:
        os.close(fd)
        raise FileTooLargeError(
            f"profile artifact {path} is {st.st_size} bytes, exceeds {max_bytes} cap"
        )
    return fd, st


def safe_read_profile_artifact(
    path: Path,
    *,
    max_bytes: int = _DEFAULT_PROFILE_ARTIFACT_MAX_BYTES,
) -> str:
    """Read a chameleon profile artifact as text with O_NOFOLLOW + size cap.

    Use for files inside a known-trusted ``.chameleon/`` directory (the
    caller has already resolved the directory via ``find_repo_root`` or
    trust-record lookup). Returns the decoded UTF-8 text.

    Closes the lstat-then-open TOCTOU window: a teammate-controlled symlink
    swap between two syscalls cannot reach the read.

    Raises:
        UnsafeFileError: on symlink (O_NOFOLLOW), non-regular file, size cap.
        FileNotFoundError: passed through so callers can distinguish absent
                           from unsafe.
    """
    fd, _ = _open_profile_artifact_fd(path, max_bytes)
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def safe_read_profile_artifact_bytes(
    path: Path,
    *,
    max_bytes: int = _DEFAULT_PROFILE_ARTIFACT_MAX_BYTES,
) -> bytes:
    """Read a chameleon profile artifact as raw bytes with O_NOFOLLOW + cap.

    Same atomicity + size guarantees as ``safe_read_profile_artifact`` but
    returns bytes (used by ``profile.trust.hash_profile`` so the hash input
    matches the exact on-disk byte sequence).
    """
    fd, _ = _open_profile_artifact_fd(path, max_bytes)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def safe_open_fd(
    repo_root: Path,
    rel_path: str,
    *,
    max_size_bytes: int = 1_000_000,
) -> tuple[int, os.stat_result, Path]:
    """Atomic open + fstat for race-resistant reads.

    Returns (fd, stat_result, resolved_abs_path). The fd is opened with
    O_NOFOLLOW + O_CLOEXEC (if available) so a dirent swap to a symlink
    between this call and a later read is impossible -- the read happens
    against the inode this fstat saw. Caller MUST os.close(fd).

    Same validations as safe_open (null byte, ADS, NFC traversal,
    forbidden segments, repo boundary, file size cap, regular-file
    only). Symlink refusal is enforced both by O_NOFOLLOW (which raises
    OSError(ELOOP) at open time) and by an explicit st_mode check.

    Used by the excerpt cache builder; other callers should keep using
    safe_open or safe_read_text.
    """
    if "\x00" in rel_path:
        raise UnsafeFileError("path contains null byte")
    if ":" in rel_path and not rel_path.startswith(("./", "../")):
        if "$DATA" in rel_path or "$SECURITY" in rel_path:
            raise UnsafeFileError("path contains Windows alternate data stream")
    normalized = unicodedata.normalize("NFC", rel_path)
    if normalized != rel_path:
        if ".." in normalized:
            raise UnsafeFileError("path contains .. after NFC normalization")
    suspicious_segments = {
        "..",
        ".git",
        ".ssh",
        ".aws",
        ".gnupg",
        ".npmrc",
        ".netrc",
        ".pypirc",
        ".dockercfg",
    }
    parts = Path(rel_path).parts
    for part in parts:
        # Block common in-repo secret files (a witness/lint path should never
        # name one). Covers .env and its variants (.env.local, .env.production).
        if part in suspicious_segments or part == ".env" or part.startswith(".env."):
            raise UnsafeFileError(f"path contains forbidden segment: {part}")

    unresolved = repo_root / rel_path
    candidate = unresolved.resolve(strict=False)
    repo_resolved = repo_root.resolve(strict=False)
    try:
        candidate.relative_to(repo_resolved)
    except ValueError as e:
        raise UnsafeFileError(
            f"path escapes repo boundary: {candidate} not under {repo_resolved}"
        ) from e

    # O_NOFOLLOW is POSIX-only (absent on Windows -> 0). The lstat symlink check
    # still rejects symlinks there; only the open()-level TOCTOU close is lost.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    flags |= cloexec
    try:
        fd = os.open(str(candidate), flags)
    except FileNotFoundError as e:
        raise UnsafeFileError(f"path does not exist: {candidate}") from e
    except OSError as e:
        raise UnsafeFileError(f"open failed: {e}") from e

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFileError(f"path is not a regular file: {candidate}")
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeFileError(f"path is a symlink (refused): {candidate}")
        if st.st_size > max_size_bytes:
            raise FileTooLargeError(f"file too large: {st.st_size} bytes > {max_size_bytes} cap")
    except UnsafeFileError:
        os.close(fd)
        raise
    except OSError as e:
        os.close(fd)
        raise UnsafeFileError(f"fstat failed: {e}") from e

    return fd, st, candidate
