"""Central, env-overridable threshold module.

An audit flagged that ~10 hardcoded numeric thresholds
(_WORKSPACE_FANOUT_CAP, _EDIT_OBS_HARD_CAP, _MAX_EXTENDS_HOPS, etc.)
were spread across the codebase with no env-override path. Operators
who hit a threshold cap had to fork the code.

This module declares each threshold once with a default value and a stable
env-var name (``CHAMELEON_<NAME>``).

Existing modules keep their local constants (for back-compat), but new
readers should use ``threshold('NAME')`` to pick up env overrides.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULTS: Final[dict[str, int | float]] = {
    "WORKSPACE_FANOUT_CAP": 500,
    "WARNING_SAMPLE_PATHS": 3,
    "SPARSE_WARNING_LIMIT": 50,
    "MAX_EXTENDS_HOPS": 8,
    "EDIT_OBS_HARD_CAP": 50_000,
    "EDIT_OBS_SOFT_CAP": 10_000,
    "EDIT_OBS_AGE_DAYS": 90,
    "STRUCTURED_TOTAL_CAP": 50_000,
    "SPAWN_WAIT_SECONDS": 3.0,
    "LISTEN_BACKLOG": 16,
    "MAX_CONCAT_FOLDS_PER_FILE": 1000,
    "CLUSTER_SHAPE_JACCARD_THRESHOLD": 0.7,
    "CLUSTER_PATH_BUCKET_DEPTH": 2,
    "RENAMES_OVERLAY_CAP": 256,
    "DRIFT_BANNER_THRESHOLD": 0.4,
    "DRIFT_BANNER_MIN_OBSERVATIONS": 10,
    "DRIFT_BANNER_TTL_SECONDS": 7 * 24 * 3600,
    "CALIBRATION_MAX_FILES": 600,
    "CALIBRATION_MAX_SIBLINGS": 10,
    "CALIBRATION_FP_EPSILON": 0.001,
}


def _env_name(name: str) -> str:
    return f"CHAMELEON_{name}"


def threshold(name: str) -> int | float:
    """Return the current value of threshold ``name``.

    Resolves ``$CHAMELEON_<NAME>`` first; falls back to the documented
    default. Returns the default on KeyError or non-numeric env input.
    """
    if name not in DEFAULTS:
        raise KeyError(f"unknown threshold: {name!r}")
    default = DEFAULTS[name]
    raw = os.environ.get(_env_name(name))
    if raw is None:
        return default
    try:
        if isinstance(default, float):
            return float(raw)
        return int(raw)
    except ValueError:
        return default


def threshold_int(name: str) -> int:
    """Convenience: ``threshold(name)`` cast to int."""
    return int(threshold(name))


def threshold_float(name: str) -> float:
    """Convenience: ``threshold(name)`` cast to float."""
    return float(threshold(name))
