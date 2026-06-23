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
    rule: str | None = None,
    file_rel: str | None = None,
    line: int | None = None,
    override: bool = False,
    session_id: str | None = None,
) -> None:
    """Append one metrics line. Best-effort; never raises.

    ``rule``, ``file_rel``, and ``line`` attribute a would_block row to the
    specific convention rule and the repo-relative file (and line, when known)
    that triggered it. The shadow report groups would_block rows by ``rule`` and
    samples ``file_rel:line`` for human spot-check, so these are populated at the
    rule-bearing block gates and left None elsewhere.

    ``override`` marks a row emitted where an inline ``chameleon-ignore``
    directive dropped a block-eligible rule. It pairs with ``rule`` to track how
    often each rule gets overridden vs would-block; the durable per-repo tally
    lives in drift.db, this row is the same write-path counter as would_block.

    ``session_id`` is the Claude Code session that produced the row. The shadow
    report counts distinct sessions per rule from it; a row without one is not
    attributed to any session rather than being mistaken for its own session, so
    distinct-session counts never silently collapse onto distinct-file counts.
    """
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
            "rule": rule,
            "file_rel": file_rel,
            "line": int(line) if isinstance(line, int) else None,
            "override": bool(override),
            "session_id": session_id,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
    except Exception:
        return
