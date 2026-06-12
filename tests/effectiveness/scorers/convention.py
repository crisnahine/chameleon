"""Convention scorer.

Deterministic: chameleon's own lint_file over every TS/Ruby file the session
changed (archetype resolved per file by get_pattern_context against the
worktree's committed, trusted profile), plus the task's plain-Python rubric
checks (placement dir, naming pattern, import style) coded next to the task.

Unscored (with reason) when changed lintable files exist but NONE could be
scored — a fabricated 0 there would read as a perfect score.
"""

from __future__ import annotations

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


def score(ctx: ScoreContext) -> dict:
    lintable = [f for f in ctx.changed_files if f.endswith(_LINTABLE_SUFFIXES)]
    violations = 0
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
        violations += len(rows)
        for row in rows:
            sev = str(row.get("severity", "warn"))
            by_severity[sev] = by_severity.get(sev, 0) + 1
        files_scored += 1

    if lintable and files_scored == 0:
        return unscored(
            f"no changed file could be scored ({unresolved} unresolved of {len(lintable)})"
        )

    out: dict = {
        "violations": violations,
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
