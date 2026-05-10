"""MCP tool implementations for chameleon.

Phase 2D: most tools wired to real implementations. Stubs remain for
get_archetype + get_canonical_excerpt + get_rules + lint_file +
merge_profiles (these need clustering + lint engine work in Phase 4).

All responses use the API versioning envelope per Round 5 API Designer:
{ "api_version": "1", "data": {...}, "truncated"?: bool, "next_cursor"?: str }
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


def _envelope(data: dict, truncated: bool = False, next_cursor: str | None = None) -> dict:
    """Standard response envelope for all tools."""
    out: dict = {"api_version": "1", "data": data}
    if truncated:
        out["truncated"] = True
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    return out


def _compute_repo_id(repo_root: Path) -> str:
    """Canonical repo_id: sha256 of resolved absolute path. Phase 2C
    simplification; Phase 4 extends with git remote URL detection."""
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()


def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to."""
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({
            "repo_id": None,
            "repo_root": None,
            "profile_status": "no_repo",
            "trust_state": "n/a",
        })

    repo_id = _compute_repo_id(repo_root)
    profile_dir = repo_root / ".chameleon"
    profile_present = (profile_dir / "profile.json").exists()
    trust = trust_state_for(repo_id)
    return _envelope({
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "profile_status": "profile_present" if profile_present else "no_profile",
        "trust_state": "trusted" if trust else "untrusted",
    })


def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Phase 2D: matches by exact path-pattern bucket equality (same logic as
    bootstrap clustering). Phase 4 adds full archetype-match predicate with
    content_signal + AST-shape verification.
    """
    from chameleon_mcp.profile.loader import LoadedProfile, find_repo_root, load_profile_dir
    from chameleon_mcp.signatures import path_pattern_bucket_for

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None or _compute_repo_id(repo_root) != repo:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": None,
            "confidence_band": "low",
        })

    profile_dir = repo_root / ".chameleon"
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": None,
            "confidence_band": "low",
        })

    # Compute the file's bucket via the same function clustering used.
    # Match archetypes by EXACT bucket equality (not substring).
    try:
        rel_str = str(p.relative_to(repo_root))
    except ValueError:
        rel_str = str(p)
    file_bucket = path_pattern_bucket_for(rel_str)

    exact_matches: list[str] = []
    fallback_matches: list[str] = []  # substring fallback if no exact match

    for name, arch in loaded.archetypes.get("archetypes", {}).items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        if pattern == file_bucket:
            exact_matches.append(name)
        elif pattern in rel_str:
            fallback_matches.append(name)

    if exact_matches:
        # Sort by cluster size descending — largest cluster wins ties
        archetypes = loaded.archetypes.get("archetypes", {})
        exact_matches.sort(
            key=lambda n: archetypes.get(n, {}).get("cluster_size", 0),
            reverse=True,
        )
        primary = exact_matches[0]
        alternatives = exact_matches[1:]
        confidence = "high"
    elif fallback_matches:
        primary = fallback_matches[0]
        alternatives = fallback_matches[1:]
        confidence = "low"
    else:
        primary = None
        alternatives = []
        confidence = "low"

    return _envelope({
        "archetype": primary,
        "alternatives": alternatives,
        "content_signal_match": None,
        "confidence_band": confidence,
    })


def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical + rules + meta in one round trip.

    Phase 2D: returns real archetype data when profile is present + trusted.
    """
    from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir
    from chameleon_mcp.profile.trust import trust_state_for

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({
            "repo": {"id": None, "profile_status": "no_repo", "trust_state": "n/a"},
            "archetype": {"name": None, "alternatives": [], "confidence_band": "low"},
            "canonical_excerpt": {"content": "", "witness_path": None, "truncated": False, "sha_hint": None},
            "rules": [],
            "meta": {"mtime_token": None, "computed_at": None},
        })

    repo_id = _compute_repo_id(repo_root)
    profile_dir = repo_root / ".chameleon"
    if not (profile_dir / "profile.json").exists():
        return _envelope({
            "repo": {"id": repo_id, "profile_status": "no_profile", "trust_state": "untrusted"},
            "archetype": {"name": None, "alternatives": [], "confidence_band": "low"},
            "canonical_excerpt": {"content": "", "witness_path": None, "truncated": False, "sha_hint": None},
            "rules": [],
            "meta": {"mtime_token": None, "computed_at": None},
        })

    trust = trust_state_for(repo_id)
    try:
        loaded = load_profile_dir(profile_dir)
    except Exception:
        return _envelope({
            "repo": {"id": repo_id, "profile_status": "profile_present", "trust_state": "trusted" if trust else "untrusted"},
            "archetype": {"name": None, "alternatives": [], "confidence_band": "low"},
            "canonical_excerpt": {"content": "", "witness_path": None, "truncated": False, "sha_hint": None},
            "rules": [],
            "meta": {"mtime_token": None, "computed_at": None},
        })

    # Reuse get_archetype logic
    arch_response = get_archetype(repo_id, file_path)
    arch_data = arch_response["data"]

    canonical_data = {"content": "", "witness_path": None, "truncated": False, "sha_hint": None}
    if arch_data["archetype"]:
        canonicals = loaded.canonicals.get("canonicals", {}).get(arch_data["archetype"], [])
        if canonicals:
            first = canonicals[0]
            witness_rel = first.get("witness", {}).get("path")
            if witness_rel:
                witness_path = repo_root / witness_rel
                if witness_path.is_file():
                    try:
                        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

                        content = witness_path.read_text(errors="replace")
                        # Truncate to ~800 tokens (~3200 chars approx)
                        truncated = len(content) > 3200
                        if truncated:
                            content = content[:3200] + "\n... [truncated]"
                        # Tag-boundary sanitization (Round 4/5 security mitigation)
                        content = sanitize_for_chameleon_context(content)
                        canonical_data = {
                            "content": content,
                            "witness_path": witness_rel,
                            "truncated": truncated,
                            "sha_hint": first.get("witness", {}).get("sha_hint"),
                        }
                    except OSError:
                        pass

    return _envelope({
        "repo": {
            "id": repo_id,
            "profile_status": "profile_present",
            "trust_state": "trusted" if trust else "untrusted",
        },
        "archetype": arch_data,
        "canonical_excerpt": canonical_data,
        "rules": list(loaded.rules.get("rules", {}).items()),
        "meta": {
            "mtime_token": loaded.mtime_token,
            "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    })


def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the annotated canonical excerpt for an archetype."""
    # Phase 2D: caller invokes get_pattern_context for full context;
    # this stub is kept for compatibility.
    return _envelope({
        "content": "",
        "witness_path": None,
        "truncated": False,
        "sha_hint": None,
    })


def get_rules(repo: str, archetype: str | None = None) -> dict:
    """Return rules + citations for repo, filtered by archetype if provided."""
    # Phase 2D: caller invokes get_pattern_context which embeds rules.
    return _envelope({"rules": []})


def lint_file(repo: str, archetype: str, content: str) -> dict:
    """Validate file content against archetype's rules. Phase 4 implements."""
    return _envelope({
        "violations": [],
        "canonical_confidence": 0.0,
        "unparseable_regions": [],
    })


def get_drift_status(repo: str) -> dict:
    """Report freshness, days_since_refresh, observed_drift_score for a repo.

    Phase 2D: reads profile.json `created_at`. Drift confidence requires
    edit_observations table (Phase 4 wires hook → drift.db updates).
    """
    from chameleon_mcp.profile.loader import LoadedProfile, find_repo_root, load_profile_dir

    # Phase 2D simplification: caller passes repo_id; we look up via env walk.
    # Real Phase 4: index.db lookup via repo_id.
    del repo  # not used in Phase 2D simplification
    return _envelope({
        "days_since_refresh": None,
        "observed_drift_score": None,
        "recommended_action": "Phase 2D simplification — pass file_path via detect_repo first",
    })


def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile.

    Phase 2D: simplification — call bootstrap_repo on the repo's root.
    Phase 4 implements true incremental refresh + flock-protected updates.
    """
    # Phase 2D: caller is expected to pass an absolute path as `repo` since
    # we don't yet have an index.db that maps repo_id → path.
    del force
    repo_path = Path(repo)
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({
            "status": "failed",
            "error": "Phase 2D refresh_repo expects absolute repo path; index.db lookup is Phase 4",
        })
    return bootstrap_repo(str(repo_path))


def bootstrap_repo(path: str, mode: str = "full", paths_glob: str | None = None) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit."""
    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap

    repo_root = Path(path).expanduser().resolve()
    if not repo_root.is_dir():
        return _envelope({
            "status": "failed",
            "error": f"path is not a directory: {path}",
        })
    del mode  # forward-compat for Phase 2D interview mode
    report = _bootstrap(repo_root, paths_glob=paths_glob)
    return _envelope(report.to_dict())


def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos. Phase 4 reads from index.db.

    Phase 2D: returns empty list (index.db not yet wired); cursor pagination
    is the API shape, not implementation.
    """
    del cursor, limit
    return _envelope({"profiles": []}, next_cursor=None)


def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge for git merge driver use. Phase 4 implements."""
    del repo, base, ours, theirs
    return _envelope({
        "status": "stub",
        "merged_profile_path": None,
        "summary_path": None,
        "note": "Phase 4 implementation pending",
    })


def teach_profile(repo: str, feedback: str) -> dict:
    """Append a captured idiom to .chameleon/idioms.md.

    Phase 2D: simple append-as-active-idiom. Phase 4 adds:
    - feedback sanitization (strip ANSI/zero-width)
    - 50KB cap on idioms.md
    - structured idiom entries with deprecation tracking
    - material-change re-prompt for trust
    """
    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({"status": "failed", "error": "expected absolute repo path"})

    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope({"status": "failed", "error": "no profile in this repo (run /chameleon-init)"})

    # Strip ANSI escapes and zero-width chars (Phase 2D minimal sanitization)
    sanitized = _sanitize_user_input(feedback)
    if len(sanitized) > 50_000:
        return _envelope({"status": "failed", "error": "feedback exceeds 50KB cap"})

    # Append as a new active idiom block
    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    addition = f"\n### idiom-{timestamp}-{int(time.time())}\nStatus: active (added {timestamp})\n{sanitized}\n"

    # Read current content; insert under "## active" header
    current = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else "# idioms\n\n## active\n\n## deprecated\n"
    if "## active" in current:
        new_content = current.replace("## active\n", f"## active\n{addition}", 1)
    else:
        new_content = current + addition
    idioms_path.write_text(new_content, encoding="utf-8")

    return _envelope({
        "status": "success",
        "idioms_added": 1,
        "idioms_deprecated": 0,
    })


def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user.

    Phase 2D: validates `confirmation_token` matches the repo's basename
    (typed repo name) or `yes-trust-<repo_id_short>`. Writes .trust file.
    """
    from chameleon_mcp.profile.trust import grant_trust

    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({"status": "failed", "error": "expected absolute repo path"})

    profile_dir = repo_path / ".chameleon"
    if not (profile_dir / "profile.json").is_file():
        return _envelope({"status": "failed", "error": "no profile to trust (run /chameleon-init first)"})

    repo_id = _compute_repo_id(repo_path)
    expected_short = repo_id[:8]

    if confirmation_token != repo_path.name and confirmation_token != f"yes-trust-{expected_short}":
        return _envelope({
            "status": "failed",
            "error": (
                f"confirmation_token must be the repo name {repo_path.name!r} "
                f"or yes-trust-{expected_short}"
            ),
        })

    record = grant_trust(repo_id, profile_dir)
    return _envelope({
        "status": "success",
        "trusted_at": record.granted_at,
        "granted_by_user": record.granted_by_user,
    })


def _sanitize_user_input(text: str) -> str:
    """Strip ANSI escapes and zero-width unicode characters.

    Phase 2D minimal sanitization. Phase 4 adds full Round 5 AppSec hardening
    (stricter regex, BiDi attack detection, etc.).
    """
    import re

    # ANSI CSI/OSC escapes
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b\][^\x07]*\x07?", "", text)
    # Zero-width characters (U+200B, U+200C, U+200D, U+FEFF)
    text = re.sub(r"[​-‍﻿]", "", text)
    return text
