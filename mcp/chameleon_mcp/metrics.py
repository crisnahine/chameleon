"""Lightweight per-hook-call metrics emitter.

Appends one JSON line to ${PLUGIN_DATA}/metrics.jsonl. Rotated by
log_rotation when it crosses the size threshold. All errors swallowed
so emission failure never breaks the hook.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    from chameleon_mcp.log_rotation import rotate_if_needed
except ImportError:

    def rotate_if_needed(_path: Path) -> None:  # noqa: ARG001
        return None


def _metrics_path() -> Path:
    """Resolve metrics.jsonl path; honors CHAMELEON_PLUGIN_DATA."""
    base = os.environ.get("CHAMELEON_PLUGIN_DATA")
    if base:
        return Path(base) / "metrics.jsonl"
    return Path.home() / ".local" / "share" / "chameleon" / "metrics.jsonl"


def emit_hook_metric(
    hook: str,
    *,
    elapsed_ms: int,
    repo_id: str | None,
    advisory_emitted: bool,
    suppression_reason: str | None = None,
    fail_open: bool = False,
    trust_state: str | None = None,
    archetype: str | None = None,
    confidence: str | None = None,
    would_block: bool = False,
) -> None:
    """Append one metrics line. Best-effort; never raises."""
    try:
        path = _metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_needed(path)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": hook,
            "repo_id": repo_id,
            "elapsed_ms": int(elapsed_ms),
            "advisory_emitted": bool(advisory_emitted),
            "suppression_reason": suppression_reason,
            "fail_open": bool(fail_open),
            "trust_state": trust_state,
            "archetype": archetype,
            "confidence": confidence,
            "would_block": bool(would_block),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    except Exception:
        return
