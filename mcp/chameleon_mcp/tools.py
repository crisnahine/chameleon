"""MCP tool implementations for chameleon.

Phase 2D: most tools wired to real implementations. Stubs remain for
get_archetype + get_canonical_excerpt + get_rules + lint_file +
merge_profiles (these need clustering + lint engine work in Phase 4).

All responses use the API versioning envelope per Round 5 API Designer:
{ "api_version": "1", "data": {...}, "truncated"?: bool, "next_cursor"?: str }
"""

from __future__ import annotations

import hashlib
import json
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
    """Detect the repo a given file path belongs to.

    trust_state values:
    - "n/a"        — no repo root detected
    - "untrusted"  — repo found, no .trust record
    - "trusted"    — .trust record exists AND profile hash matches
    - "stale"      — .trust record exists but profile changed since grant;
                     user must re-confirm via /chameleon-trust before
                     chameleon resumes injection
    """
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import is_material_change, trust_state_for

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

    if trust is None:
        trust_state = "untrusted"
    elif profile_present and is_material_change(repo_id, profile_dir):
        trust_state = "stale"
    else:
        trust_state = "trusted"

    return _envelope({
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "profile_status": "profile_present" if profile_present else "no_profile",
        "trust_state": trust_state,
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

    from chameleon_mcp.profile.trust import is_material_change
    trust = trust_state_for(repo_id)
    if trust is None:
        trust_state_str = "untrusted"
    elif is_material_change(repo_id, profile_dir):
        trust_state_str = "stale"
    else:
        trust_state_str = "trusted"

    try:
        loaded = load_profile_dir(profile_dir)
    except Exception:
        return _envelope({
            "repo": {"id": repo_id, "profile_status": "profile_present", "trust_state": trust_state_str},
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

    # Surface team idioms (captured via /chameleon-teach) — sanitized + capped.
    # The using-chameleon skill says "shape your output using archetype,
    # canonical, rules, AND idioms"; without this field, captured idioms
    # never reach the model.
    idioms_text = loaded.idioms_text or ""
    if idioms_text:
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context
        idioms_text = sanitize_for_chameleon_context(idioms_text)
        # Cap at 8000 chars (~2000 tokens) to bound prompt size; idioms.md
        # has its own 50KB cap so this is a defense-in-depth ceiling.
        if len(idioms_text) > 8000:
            idioms_text = idioms_text[:8000] + "\n... [truncated]"

    return _envelope({
        "repo": {
            "id": repo_id,
            "profile_status": "profile_present",
            "trust_state": trust_state_str,
        },
        "archetype": arch_data,
        "canonical_excerpt": canonical_data,
        "rules": list(loaded.rules.get("rules", {}).items()),
        "idioms": idioms_text,
        "meta": {
            "mtime_token": loaded.mtime_token,
            "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    })


def _resolve_repo_root_by_id(repo_id: str) -> Path | None:
    """Map a repo_id back to its repo_root via the trust record.

    Returns None if no trust record exists or the recorded repo_root is gone.
    Phase 2D: trust record is the only stored repo_id → repo_root map.
    """
    from chameleon_mcp.profile.trust import trust_state_for

    record = trust_state_for(repo_id)
    if record is None or not record.repo_root:
        return None
    p = Path(record.repo_root)
    return p if p.is_dir() else None


def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the annotated canonical excerpt for an archetype."""
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    repo_root = _resolve_repo_root_by_id(repo)
    if repo_root is None:
        return _envelope({
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })

    try:
        loaded = load_profile_dir(repo_root / ".chameleon")
    except Exception:
        return _envelope({
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })

    canonicals = loaded.canonicals.get("canonicals", {}).get(archetype, [])
    if not canonicals:
        return _envelope({
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })

    first = canonicals[0]
    witness = first.get("witness", {}) or {}
    witness_rel = witness.get("path")
    if not witness_rel:
        return _envelope({
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": witness.get("sha_hint"),
        })

    witness_path = repo_root / witness_rel
    if not witness_path.is_file():
        return _envelope({
            "content": "",
            "witness_path": witness_rel,
            "truncated": False,
            "sha_hint": witness.get("sha_hint"),
        })

    try:
        content = witness_path.read_text(errors="replace")
    except OSError:
        return _envelope({
            "content": "",
            "witness_path": witness_rel,
            "truncated": False,
            "sha_hint": witness.get("sha_hint"),
        })

    truncated = len(content) > 3200
    if truncated:
        content = content[:3200] + "\n... [truncated]"
    content = sanitize_for_chameleon_context(content)
    return _envelope({
        "content": content,
        "witness_path": witness_rel,
        "truncated": truncated,
        "sha_hint": witness.get("sha_hint"),
    })


def get_rules(repo: str, archetype: str | None = None) -> dict:
    """Return rules + citations for repo, filtered by archetype if provided."""
    from chameleon_mcp.profile.loader import load_profile_dir

    repo_root = _resolve_repo_root_by_id(repo)
    if repo_root is None:
        return _envelope({"rules": []})

    try:
        loaded = load_profile_dir(repo_root / ".chameleon")
    except Exception:
        return _envelope({"rules": []})

    rules_dict = loaded.rules.get("rules", {}) or {}
    if archetype is None:
        return _envelope({"rules": list(rules_dict.items())})
    # Filter rules whose key matches archetype prefix or exact name
    filtered = [(k, v) for k, v in rules_dict.items() if archetype in str(k)]
    return _envelope({"rules": filtered})


def lint_file(repo: str, archetype: str, content: str) -> dict:
    """Stub. Real lint engine ships in Phase 4 (AST query against the
    canonical's normative shape).

    The response carries `"stub": true` so callers don't treat the empty
    violations list as a passing lint. Use `get_pattern_context` for the
    actual archetype guidance until the real linter lands.
    """
    del repo, archetype
    # Cap content at 100 KB per architecture
    truncated = len(content) > 100_000
    return _envelope(
        {
            "stub": True,
            "stub_reason": "lint engine is Phase 4; v0.2 returns no violations regardless of input",
            "violations": [],
            "canonical_confidence": 0.0,
            "unparseable_regions": [],
            "content_size": len(content),
        },
        truncated=truncated,
    )


def get_drift_status(repo: str) -> dict:
    """Report freshness for a repo by repo_id.

    Computes:
    - days_since_refresh from the trust record's granted_at
    - observed_drift_score from drift.db's recent edit_observations
      (None if no observations yet)
    - recommended_action: combines both signals
    """
    import time

    from chameleon_mcp.drift.observations import compute_drift_score
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    if not isinstance(repo, str) or not repo:
        return _envelope({
            "days_since_refresh": None,
            "observed_drift_score": None,
            "recommended_action": "invalid repo_id",
        })

    repo_data = plugin_data_dir() / repo
    trust = trust_state_for(repo) if repo_data.is_dir() else None

    days_since_refresh: int | None = None
    if trust is not None and trust.granted_at:
        try:
            granted_epoch = time.mktime(time.strptime(trust.granted_at, "%Y-%m-%dT%H:%M:%SZ"))
            days_since_refresh = max(0, int((time.time() - granted_epoch) / 86_400))
        except ValueError:
            days_since_refresh = None

    drift_score = compute_drift_score(repo)

    if days_since_refresh is None:
        recommended = "no trust grant found; run /chameleon-trust first"
    elif drift_score is not None and drift_score > 0.5:
        recommended = (
            f"observed drift is high ({drift_score:.2f}); run /chameleon-refresh"
        )
    elif days_since_refresh > 90:
        recommended = "profile may be stale; run /chameleon-refresh"
    elif days_since_refresh > 30:
        recommended = "consider /chameleon-refresh if codebase has materially changed"
    else:
        recommended = "fresh"

    return _envelope({
        "repo_id": repo,
        "days_since_refresh": days_since_refresh,
        "observed_drift_score": drift_score,
        "recommended_action": recommended,
    })


def refresh_repo(repo: str, force: bool = False) -> dict:
    """Re-analyze repo, detect drift, update profile.

    Phase 2D: simplification — call bootstrap_repo on the repo's root.
    Phase 4 implements true incremental refresh + flock-protected updates.

    The `force` parameter is currently a no-op: refresh_repo always re-runs
    the full bootstrap pipeline in v0.2 (drift detection is the user's
    decision to invoke). When Phase 4 adds incremental refresh, `force=True`
    will bypass the incremental short-circuit.
    """
    del force  # accepted for forward-compat; see docstring
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
    """List all known repos this user has touched.

    Phase 2D: scans ${PLUGIN_DATA}/<repo_id>/ directories for trust records.
    Cursor pagination shape implemented from day 1 (Round 5 API Designer);
    Phase 4 swaps to index.db single-SQLite query for >1000 repos.
    """
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    if not isinstance(limit, int) or limit <= 0 or limit > 1000:
        return _envelope({
            "status": "failed",
            "error": "limit must be an integer in 1..1000",
        })

    base = plugin_data_dir()
    if not base.is_dir():
        return _envelope({"profiles": []}, next_cursor=None)

    # Collect all known repo_ids by directory listing
    all_repo_ids = sorted(
        [d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
    )

    # Cursor = first repo_id of next page
    if cursor:
        try:
            start = all_repo_ids.index(cursor)
        except ValueError:
            return _envelope({
                "status": "failed",
                "error": f"unknown cursor {cursor!r}; pass the next_cursor value from a prior page",
            })
    else:
        start = 0

    page = all_repo_ids[start : start + limit]
    next_cursor = (
        all_repo_ids[start + limit] if start + limit < len(all_repo_ids) else None
    )

    profiles = []
    for repo_id in page:
        trust = trust_state_for(repo_id)
        profiles.append({
            "repo_id": repo_id,
            "trust_state": "trusted" if trust else "untrusted",
            "trusted_at": trust.granted_at if trust else None,
            "trusted_by": trust.granted_by_user if trust else None,
        })

    return _envelope(
        {"profiles": profiles, "total_known": len(all_repo_ids)},
        next_cursor=next_cursor,
    )


def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge for git merge driver use.

    Per ARCHITECTURE.md "merge_profiles algorithm": the canonical-correct
    merge of two profile JSONs is to re-cluster from the union — but the
    git merge driver only has the static .json content of base/ours/theirs,
    not the underlying repo. So we approximate: take the union of archetypes
    from ours+theirs, dedup by cluster name, prefer the higher cluster_size
    on conflict (ties broken by alphabetic witness path), and write the
    result to `ours` so the merge driver can stage it.

    The base argument is currently used only for conflict-detection logging;
    canonical-correct three-way merging requires re-bootstrap from the merged
    repo state, which the user can trigger with /chameleon-refresh after
    accepting the merge.
    """
    del base  # unused — see docstring

    ours_path = Path(ours)
    theirs_path = Path(theirs)
    if not ours_path.is_file() or not theirs_path.is_file():
        return _envelope({
            "status": "failed",
            "error": "ours and theirs must point to existing profile JSON files",
            "merged_profile_path": None,
        })

    try:
        ours_data = json.loads(ours_path.read_text(encoding="utf-8"))
        theirs_data = json.loads(theirs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _envelope({
            "status": "failed",
            "error": f"profile JSON parse error: {e}",
            "merged_profile_path": None,
        })

    ours_archs = ours_data.get("archetypes", {}) or {}
    theirs_archs = theirs_data.get("archetypes", {}) or {}

    merged: dict[str, dict] = dict(ours_archs)
    for name, arch in theirs_archs.items():
        if name not in merged:
            merged[name] = arch
            continue
        # Conflict: prefer higher cluster_size; ties → alphabetic witness path.
        ours_size = (merged[name] or {}).get("cluster_size", 0)
        theirs_size = (arch or {}).get("cluster_size", 0)
        if theirs_size > ours_size:
            merged[name] = arch
        elif theirs_size == ours_size:
            ours_witness = (merged[name] or {}).get("canonical_witness", "")
            theirs_witness = (arch or {}).get("canonical_witness", "")
            if theirs_witness < ours_witness:
                merged[name] = arch

    merged_data = dict(ours_data)
    merged_data["archetypes"] = merged

    # Write merge result to `ours` (git merge driver convention).
    ours_path.write_text(
        json.dumps(merged_data, indent=2, sort_keys=True), encoding="utf-8"
    )

    return _envelope({
        "status": "success",
        "merged_profile_path": str(ours_path),
        "merged_archetype_count": len(merged),
        "ours_archetype_count": len(ours_archs),
        "theirs_archetype_count": len(theirs_archs),
        "note": (
            "merged by archetype-name union; run /chameleon-refresh after accepting "
            "the merge to re-cluster from the actual merged repo state"
        ),
    })


def teach_profile(repo: str, feedback: str) -> dict:
    """Append a captured idiom to .chameleon/idioms.md.

    Sanitization is delegated to `sanitize_for_chameleon_context` (ANSI,
    zero-width, NFC, tag-boundary). On top of that we:

    - Reject empty / whitespace-only feedback (no orphan idioms).
    - Honor a user-supplied `### slug` header instead of always prepending
      an auto-generated one.
    - Escape level-1 and level-2 ATX headings (`#` / `##`) in the body so a
      `## deprecated` line in feedback can't fork idioms.md's section
      structure.
    - Strip the `_(no idioms yet …)_` placeholder the first time an active
      idiom is added.
    - Hold an advisory flock around the read-modify-write so concurrent
      `/chameleon-teach` calls don't lose idioms.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({"status": "failed", "error": "expected absolute repo path"})

    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope({"status": "failed", "error": "no profile in this repo (run /chameleon-init)"})

    sanitized = _sanitize_user_input(feedback)
    if not sanitized.strip():
        return _envelope({"status": "failed", "error": "feedback is empty after sanitization"})
    if len(sanitized) > 50_000:
        return _envelope({"status": "failed", "error": "feedback exceeds 50KB cap"})

    body = _escape_markdown_section_headings(sanitized)

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    if body.lstrip().startswith("### "):
        # User supplied a slug — use as-is.
        addition = f"\n{body.rstrip()}\n"
    else:
        slug = f"idiom-{timestamp}-{int(time.time())}"
        addition = f"\n### {slug}\nStatus: active (added {timestamp})\n{body}\n"

    lock_path = idioms_path.parent / ".idioms.lock"
    try:
        with acquire_advisory_lock(lock_path):
            current = (
                idioms_path.read_text(encoding="utf-8")
                if idioms_path.exists()
                else "# idioms\n\n## active\n\n## deprecated\n"
            )
            # Drop the "(no idioms yet …)" placeholder on first add.
            current = current.replace(
                "_(no idioms yet — run /chameleon-teach to capture team conventions)_\n\n",
                "",
                1,
            )
            if "## active" in current:
                new_content = current.replace("## active\n", f"## active\n{addition}", 1)
            else:
                new_content = current + addition
            idioms_path.write_text(new_content, encoding="utf-8")
    except LockHeldError as e:
        return _envelope({
            "status": "failed",
            "error": (
                f"another /chameleon-teach is in progress (PID {e.holder_pid}); "
                "retry shortly"
            ),
        })

    return _envelope({
        "status": "success",
        "idioms_added": 1,
        "idioms_deprecated": 0,
    })


def _escape_markdown_section_headings(text: str) -> str:
    """Escape `#` / `##` ATX headings at start of line.

    idioms.md uses `## active` / `## deprecated` as section markers; an
    unsanitized `## deprecated` line in a user idiom body would otherwise
    split the active section. CommonMark renders `\\##` as literal text.

    Only levels 1 and 2 are escaped — `###`, `####`, … are valid idiom
    sub-headers and stay untouched.
    """
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if (
            stripped.startswith("## ")
            or stripped.startswith("# ")
            or stripped in ("##", "#")
        ):
            out.append(f"{indent}\\{stripped}")
        else:
            out.append(line)
    return "\n".join(out)


def disable_session(repo: str, session_id: str) -> dict:
    """Mark chameleon disabled for the given session_id.

    Writes a `.session_disabled.<session_id>` marker under the per-repo
    plugin data dir. preflight-and-advise checks this marker before
    injecting context — when present, no <chameleon-context> content
    is added to Edit/Write/NotebookEdit operations for that session.

    Used by the /chameleon-disable slash command.
    """
    from chameleon_mcp.optouts import write_session_disable

    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({"status": "failed", "error": "expected absolute repo path"})
    if not session_id or not isinstance(session_id, str):
        return _envelope({"status": "failed", "error": "session_id required"})

    repo_id = _compute_repo_id(repo_path)
    marker = write_session_disable(repo_id, session_id)
    return _envelope({
        "status": "success",
        "marker_path": str(marker),
        "session_id": session_id,
        "scope": "session",
    })


def pause_session(repo: str, minutes: int = 15) -> dict:
    """Pause chameleon advisory injections for `minutes` minutes.

    Writes a `.pause_until` file with an ISO 8601 expiry timestamp
    under the per-repo plugin data dir. preflight-and-advise auto-
    expires the marker; no manual cleanup needed.

    Used by the /chameleon-pause-15m slash command (and any future
    /chameleon-pause-<N> variants).
    """
    from chameleon_mcp.optouts import write_pause

    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({"status": "failed", "error": "expected absolute repo path"})
    if not isinstance(minutes, int) or minutes <= 0 or minutes > 240:
        return _envelope({"status": "failed", "error": "minutes must be 1..240"})

    repo_id = _compute_repo_id(repo_path)
    expiry_iso = write_pause(repo_id, minutes)
    return _envelope({
        "status": "success",
        "expires_at": expiry_iso,
        "minutes": minutes,
    })


def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user.

    Phase 2D: validates `confirmation_token` matches the repo's basename
    (typed repo name) or `yes-trust-<repo_id_short>`. Writes .trust file.
    """
    from chameleon_mcp.profile.trust import grant_trust

    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute():
        return _envelope({"status": "failed", "error": f"repo path must be absolute: {repo!r}"})
    if not repo_path.exists():
        return _envelope({"status": "failed", "error": f"repo path does not exist: {repo!r}"})
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    profile_dir = repo_path / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope({"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"})
    if not (profile_dir / "profile.json").is_file():
        return _envelope({"status": "failed", "error": "no profile.json in .chameleon/ (run /chameleon-init first)"})

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
    """Sanitize user-supplied text before persisting to idioms.md.

    User idioms get echoed back into the model's context inside a
    <chameleon-context> wrapper, so the same tag-boundary protections that
    apply to canonical excerpts must apply here. sanitize_for_chameleon_context
    already covers ANSI escapes, zero-width unicode, NFC normalization, AND
    closing-tag neutralization — there is no reason teach_profile should
    use a weaker subset.
    """
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    return sanitize_for_chameleon_context(text)
