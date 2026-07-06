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
    """Compact ``name(params): ret -- path`` for a callable signature row, or
    ``class Name(Base) -- path`` for a class/module row, or the bare name if
    rendering fails."""
    if isinstance(row, dict) and row.get("kind") == "class":
        base = row.get("extends")
        keyword = row.get("keyword") if row.get("keyword") in ("module", "class") else "class"
        head = (
            f"{keyword} {name}({base})" if isinstance(base, str) and base else f"{keyword} {name}"
        )
        return f"{head} — {rel}"
    from chameleon_mcp.symbol_signatures import render_imported_definition

    try:
        return render_imported_definition(name, row, rel)
    except Exception:
        return name


def _match_tier(q: str, qtokens: list[str], nl: str, pl: str, path_hit: bool) -> int | None:
    """Rank how well a symbol name ``nl`` (in file ``pl``) matches query ``q``:
    exact > prefix > substring > all-tokens-present > file-path, else None."""
    if nl == q:
        return 5
    if nl.startswith(q):
        return 4
    if q in nl:
        return 3
    if len(qtokens) > 1 and all(t in nl or t in pl for t in qtokens):
        return 2
    if path_hit:
        return 1
    return None


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
    # len(sigs) counts CALLABLE files only; a repo whose files carry classes but
    # no recorded callables (e.g. Ruby module/DSL files, bare dataclasses) has an
    # empty callable map yet real class definitions, so also proceed when the
    # class section is non-empty -- otherwise class search would silently return
    # nothing while the empty-result note claims classes are covered.
    if sigs is None or (len(sigs) == 0 and not any(True for _ in sigs.class_items())):
        return []
    calls = load_calls_index(profile_root)
    qtokens = q.split()

    candidates: list[tuple] = []  # (tier, callers, rel, name, row)
    for rel, names in sigs.items():
        pl = rel.lower()
        path_hit = q in pl
        for name, row in names.items():
            tier = _match_tier(q, qtokens, name.lower(), pl, path_hit)
            if tier is None:
                continue
            callers = 0
            if calls is not None:
                entry = calls.callers_of(rel, name)
                if entry:
                    callers = entry.get("total", 0)
            candidates.append((tier, callers, rel, name, row))

    # Class/module definitions from the additive class section, so "find class X"
    # resolves even when the class is never instantiated. This runs BEFORE the
    # calls_index fallback so a class that IS instantiated still gets its rich
    # class row (real def line + `class X(Base)` shape) rather than the fallback's
    # minimal line-less callee row. Deduped against callable matches on (rel, name).
    seen_cls = {(rel, name) for _t, _c, rel, name, _r in candidates}
    for rel, cnames in sigs.class_items():
        pl = rel.lower()
        path_hit = q in pl
        for name, crow in cnames.items():
            if (rel, name) in seen_cls or not isinstance(crow, dict):
                continue
            tier = _match_tier(q, qtokens, name.lower(), pl, path_hit)
            if tier is None:
                continue
            callers = 0
            if calls is not None:
                entry = calls.callers_of(rel, name)
                if entry:
                    callers = entry.get("total", 0)
            crow_out: dict = {"start_line": crow.get("start_line"), "kind": "class"}
            ext = crow.get("extends")
            if isinstance(ext, str) and ext:
                crow_out["extends"] = ext
            kw = crow.get("keyword")
            if kw in ("module", "class"):
                crow_out["keyword"] = kw
            candidates.append((tier, callers, rel, name, crow_out))
            seen_cls.add((rel, name))

    # symbol_signatures indexes CALLABLES + (now) classes, but calls_index also
    # records classes/constants as callees. Without this fallback a callee
    # surfaced in the overview (e.g. a god-symbol) that is neither a callable nor
    # a recorded class definition is unfindable here, so the two comprehension
    # tools disagree on the symbol universe. Add matching calls_index callee names
    # not already covered (minimal row: no def line, but a real callers count).
    if calls is not None:
        seen = {(rel, name) for _t, _c, rel, name, _r in candidates}
        for rel, names in calls.items():
            pl = rel.lower()
            path_hit = q in pl
            for name, entry in names.items():
                if (rel, name) in seen:
                    continue
                tier = _match_tier(q, qtokens, name.lower(), pl, path_hit)
                if tier is None:
                    continue
                callers = entry.get("total", 0) if isinstance(entry, dict) else 0
                candidates.append((tier, callers, rel, name, {}))
                seen.add((rel, name))

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
    from chameleon_mcp.calls_index import load_calls_index
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
    lp = None
    try:
        lp = load_profile_dir(Path(profile_root) / ".chameleon")
    except Exception:
        # The profile bundle failed cross-artifact validation (a generation
        # mismatch, a corrupt/merge-mangled sibling). Do NOT return an empty
        # overview: symbol_signatures / calls_index are INDEPENDENT artifacts that
        # search_codebase reads directly, so a bundle failure would otherwise make
        # describe report a populated repo as an empty codebase, contradicting
        # search over the same profile. Fall through, mark the overview degraded,
        # and still report the real file/symbol totals + god symbols below.
        out["degraded"] = True
    if lp is not None:
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
        # The signatures artifact caps at DUPLICATION_CATALOG_MAX_FILES files, so
        # on a repo above the cap len(sigs) is the cap value, not the repo's real
        # file total -- reporting it bare read as truth (e.g. file_count 8000 on a
        # 30k-file monolith). At the cap the count is a floor: flag it truncated
        # and degraded so the "what is this codebase" answer never states a capped
        # number as the whole picture. (A repo with exactly the cap many
        # signature-bearing files trips this too; degraded is the safe direction.)
        if len(sigs) >= threshold_int("DUPLICATION_CATALOG_MAX_FILES"):
            out["truncated"] = True
            out["degraded"] = True
    else:
        # load_symbol_signatures returns None for absent, corrupt, AND
        # schema-stale (a profile built before this artifact, or one whose
        # schema the current loader gates out). In every one of those the zero
        # file_count/symbol_count is UNKNOWN, not a verified empty repo, so mark
        # the overview degraded. The absent case (an older-engine profile that
        # never built the artifact) is the common one after an upgrade; without
        # this it reported clean zeros next to populated archetypes.
        out["degraded"] = True
    out["god_symbols"] = god_symbols(repo_root, limit=threshold_int("COMPREHEND_GOD_SYMBOLS"))
    if load_calls_index(profile_root) is None:
        # Same honesty posture as the symbol_signatures branch: an absent,
        # corrupt, or schema-stale calls_index makes god_symbols silently return
        # [] (and zeroes every caller count search_codebase surfaces), so mark
        # the overview degraded rather than reporting a verified call-graph-free
        # repo. Absent-vs-damaged is not distinguished here; both leave the
        # call-graph reads blind.
        out["degraded"] = True
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
