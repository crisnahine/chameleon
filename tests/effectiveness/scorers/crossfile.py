"""Crossfile scorer.

(a) Broken-export count: chameleon's get_crossfile_context over the FINAL
    tree; only high_confidence findings count (the tool's own consumption
    rule). TS-only by design — on Rails worktrees the tool reports
    found=False with a reason, recorded as broken_exports_unscored.
(b) Callers-updated: for the task's declared target function, every caller
    site recorded in the calls_index AT THE BASELINE COMMIT (git show, so a
    session that edits .chameleon cannot move the goalposts) must reference
    the new name in the final tree. Word-bounded grep — deterministic, no
    parser, mirroring the tool's own presence checks.

Fully unscored only when the caller half was requested but the baseline
calls_index lacks the target (spec: missing calls_index -> unscored with
reason).
"""

from __future__ import annotations

import json
import re

from tests.effectiveness.scorers.base import ScoreContext, unscored
from tests.journey.harness.bash import run_bash


def _crossfile_context(repo_path: str) -> dict:
    """Seam: real chameleon call; tests monkeypatch this."""
    from chameleon_mcp.tools import get_crossfile_context

    return get_crossfile_context(repo_path)


def _word_re(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")


def score(ctx: ScoreContext) -> dict:
    out: dict = {}

    resp = _crossfile_context(str(ctx.worktree))
    data = resp.get("data") or {}
    if data.get("found"):
        out["broken_exports"] = sum(
            1 for f in (data.get("findings") or []) if f.get("high_confidence")
        )
    else:
        out["broken_exports_unscored"] = str(
            data.get("reason") or data.get("status") or "unavailable"
        )

    target = ctx.pack.crossfile_targets.get(ctx.task.task_id)
    if target is not None:
        r = run_bash(
            f"git show {ctx.baseline_sha}:.chameleon/calls_index.json",
            cwd=ctx.worktree,
        )
        entry = None
        if r.returncode == 0:
            try:
                idx = json.loads(r.stdout)
                entry = ((idx.get("callees") or {}).get(target["module"]) or {}).get(
                    target["function"]
                )
            except (ValueError, TypeError):
                entry = None
        if entry is None or not isinstance(entry.get("callers"), list):
            return unscored(
                f"calls_index at baseline lacks target {target['module']}::{target['function']}"
            )
        new_name_re = _word_re(target["new_name"])
        old_name_re = _word_re(target["function"])
        updated = 0
        stale = 0
        for row in entry["callers"]:
            site = ctx.worktree / str(row.get("path", ""))
            try:
                text = site.read_bytes()[:1_000_000].decode("utf-8", errors="replace")
            except OSError:
                text = ""
            if new_name_re.search(text) and not old_name_re.search(text):
                updated += 1
            else:
                stale += 1
        out["callers_total"] = len(entry["callers"])
        out["callers_updated"] = updated
        out["callers_stale"] = stale

    if not out:
        return unscored("neither breakage check nor caller target available")
    return out
