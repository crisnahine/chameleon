"""Central, env-overridable threshold module.

v0.5.7 audit item #2 flagged that ~10 hardcoded numeric thresholds
(_WORKSPACE_FANOUT_CAP, _EDIT_OBS_HARD_CAP, _MAX_EXTENDS_HOPS, etc.)
were spread across the codebase with no env-override path. Operators
who hit a threshold cap had to fork the code.

This module declares each threshold once with:
  - a default value,
  - a stable env-var name (``CHAMELEON_<NAME>``),
  - a one-line rationale via the DOCS map.

Existing modules keep their local constants (for back-compat), but new
readers should use ``threshold('NAME')`` to pick up env overrides.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULTS: Final[dict[str, int | float]] = {
    # bootstrap/orchestrator.py
    "WORKSPACE_FANOUT_CAP": 50,
    "WARNING_SAMPLE_PATHS": 3,
    "SPARSE_WARNING_LIMIT": 50,
    # bootstrap/tool_config.py
    "MAX_EXTENDS_HOPS": 8,
    # drift/observations.py
    "EDIT_OBS_HARD_CAP": 50_000,
    "EDIT_OBS_SOFT_CAP": 10_000,
    "EDIT_OBS_AGE_DAYS": 90,
    # tools.py
    "STRUCTURED_TOTAL_CAP": 50_000,
    # daemon.py
    "SPAWN_WAIT_SECONDS": 3.0,
    "LISTEN_BACKLOG": 16,
    # lint_engine.py
    "MAX_CONCAT_FOLDS_PER_FILE": 1000,
    # bootstrap/clustering.py — Option 1 shape-fuzzy merge
    # Two clusters sharing (path_pattern_bucket, default_export_kind, jsx_present)
    # merge when their UNION top_level_node_kinds Jaccard >= this threshold.
    # Default 0.7: conservative enough to avoid over-merging genuinely different
    # archetypes (ClassNode-only vs FunctionDeclaration-only scores 0.0).
    "CLUSTER_SHAPE_JACCARD_THRESHOLD": 0.7,
}

DOCS: Final[dict[str, str]] = {
    "WORKSPACE_FANOUT_CAP": "Max first-level workspace dirs walked when detecting a TS monorepo.",
    "WARNING_SAMPLE_PATHS": "Sample paths emitted per sparse_cluster_warning.",
    "SPARSE_WARNING_LIMIT": "Max sparse_cluster_warnings emitted in a bootstrap response.",
    "MAX_EXTENDS_HOPS": "Max levels deep tsconfig 'extends' resolution walks.",
    "EDIT_OBS_HARD_CAP": "drift.db edit_observations row count that triggers cleanup.",
    "EDIT_OBS_SOFT_CAP": "Target row count after cleanup trim.",
    "EDIT_OBS_AGE_DAYS": "Edit observations older than this many days are deleted on cleanup.",
    "STRUCTURED_TOTAL_CAP": "Max byte size of structured-teach rationale + example + counterexample.",
    "SPAWN_WAIT_SECONDS": "Wait time for daemon socket to appear after spawn.",
    "LISTEN_BACKLOG": "Daemon UNIX socket listen() backlog.",
    "MAX_CONCAT_FOLDS_PER_FILE": "Cap on AST-walk concat-folding iterations to avoid pathological inputs.",
    "CLUSTER_SHAPE_JACCARD_THRESHOLD": (
        "Jaccard threshold for the Option 1 shape-fuzzy merge step in cluster_files. "
        "Clusters sharing (path_pattern_bucket, default_export_kind, jsx_present) merge "
        "when their union top_level_node_kinds Jaccard >= this value (default 0.7)."
    ),
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
