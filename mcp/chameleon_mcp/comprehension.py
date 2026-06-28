"""Codebase comprehension over chameleon's committed profile artifacts.

chameleon derives a repo's conventions for CONFORMANCE (shape new edits to match
the repo). The same committed artifacts -- ``symbol_signatures`` (every callable's
name + location + signature), ``calls_index`` (callee -> callers), ``archetypes``,
and ``canonicals`` -- also answer COMPREHENSION questions an assistant asks about
code that already exists: "where is X defined", "what is this codebase", "what
does this function call". This module turns those deterministic indexes into a
queryable comprehension surface, so chameleon does both conformance AND
comprehension off ONE profile, offline and with no repo-code execution.

Everything here is read-only over committed artifacts, bounded, and fails open
(an absent or unreadable artifact yields an empty answer, never an exception). It
is the pull-side counterpart to chameleon's push-side conformance injection.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp._thresholds import threshold_int


def _signature_string(name: str, row: dict, rel: str) -> str:
    """Compact ``name(params): ret -- path`` for a signature row, or the bare
    name if rendering fails."""
    from chameleon_mcp.symbol_signatures import render_imported_definition

    try:
        return render_imported_definition(name, row, rel)
    except Exception:
        return name


def search_symbols(repo_root, query: str, *, limit: int) -> list[dict]:
    """Rank symbols whose name or file matches ``query``, most-central first.

    Walks ``symbol_signatures`` (every recorded callable across all languages) and
    tiers each match: exact name > name prefix > name substring > all query tokens
    present (name or path) > file-path substring. Ties break by how many callers
    the symbol has in ``calls_index`` (a more-called symbol is more central), then
    by ``(file, name)`` for determinism. Returns up to ``limit`` dicts
    ``{name, file, line, signature, callers}``. Empty on no index / no match /
    blank query. The "where is X / find Y" comprehension primitive.
    """
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.symbol_signatures import load_symbol_signatures
    from chameleon_mcp.worktree import resolve_profile_root

    q = (query or "").strip().lower()
    if not q:
        return []
    profile_root = resolve_profile_root(Path(repo_root))
    sigs = load_symbol_signatures(profile_root)
    if sigs is None or len(sigs) == 0:
        return []
    calls = load_calls_index(profile_root)
    qtokens = q.split()

    candidates: list[tuple] = []  # (tier, callers, rel, name, row)
    for rel, names in sigs.items():
        pl = rel.lower()
        path_hit = q in pl
        for name, row in names.items():
            nl = name.lower()
            if nl == q:
                tier = 5
            elif nl.startswith(q):
                tier = 4
            elif q in nl:
                tier = 3
            elif len(qtokens) > 1 and all(t in nl or t in pl for t in qtokens):
                tier = 2
            elif path_hit:
                tier = 1
            else:
                continue
            callers = 0
            if calls is not None:
                entry = calls.callers_of(rel, name)
                if entry:
                    callers = entry.get("total", 0)
            candidates.append((tier, callers, rel, name, row))

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3]))
    out: list[dict] = []
    for _tier, callers, rel, name, row in candidates[: max(0, int(limit))]:
        out.append(
            {
                "name": name,
                "file": rel,
                "line": row.get("start_line"),
                "signature": _signature_string(name, row, rel),
                "callers": callers,
            }
        )
    return out


def _is_test_path(rel: str) -> bool:
    """Heuristic: does ``rel`` look like a test file? Used to keep the codebase
    overview's god symbols focused on production architecture (a test-heavy repo
    would otherwise rank test helpers as its most-called functions)."""
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    return (
        low.startswith(("test/", "tests/", "spec/", "__tests__/"))
        or "/tests/" in low
        or "/test/" in low
        or "/__tests__/" in low
        or "/spec/" in low
        or base.startswith("test_")
        or base.startswith("test-")
        or base.endswith(("_test.py", "_test.rb", "_test.go", "_spec.rb"))
        or any(s in base for s in (".test.", ".spec."))
    )


def god_symbols(repo_root, *, limit: int, exclude_tests: bool = True) -> list[dict]:
    """The most-called symbols in the repo (its "god nodes"): the functions the
    rest of the codebase depends on most, ranked by caller count from
    ``calls_index``. Test files are excluded by default so the overview shows
    production architecture, not test helpers. Deterministic. Empty on no index."""
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.worktree import resolve_profile_root

    calls = load_calls_index(resolve_profile_root(Path(repo_root)))
    if calls is None:
        return []
    ranked: list[tuple] = []  # (count, rel, name)
    for rel, names in calls.items():
        if exclude_tests and _is_test_path(rel):
            continue
        for name, entry in names.items():
            if not isinstance(entry, dict):
                continue
            if exclude_tests:
                # Count only production callers, so a symbol used mostly by tests
                # is not ranked as production-central. (Recorded callers, which
                # may undercount a truncated high-caller list -- acceptable for a
                # ranking signal.)
                count = sum(
                    1
                    for c in (entry.get("callers") or [])
                    if isinstance(c, dict) and not _is_test_path(str(c.get("path") or ""))
                )
            else:
                count = entry.get("total", 0)
            if count > 0:
                ranked.append((count, rel, name))
    ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [
        {"name": name, "file": rel, "callers": total}
        for total, rel, name in ranked[: max(0, int(limit))]
    ]


def describe_codebase(repo_root) -> dict:
    """A structural overview of the repo from its profile: language, framework,
    archetypes (kinds of files + their canonical witness + size), totals, and the
    god symbols. The "what is this codebase" comprehension answer. Returns the
    empty-shaped dict (never raises) when no profile is present."""
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.symbol_signatures import load_symbol_signatures
    from chameleon_mcp.worktree import resolve_profile_root

    profile_root = resolve_profile_root(Path(repo_root))
    out: dict = {
        "language": None,
        "framework": None,
        "archetypes": [],
        "file_count": 0,
        "symbol_count": 0,
        "god_symbols": [],
    }
    try:
        lp = load_profile_dir(Path(profile_root) / ".chameleon")
    except Exception:
        return out
    prof = lp.profile if isinstance(lp.profile, dict) else {}
    out["language"] = prof.get("language")
    out["framework"] = prof.get("framework")
    arch = lp.archetypes.get("archetypes", {}) if isinstance(lp.archetypes, dict) else {}
    canon = lp.canonicals.get("canonicals", {}) if isinstance(lp.canonicals, dict) else {}
    archetypes: list[dict] = []
    for name, body in arch.items():
        if not isinstance(body, dict):
            continue
        witness = None
        rows = canon.get(name)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            w = rows[0].get("witness")
            if isinstance(w, dict):
                witness = w.get("path")
        archetypes.append(
            {
                "name": name,
                "summary": body.get("summary"),
                "size": body.get("cluster_size"),
                "paths": body.get("paths_pattern_display") or body.get("paths_pattern"),
                "witness": witness,
            }
        )
    archetypes.sort(key=lambda a: (-(a["size"] or 0), a["name"]))
    out["archetypes"] = archetypes
    sigs = load_symbol_signatures(profile_root)
    if sigs is not None:
        out["file_count"] = len(sigs)
        out["symbol_count"] = sum(len(v) for _, v in sigs.items())
    out["god_symbols"] = god_symbols(repo_root, limit=threshold_int("COMPREHEND_GOD_SYMBOLS"))
    return out


def callees_of(repo_root, file_rel: str, name: str) -> list[dict]:
    """Forward call edges: what ``name`` defined in ``file_rel`` calls, derived by
    inverting the reverse ``calls_index``. The forward counterpart to
    ``callers_of`` / ``get_blast_radius``. Deduped, deterministic order. Empty on
    no index / no recorded edge. Same honesty posture as the reverse views: a
    committed snapshot with deterministic grades only, absence is not proof."""
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.worktree import resolve_profile_root

    calls = load_calls_index(resolve_profile_root(Path(repo_root)))
    if calls is None or not file_rel or not name:
        return []
    seen: set[tuple] = set()
    out: list[dict] = []
    for callee_rel, names in calls.items():
        for callee_name, entry in names.items():
            if not isinstance(entry, dict):
                continue
            for row in entry.get("callers", []):
                if row.get("path") == file_rel and row.get("caller") == name:
                    key = (callee_rel, callee_name)
                    if key not in seen:
                        seen.add(key)
                        out.append(
                            {"callee": callee_name, "file": callee_rel, "grade": row.get("grade")}
                        )
    out.sort(key=lambda c: (c["file"], c["callee"]))
    return out
