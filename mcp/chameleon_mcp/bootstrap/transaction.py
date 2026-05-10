"""Atomic multi-file commit pattern for chameleon profile writes.

Per ARCHITECTURE.md "Atomicity & Crash Safety" → "Multi-file transactional commit":

  1. Write all artifacts into .chameleon/.tmp/<txn-id>/
  2. Verify each artifact (fsync, schema-validate, secret-scan)
  3. Write COMMITTED sentinel file last
  4. Atomic rename: .chameleon/.tmp/<txn-id>/ → .chameleon/

Loaders refuse to read .chameleon/ if COMMITTED is missing.
Per-PID temp subdir prevents collision when two refresh processes run simultaneously.

Round 4 distributed-systems hardening — addresses one of the 6 BLOCKING items.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

COMMITTED_SENTINEL = "COMMITTED"


@contextmanager
def atomic_profile_commit(target_dir: Path):
    """Context manager for atomic multi-file profile writes.

    Usage:
        with atomic_profile_commit(repo_root / ".chameleon") as txn_dir:
            (txn_dir / "profile.json").write_text(...)
            (txn_dir / "archetypes.json").write_text(...)
            # ... etc; all writes go to txn_dir, never directly to target_dir
        # On exit: COMMITTED sentinel written, txn_dir atomically renamed to target_dir.
        # On exception: txn_dir is removed; target_dir untouched.

    Args:
        target_dir: the .chameleon/ directory to atomically replace.
    """
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Per-PID + uuid temp subdir to prevent collisions between concurrent
    # processes (refresh_repo + bootstrap_repo running simultaneously).
    txn_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}-{int(time.time())}"
    tmp_root = target_dir.parent / f".{target_dir.name}.tmp"
    tmp_root.mkdir(exist_ok=True)
    txn_dir = tmp_root / txn_id
    txn_dir.mkdir()

    try:
        yield txn_dir

        # All writes succeeded. Verify expected artifacts are present.
        # (Real validation in Phase 2; for now, just check for at least one file.)
        if not any(txn_dir.iterdir()):
            raise RuntimeError("atomic_profile_commit: no artifacts written")

        # Write COMMITTED sentinel LAST.
        sentinel = txn_dir / COMMITTED_SENTINEL
        sentinel.write_text(f"committed-at={time.time()}\npid={os.getpid()}\n")
        # fsync the sentinel to ensure it's on disk before the rename
        with open(sentinel, "rb") as f:
            os.fsync(f.fileno())

        # Atomic rename: txn_dir → target_dir.
        # If target_dir already exists, we need to swap atomically. POSIX rename(2)
        # over an existing directory only works if target is empty. So we use a
        # two-step pattern: rename old target out of the way, then rename txn into place.
        backup_dir = target_dir.parent / f".{target_dir.name}.backup-{txn_id}"
        if target_dir.exists():
            os.rename(target_dir, backup_dir)
        try:
            os.rename(txn_dir, target_dir)
        except OSError:
            # Restore backup on rename failure
            if backup_dir.exists():
                os.rename(backup_dir, target_dir)
            raise
        # Success: remove backup
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        # Clean up partial txn dir on any failure
        if txn_dir.exists():
            shutil.rmtree(txn_dir, ignore_errors=True)
        raise


def is_committed(target_dir: Path) -> bool:
    """Return True iff target_dir contains a valid COMMITTED sentinel.

    Loaders use this to refuse incomplete profiles per ARCHITECTURE.md.
    """
    if not target_dir.is_dir():
        return False
    sentinel = target_dir / COMMITTED_SENTINEL
    return sentinel.is_file()


def cleanup_orphan_tmp_dirs(target_parent: Path, profile_dir_name: str = ".chameleon") -> int:
    """Sweep orphaned .tmp/<txn-id>/ directories that lack COMMITTED sentinels.

    Called on MCP server startup. Returns count of cleaned-up directories.
    """
    tmp_root = target_parent / f".{profile_dir_name}.tmp"
    if not tmp_root.is_dir():
        return 0
    cleaned = 0
    for txn_dir in tmp_root.iterdir():
        if txn_dir.is_dir() and not (txn_dir / COMMITTED_SENTINEL).exists():
            # TODO Phase 2: also check if PID prefix is dead before cleaning
            shutil.rmtree(txn_dir, ignore_errors=True)
            cleaned += 1
    return cleaned
