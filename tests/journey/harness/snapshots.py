"""Capture chameleon state per phase for post-mortem inspection.

A snapshot is a recursive copy of:
  - <fixture>/.chameleon/  (the committed profile state)
  - <plugin_data_dir>/     (the per-run global state including drift.db)

into <run_dir>/snapshots/<act_id>/<phase_id>/.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def capture(snapshot_root: Path, act_id: str, phase_id: int, sources: list[Path]) -> Path:
    """Copy each source path into snapshot_root/<act_id>/phase_<phase_id>/<name>/.

    Missing sources are skipped silently. Returns the destination directory.
    """
    dest = snapshot_root / act_id / f"phase_{phase_id:02d}"
    dest.mkdir(parents=True, exist_ok=True)

    for src in sources:
        if not src.exists():
            continue
        target = dest / src.name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(src, target)
    return dest
