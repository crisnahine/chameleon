"""MCP tool implementations for chameleon.

Phase 1C: stubs returning hardcoded valid-shape responses. The shapes
match ARCHITECTURE.md "MCP server" tool table contracts.

Real implementations land in:
- Phase 2: detect_repo, get_archetype, get_canonical_excerpt, get_rules,
  lint_file, get_drift_status, refresh_repo, bootstrap_repo, list_profiles,
  teach_profile (need extractor + bootstrap engine + sqlite)
- Phase 4: trust_profile (cooldown + frequency limits), merge_profiles
  (deterministic algorithm), get_pattern_context (collapsed call coordinator)

All responses use the API versioning envelope per Round 5 API Designer #3:
{ "api_version": "1", "data": {...}, "truncated"?: bool, "next_cursor"?: str }
"""

from __future__ import annotations

# Phase 1C: every tool is a stub. Real implementations import from the
# corresponding submodule (extractors/, bootstrap/, profile/, drift/, etc.)


def _envelope(data: dict, truncated: bool = False, next_cursor: str | None = None) -> dict:
    """Standard response envelope for all tools.

    Adopted now (Phase 1C) so we never need to retrofit it later.
    """
    out: dict = {"api_version": "1", "data": data}
    if truncated:
        out["truncated"] = True
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    return out


def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to."""
    # TODO Phase 2: walk up file_path looking for .git directory
    # TODO Phase 2: compute repo_id = sha256(canonicalize(git_remote_url) || canonicalize_path(repo_root))
    # TODO Phase 2: check ${PLUGIN_DATA}/<repo_id>/.trust file
    # TODO Phase 2: check <repo>/.chameleon/profile.json + COMMITTED sentinel
    return _envelope({
        "repo_id": "stub-not-yet-implemented",
        "profile_status": "no_profile",
        "trust_state": "n/a",
    })


def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches."""
    # TODO Phase 2: load archetypes.json, run archetype-match predicate
    # TODO Phase 2: return name + alternatives + content_signal_match
    return _envelope({
        "archetype": None,
        "alternatives": [],
        "content_signal_match": None,
        "confidence_band": "low",
    })


def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical + rules + meta in one round trip.

    Round 5 API Designer #2 recommendation: replaces v3 4-call dance.
    """
    # TODO Phase 2: coordinate detect_repo + get_archetype + get_canonical_excerpt + get_rules
    # TODO Phase 2: include meta.mtime_token for hook-side cache invalidation
    return _envelope({
        "repo": {
            "id": "stub-not-yet-implemented",
            "profile_status": "no_profile",
            "trust_state": "n/a",
        },
        "archetype": {
            "name": None,
            "alternatives": [],
            "confidence_band": "low",
        },
        "canonical_excerpt": {
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        },
        "rules": [],
        "meta": {
            "mtime_token": None,
            "computed_at": None,
        },
    })


def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the annotated canonical excerpt for an archetype."""
    # TODO Phase 2: load canonicals.json, fetch witness file content via safe_open
    # TODO Phase 4: tag-boundary sanitize injected content (escape </chameleon-context>)
    # TODO Phase 4: secret-scan + injection-scan before returning
    return _envelope({
        "content": "",
        "witness_path": None,
        "truncated": False,
        "sha_hint": None,
    })


def get_rules(repo: str, archetype: str | None = None) -> dict:
    """Return rules + citations for repo, filtered by archetype if provided."""
    # TODO Phase 2: load rules.json, filter by archetype if specified
    return _envelope({"rules": []})


def lint_file(repo: str, archetype: str, content: str) -> dict:
    """Validate file content against archetype's rules."""
    # TODO Phase 2: parse content via TS Compiler API, run rule checks
    # TODO Phase 4: enforce 100KB content cap + 50k AST node ceiling
    # TODO Phase 4: return partial results with truncated flag if caps hit
    return _envelope({
        "violations": [],
        "canonical_confidence": 0.0,
        "unparseable_regions": [],
    })


def get_drift_status(repo: str) -> dict:
    """Report freshness, days_since_refresh, observed_drift_score for a repo."""
    # TODO Phase 2: query drift.db (WAL + busy_timeout configured)
    # TODO Phase 2: read manifest.last_analyzed
    return _envelope({
        "days_since_refresh": None,
        "observed_drift_score": None,
        "recommended_action": "run /chameleon-init to bootstrap",
    })


def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile. OS-level locked via flock."""
    # TODO Phase 2: acquire flock on .chameleon/.refresh.lock
    # TODO Phase 2: incremental algorithm (recompute-all-from-cached-signatures)
    # TODO Phase 2: atomic commit via .tmp/<txn-id>/COMMITTED sentinel
    return _envelope({
        "status": "stub",
        "files_processed": 0,
        "archetypes_changed": 0,
        "duration_ms": 0,
    })


def bootstrap_repo(path: str, mode: str = "full", paths_glob: str | None = None) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit.

    Phase 2B: non-interactive bootstrap producing a working profile with
    auto-generated archetype names (cluster-<hash>). Phase 2D wraps this
    with the ≤3-prompt interview to rename archetypes and collect idioms.
    """
    from pathlib import Path

    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap

    repo_root = Path(path).expanduser().resolve()
    if not repo_root.is_dir():
        return _envelope({
            "status": "failed",
            "error": f"path is not a directory: {path}",
        })

    # mode kept for forward-compat (Phase 2D adds "interview" mode); Phase 2B
    # always runs non-interactively.
    del mode

    report = _bootstrap(repo_root, paths_glob=paths_glob)
    return _envelope(report.to_dict())


def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos. Cursor-paginated from day 1 (Round 5 API #6)."""
    # TODO Phase 2: query index.db for repos sorted by last_seen_at desc
    # TODO Phase 2: return up to `limit` repos + next_cursor if more
    return _envelope({"profiles": []}, next_cursor=None)


def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge for git merge driver use."""
    # TODO Phase 4: re-cluster from union of ours + theirs file sets
    # TODO Phase 4: deterministic tie-breaking (lexicographic + recency-weighted cluster_size)
    # TODO Phase 4: idiom merge (deprecation-aware)
    # TODO Phase 4: write reproposed profile to repo for user review via profile.summary.md
    return _envelope({
        "status": "stub",
        "merged_profile_path": None,
        "summary_path": None,
    })


def teach_profile(repo: str, feedback: str) -> dict:
    """Apply user-driven correction to profile (idiom, banned import, etc.)."""
    # TODO Phase 2: sanitize feedback (strip ANSI/zero-width, 50KB cap)
    # TODO Phase 2: append to idioms.md or update canonicals/rules
    # TODO Phase 4: deprecation tracking on existing idioms
    return _envelope({
        "status": "stub",
        "idioms_added": 0,
        "idioms_deprecated": 0,
    })


def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user."""
    # TODO Phase 2: validate confirmation_token matches typed repo name or yes-trust-<repo_id_short>
    # TODO Phase 2: write .trust file with granted_at + granted_by_user + profile_sha256
    # TODO Phase 4: cooldown + frequency limits (max 3 fresh trusts/hour, 10/day)
    return _envelope({
        "status": "stub",
        "trusted_at": None,
    })
