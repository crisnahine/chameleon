"""Size-based rotator for chameleon's hook error log.

Called from bash hook scripts via `python -m chameleon_mcp.log_rotation`
before appending to .hook_errors.log. Rotates when the file exceeds
ROTATE_THRESHOLD_BYTES; keeps up to MAX_ROTATIONS old files (.1, .2, ...).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROTATE_THRESHOLD_BYTES = 10 * 1024 * 1024
MAX_ROTATIONS = 5


def _backup_path(log_path: Path, n: int) -> Path:
    return log_path.parent / f"{log_path.name}.{n}"


def rotate_if_needed(log_path: Path) -> None:
    """Rotate `log_path` to `log_path.1` if it exceeds the threshold.

    Existing .1 -> .2, .2 -> .3, etc. Files beyond MAX_ROTATIONS get
    deleted. Silent on all errors (logging failures must not crash hooks).
    """
    try:
        size = log_path.stat().st_size
    except (FileNotFoundError, OSError):
        return
    if size < ROTATE_THRESHOLD_BYTES:
        return
    try:
        _backup_path(log_path, MAX_ROTATIONS).unlink(missing_ok=True)
    except OSError:
        pass
    for i in range(MAX_ROTATIONS - 1, 0, -1):
        src = _backup_path(log_path, i)
        dst = _backup_path(log_path, i + 1)
        try:
            src.rename(dst)
        except (FileNotFoundError, OSError):
            continue
    try:
        log_path.rename(_backup_path(log_path, 1))
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    rotate_if_needed(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
