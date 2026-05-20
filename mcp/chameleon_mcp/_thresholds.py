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
    # signatures.py — Option 4 path bucket depth.
    # Controls how many leading directory segments the path bucket uses for
    # non-monorepo paths with 4+ segments. Default 2 gives parts[0]/parts[1],
    # which collapses app/services/zoom and app/services/billing into one
    # app/services bucket. Set to 3 to restore the pre-v0.5.9 depth-3 formula
    # (parts[0]/parts[-3]/parts[-2]). The monorepo branch is always depth-3
    # and is unaffected by this setting.
    "CLUSTER_PATH_BUCKET_DEPTH": 2,
    # bootstrap/orchestrator.py + tools.py — renames overlay entry cap.
    # The merge logic at tools.py:_merge_rename_overlay walks back to the
    # auto-name on re-rename so the overlay can never exceed the number of
    # archetypes in the profile. Real chameleon profiles seen in dogfood
    # have on the order of 50-150 archetypes; 256 bounds DoS while leaving
    # generous headroom. Override via CHAMELEON_RENAMES_OVERLAY_CAP if a
    # genuinely larger monorepo needs more. Overlay loads that exceed the
    # cap return {} from the read path; apply_archetype_renames separately
    # refuses to write when the on-disk overlay is over-cap so a single
    # /chameleon-rename cannot silently wipe a teammate's larger overlay.
    "RENAMES_OVERLAY_CAP": 256,
    # hook_helper.py — drift banner gates surfaced at SessionStart.
    # Banner fires when (drift_score >= threshold) AND (observation
    # count >= min_observations) AND (per-repo cooldown marker is older
    # than TTL seconds). Marker lives under plugin_data_dir, not in-repo.
    "DRIFT_BANNER_THRESHOLD": 0.4,
    "DRIFT_BANNER_MIN_OBSERVATIONS": 10,
    "DRIFT_BANNER_TTL_SECONDS": 7 * 24 * 3600,
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
    "CLUSTER_PATH_BUCKET_DEPTH": (
        "Number of leading directory segments used for the path bucket on non-monorepo "
        "paths with 4+ segments (Option 4). Default 2: parts[0]/parts[1]. "
        "Set to 3 to restore the pre-v0.5.9 depth-3 formula (parts[0]/parts[-3]/parts[-2]). "
        "The monorepo branch is always depth-3 and is unaffected by this setting."
    ),
    "RENAMES_OVERLAY_CAP": (
        "Max number of entries accepted from a .chameleon/renames.json overlay. "
        "Loads that exceed the cap return an empty overlay (the workflow self-heals "
        "on next /chameleon-rename). Default 256 ~ 3.6x the largest realistic "
        "archetype_count."
    ),
    "DRIFT_BANNER_THRESHOLD": (
        "Minimum observed_drift_score (0.0-1.0) needed for the SessionStart "
        "drift banner to fire. Default 0.4 (avg confidence ~0.6, mostly medium). "
        "Raise to ~0.55 if banner fires too often on active repos."
    ),
    "DRIFT_BANNER_MIN_OBSERVATIONS": (
        "Minimum edit_observations count in the 14-day window before the "
        "drift banner is allowed to fire. Stops a single low-confidence edit "
        "from spiking the score and producing a false positive."
    ),
    "DRIFT_BANNER_TTL_SECONDS": (
        "Per-repo cooldown for the drift banner. Once fired, the next banner "
        "for the same repo is suppressed for this many seconds (default 7 days)."
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
