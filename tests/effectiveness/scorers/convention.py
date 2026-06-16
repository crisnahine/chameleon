"""Convention scorer.

Deterministic: chameleon's own lint_file over every TS/Ruby file the session
changed (archetype resolved per file by get_pattern_context against the
worktree's committed, trusted profile), plus the task's plain-Python rubric
checks (placement dir, naming pattern, import style) coded next to the task.

Unscored (with reason) when changed lintable files exist but NONE could be
scored — a fabricated 0 there would read as a perfect score.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from tests.effectiveness.scorers.base import ScoreContext, unscored

_LINTABLE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".rb")


def _pattern_context(path: str) -> dict:
    """Seam: real chameleon call; tests monkeypatch this."""
    from chameleon_mcp.tools import get_pattern_context

    return get_pattern_context(path)


def _lint(*, repo: str, archetype: str, content: str, file_path: str) -> dict:
    """Seam: real chameleon call; tests monkeypatch this."""
    from chameleon_mcp.tools import lint_file

    return lint_file(repo=repo, archetype=archetype, content=content, file_path=file_path)


def _baseline_content(worktree: Path, baseline_sha: str, rel: str) -> str | None:
    """File content at the baseline commit, or None when the file is new / git
    cannot resolve it. Seam: tests monkeypatch this.

    Lets the scorer count only the violations the session INTRODUCED, not the
    pre-existing violations in whatever file it happened to touch. Without this,
    a session that edits a messy file is charged for that file's history, which
    confounds the A/B: the verification arm picking a pre-existing-messy file
    (yup.ts) read as a chameleon regression when it was just file choice.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree), "show", f"{baseline_sha}:{rel}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def _violation_key(row: dict) -> tuple:
    return (row.get("rule"), row.get("expected"), row.get("actual"), row.get("message"))


def _baseline_keys(ctx: ScoreContext, *, rel: str, archetype: str) -> Counter:
    """Multiset of violation keys in the file's BASELINE version, or empty."""
    content = _baseline_content(ctx.worktree, ctx.baseline_sha, rel)
    if content is None:
        return Counter()
    resp = _lint(repo=str(ctx.worktree), archetype=archetype, content=content, file_path=rel)
    data = resp.get("data") or {}
    if data.get("stub"):
        return Counter()
    return Counter(_violation_key(r) for r in (data.get("violations") or []))


def score(ctx: ScoreContext) -> dict:
    lintable = [f for f in ctx.changed_files if f.endswith(_LINTABLE_SUFFIXES)]
    # "violations" = the violations the session INTRODUCED (present now but not in
    # the file's baseline version), not the absolute count, so a change is scored
    # on what it added, not on the chosen file's pre-existing conformance.
    violations = 0
    baseline_total = 0
    current_total = 0
    by_severity: dict[str, int] = {}
    files_scored = 0
    unresolved = 0

    for rel in lintable:
        abs_path = ctx.worktree / rel
        if not abs_path.is_file():
            # Deleted by the session; nothing to lint.
            continue
        arch_resp = _pattern_context(str(abs_path))
        arch = ((arch_resp.get("data") or {}).get("archetype") or {}).get("archetype")
        if not arch:
            unresolved += 1
            continue
        try:
            content = abs_path.read_bytes()[:100_000].decode("utf-8", errors="replace")
        except OSError:
            unresolved += 1
            continue
        lint_resp = _lint(
            repo=str(ctx.worktree), archetype=arch, content=content, file_path=str(abs_path)
        )
        data = lint_resp.get("data") or {}
        if data.get("stub"):
            unresolved += 1
            continue
        rows = data.get("violations") or []
        current_total += len(rows)
        # Subtract the baseline version's violations (greedy multiset match) so
        # only NET-new violations count; pre-existing ones in a touched file do not.
        baseline = _baseline_keys(ctx, rel=rel, archetype=arch)
        baseline_total += sum(baseline.values())
        for row in rows:
            key = _violation_key(row)
            if baseline.get(key, 0) > 0:
                baseline[key] -= 1  # matches a pre-existing violation; not introduced
                continue
            violations += 1
            sev = str(row.get("severity", "warn"))
            by_severity[sev] = by_severity.get(sev, 0) + 1
        files_scored += 1

    if lintable and files_scored == 0:
        return unscored(
            f"no changed file could be scored ({unresolved} unresolved of {len(lintable)})"
        )

    out: dict = {
        "violations": violations,
        "violations_baseline": baseline_total,
        "violations_current": current_total,
        "files_scored": files_scored,
        "files_unresolved": unresolved,
    }
    for sev, count in sorted(by_severity.items()):
        out[f"violations_{sev}"] = count

    rubric = ctx.pack.rubrics.get(ctx.task.task_id)
    if rubric is not None:
        try:
            for key, value in rubric(Path(ctx.worktree)).items():
                out[f"rubric_{key}"] = value
        except Exception as exc:  # noqa: BLE001 - rubric bugs must not sink the cell
            out["rubric_error"] = f"{type(exc).__name__}: {exc}"
    return out
