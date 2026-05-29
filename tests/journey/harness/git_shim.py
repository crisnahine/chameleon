"""Plant a fake `git` executable on PATH that sleeps before delegating.

Used to test trust.auto_preserve_when 2-second timeout (Phase 14).
ShimHandle supports context-manager protocol so PATH is restored even
if the test raises.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


class ShimHandle:
    def __init__(self, shim_dir: Path, original_path: str):
        self.shim_dir = shim_dir
        self.original_path = original_path

    def __enter__(self) -> ShimHandle:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()

    def restore(self) -> None:
        """Restore PATH to its pre-shim value. Idempotent."""
        if os.environ.get("PATH") != self.original_path:
            os.environ["PATH"] = self.original_path


def setup_git_shim(delay_seconds: float, shim_dir_parent: Path) -> ShimHandle:
    """Plant a fake `git` that sleeps, then exec real git. Returns ShimHandle.

    Usage:
        with setup_git_shim(5.0, ctx.run_dir / "shim") as shim:
            # any git invocation now sleeps 5s before real execution
            ...
    """
    shim_dir = shim_dir_parent / "git_shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "git"

    original_path = os.environ.get("PATH", "")
    real_git = None
    for d in original_path.split(":"):
        candidate = Path(d) / "git"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            real_git = str(candidate)
            break
    if real_git is None:
        raise RuntimeError("could not locate real git binary on PATH")

    shim_path.write_text(
        f"#!/bin/bash\nsleep {delay_seconds}\nexec {real_git} \"$@\"\n",
        encoding="utf-8",
    )
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    os.environ["PATH"] = f"{shim_dir}:{original_path}"
    return ShimHandle(shim_dir=shim_dir, original_path=original_path)
