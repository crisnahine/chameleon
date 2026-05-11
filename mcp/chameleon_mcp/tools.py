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
import re
import subprocess
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


# Hosts whose URLs are case-insensitive — folded to lowercase before hashing.
_CASE_INSENSITIVE_HOSTS: frozenset[str] = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com"}
)

# SSH-style URL: git@host:owner/repo[.git]
_SSH_URL_RE = re.compile(r"^(?:[\w-]+@)?([^:]+):(.+?)(?:\.git)?/?$")


def _normalize_git_url(url: str) -> str:
    """Canonicalize a git remote URL for repo_id derivation.

    The goal is that two checkouts of the same repo — regardless of whether
    the remote was cloned over https or ssh, with or without a trailing
    .git, and with or without case-variation on the host — collapse to the
    same canonical string.

    Transforms applied (in order):
    1. Strip surrounding whitespace.
    2. Rewrite scp/ssh syntax `git@host:owner/repo` → `ssh://git@host/owner/repo`.
    3. Strip a trailing `.git` from the path.
    4. Strip a trailing slash from the path.
    5. Force scheme to `https://` when the host is one of the well-known
       hosting providers — both `https://github.com/...` and
       `ssh://git@github.com/...` resolve to the same repository.
    6. Lowercase the host for case-insensitive hosts.

    Returns the canonical URL string. Non-URL input is returned stripped so
    we never crash — the caller still hashes whatever we return, which keeps
    the function total.
    """
    s = (url or "").strip()
    if not s:
        return s

    # SSH/scp shorthand → ssh://… so the rest of the pipeline has a uniform
    # `scheme://host/path` shape to work with.
    m = _SSH_URL_RE.match(s)
    if m and "://" not in s:
        host, path = m.group(1), m.group(2)
        s = f"ssh://git@{host}/{path}"

    # Drop trailing `.git` / trailing slash on the path.
    s = re.sub(r"\.git/?$", "", s)
    s = s.rstrip("/")

    # Split scheme/host/path so we can lowercase the host independently.
    proto_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*)://([^/]+)(/.*)?$", s)
    if not proto_match:
        return s
    scheme, host, path = proto_match.group(1), proto_match.group(2), proto_match.group(3) or ""

    # Drop user@ prefix on the host (ssh URLs).
    if "@" in host:
        host = host.split("@", 1)[1]

    host_l = host.lower()
    if host_l in _CASE_INSENSITIVE_HOSTS:
        host = host_l
        # Collapse https/http/ssh/git to https for well-known hosts — same
        # repo, same id, regardless of clone protocol.
        scheme = "https"

    return f"{scheme}://{host}{path}"


def _git_remote_url(repo_root: Path) -> str | None:
    """Return the `origin` remote URL, or None if not a git repo / no remote.

    Bounded by a 2 second timeout — if git takes longer than that to answer
    a config lookup something is wrong with the workspace, and the path-based
    fallback is the safer choice than blocking bootstrap.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _compute_repo_id(repo_root: Path) -> str:
    """Canonical repo_id.

    Schema v6+: prefer git remote URL (stable across moved checkouts);
    fall back to the resolved absolute path when no git remote exists.

    Two checkouts of the same repository — even on different machines or
    after moving the working tree — get the same id, so the per-user trust
    grant and drift observations follow the project rather than the
    filesystem location. Repos without `origin` (fresh `git init`, vendored
    snapshots, archive extracts) keep the v0.1–v0.3 path-based behavior.
    """
    url = _git_remote_url(repo_root)
    if url:
        canonical = _normalize_git_url(url)
        if canonical:
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()


def _legacy_path_repo_id(repo_root: Path) -> str:
    """The pre-v6 path-derived repo_id.

    Used by `detect_repo` to look up trust grants made by v0.1–v0.3 engines.
    A trust record found at the legacy id surfaces a `legacy_trust_state`
    hint so the model can prompt the user to re-trust under the new id.
    """
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

    # Schema v6 migration helper: when the canonical (git-remote-derived) id
    # has no trust grant but a legacy path-derived id DOES, surface a hint
    # so the model can prompt the user to re-trust under the new id. Skip
    # the check when the two ids happen to be equal (no git remote — the
    # function already returned the legacy id and there's nothing to migrate).
    legacy_id = _legacy_path_repo_id(repo_root)
    legacy_trust_hint: str | None = None
    if trust is None and legacy_id != repo_id and trust_state_for(legacy_id) is not None:
        legacy_trust_hint = (
            "Trust record found at the legacy (pre-v0.4) path-derived repo_id "
            f"{legacy_id[:8]}…; the canonical repo_id is now derived from the "
            "git remote URL. Run /chameleon-trust to re-grant under the new id."
        )

    data: dict = {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "profile_status": "profile_present" if profile_present else "no_profile",
        "trust_state": trust_state,
    }
    if legacy_trust_hint is not None:
        data["legacy_repo_id"] = legacy_id
        data["legacy_trust_hint"] = legacy_trust_hint
    return _envelope(data)


def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    v0.4 (4.2): tiebreaks among multiple path-bucket matches by AST shape.
    When the file exists on disk we extract its dimensions via the lint
    engine's pure-function `extract_dimensions` and score each path-bucket
    candidate by how many `ast_query` dimensions align. Higher score wins;
    ties fall back to the v0.3 cluster-size ordering.

    The confidence band reflects how strong the AST signal was:
      "high"   — score >= 4 of 5 ast_query dimensions agreed
      "medium" — at least one dimension agreed
      "low"    — no AST signal (file missing on disk, no ast_query, or
                 substring-only fallback match)

    Backwards compat: files without on-disk content (deleted, just-detected
    from a hook input that doesn't carry content) fall back to the v0.3
    path-bucket-only behavior so the function stays callable on hypothetical
    paths.
    """
    from chameleon_mcp.lint_engine import (
        canonical_confidence,
        detect_language,
        extract_dimensions,
    )
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

    archetypes = loaded.archetypes.get("archetypes", {})
    for name, arch in archetypes.items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        if pattern == file_bucket:
            exact_matches.append(name)
        elif pattern in rel_str:
            fallback_matches.append(name)

    # If the path bucket gave us no exact matches, fall back to substring
    # behavior (v0.3) and don't bother extracting dimensions.
    if not exact_matches:
        if fallback_matches:
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

    # Path-bucket gave us one or more candidates; try AST shape verification.
    # The cluster-size ordering becomes our stable tiebreak when AST signals
    # are absent or tied.
    exact_matches.sort(
        key=lambda n: archetypes.get(n, {}).get("cluster_size", 0),
        reverse=True,
    )

    # Read the file's content if it exists. If it doesn't, fall back to
    # path-bucket-only matching (v0.3 behavior) so the function stays
    # callable on hypothetical paths.
    content: str | None = None
    if p.is_file():
        try:
            # Cap at 100KB — same ceiling as lint_file. Files larger than
            # this are unlikely to be useful canonical examples anyway.
            raw = p.read_bytes()[:100_000]
            content = raw.decode("utf-8", errors="replace")
        except OSError:
            content = None

    if content is None:
        # No content available — v0.3 cluster-size ordering wins.
        return _envelope({
            "archetype": exact_matches[0],
            "alternatives": exact_matches[1:],
            "content_signal_match": None,
            "confidence_band": "high" if len(exact_matches) == 1 else "low",
        })

    # Extract dimensions and score each candidate against its ast_query.
    language = detect_language(str(p)) or loaded.profile.get("language")
    if language not in ("typescript", "ruby"):
        language = None
    snapshot = extract_dimensions(content, language=language)

    canonicals = loaded.canonicals.get("canonicals", {}) or {}
    scored: list[tuple[str, float, int]] = []  # (name, score, ast_query_field_count)
    for name in exact_matches:
        entries = canonicals.get(name) or []
        ast_query: dict | None = None
        if entries:
            first = entries[0] or {}
            ast_query = (first.get("normative_shape") or {}).get("ast_query")
        if not ast_query:
            # No AST signal available — confidence stays low for this candidate.
            scored.append((name, -1.0, 0))
            continue
        # Count non-null fields the archetype constrains. canonical_confidence
        # returns a 0..1 ratio; we want the absolute count of matched fields
        # so we can apply the architecture's "score >= 4 of 5" rule directly.
        constrained = sum(1 for k in (
            "default_export_kind",
            "top_level_node_kinds",
            "named_export_count_bucket",
            "jsx_present",
            "content_signal",
        ) if ast_query.get(k) not in (None, [], ""))
        ratio = canonical_confidence(snapshot, ast_query)
        absolute_matches = ratio * constrained
        scored.append((name, absolute_matches, constrained))

    # If at least one candidate has an AST signal, rank by score; otherwise
    # keep the cluster-size ordering.
    if any(s > -1.0 for _, s, _ in scored):
        scored.sort(
            key=lambda item: (
                -item[1],  # highest score first
                -archetypes.get(item[0], {}).get("cluster_size", 0),
            )
        )
        primary = scored[0][0]
        alternatives = [n for n, _, _ in scored[1:]]
        best_score = scored[0][1]
        if best_score >= 4:
            confidence = "high"
        elif best_score > 0:
            confidence = "medium"
        else:
            confidence = "low"
    else:
        primary = exact_matches[0]
        alternatives = exact_matches[1:]
        confidence = "high" if len(exact_matches) == 1 else "low"

    return _envelope({
        "archetype": primary,
        "alternatives": alternatives,
        "content_signal_match": snapshot.content_signal,
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
    """Map a repo_id back to its repo_root.

    Phase 4.4 lookup order:
      1. index.db (primary; populated by bootstrap_repo on success)
      2. trust record's repo_root (backward compat with v0.1/v0.2 installs
         that bootstrapped before index.db existed)

    Returns None if neither layer resolves to an existing directory.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import trust_state_for

    # Primary: index.db
    indexed = index_db.resolve_repo_root(repo_id)
    if indexed:
        p = Path(indexed)
        if p.is_dir():
            return p.resolve()
        # Stale index row (the user moved or deleted the repo). Fall
        # through to the trust record rather than returning None outright
        # — the trust record may have been updated by a more recent
        # grant_trust call that has not yet been mirrored into index.db.

    # Fallback: trust record
    record = trust_state_for(repo_id)
    if record is None or not record.repo_root:
        return None
    p = Path(record.repo_root)
    return p.resolve() if p.is_dir() else None


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
    """Compare `content` against the archetype's canonical AST shape; return
    structural violations.

    Phase 4.1 (v0.3): real implementation. The engine extracts the file's
    shape dimensions via language-aware regex heuristics (see
    `lint_engine.extract_dimensions`) and compares them against the
    archetype's `ast_query` block in canonicals.json.

    Resolution rules:
    - If `repo` can be resolved to a profile dir AND the archetype has a
      non-null ast_query, run the real engine and return `"stub": False`.
    - If the archetype exists but its ast_query is null / missing, return
      a real-envelope shape with `"stub": False` and a `"reason"` field
      explaining the no-op (the engine ran; it just had nothing to check).
    - If the repo / profile cannot be resolved at all, fall back to the
      legacy stub envelope (`"stub": True`) so callers without a real
      profile continue to see the no-op semantics they relied on in v0.2.

    The 100 KB content cap from v0.2 is preserved: oversized content is
    flagged via the `truncated` envelope field and the engine processes
    the truncated buffer (not the full content). The engine is otherwise
    pure (no I/O), so the function is safe to call repeatedly without
    leaking subprocess state.
    """
    from chameleon_mcp.lint_engine import (
        canonical_confidence as _canonical_confidence,
    )
    from chameleon_mcp.lint_engine import (
        detect_language as _detect_language,
    )
    from chameleon_mcp.lint_engine import (
        extract_dimensions as _extract_dimensions,
    )
    from chameleon_mcp.lint_engine import (
        lint as _lint,
    )
    from chameleon_mcp.lint_engine import (
        scan_secrets as _scan_secrets,
    )
    from chameleon_mcp.profile.loader import load_profile_dir

    # 1. Cap content (architecture's lint_file size contract).
    content_size = len(content)
    truncated = content_size > 100_000
    working_content = content[:100_000] if truncated else content

    # v0.4 (4.8) — secret-detection runs FIRST and independently of the
    # archetype lookup. Even a stub-envelope response (unresolvable repo,
    # missing profile) should still surface secret violations because the
    # security risk is identical whether or not the file has a known
    # archetype.
    secret_violations = [v.to_dict() for v in _scan_secrets(working_content)]

    # 2. Resolve the repo. Try the standard repo_id → repo_root mapping
    # first (the documented contract). If `repo` isn't a recognized repo_id,
    # fall back to treating it as a path (defensive: some tests + older
    # callers pass paths, and the architecture's contract for `repo` is a
    # repo_id but we want to be liberal in what we accept).
    repo_root = _resolve_repo_root_by_id(repo)
    if repo_root is None:
        candidate = Path(repo) if isinstance(repo, str) and repo else None
        if (
            candidate is not None
            and candidate.is_absolute()
            and candidate.is_dir()
            and (candidate / ".chameleon" / "profile.json").is_file()
        ):
            repo_root = candidate

    if repo_root is None:
        # Cannot run the real engine — preserve the legacy stub envelope so
        # callers that depended on v0.2 behavior (no profile / unresolvable
        # repo) keep working. Secret-scan results are still surfaced (this
        # is a security check that must NOT be gated on having a profile).
        return _envelope(
            {
                "stub": True,
                "stub_reason": (
                    "repo could not be resolved to a profile dir; "
                    "/chameleon-init or /chameleon-trust the repo first"
                ),
                "violations": secret_violations,
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": content_size,
            },
            truncated=truncated,
        )

    # 3. Load the profile + look up the archetype's ast_query.
    try:
        loaded = load_profile_dir(repo_root / ".chameleon")
    except Exception:
        return _envelope(
            {
                "stub": True,
                "stub_reason": "profile failed to load (corrupted? run /chameleon-refresh)",
                "violations": secret_violations,
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": content_size,
            },
            truncated=truncated,
        )

    canonicals = loaded.canonicals.get("canonicals", {}) or {}
    entries = canonicals.get(archetype) or []
    ast_query: dict | None = None
    witness_rel_path: str | None = None
    if entries:
        first = entries[0] or {}
        ast_query = (first.get("normative_shape") or {}).get("ast_query")
        witness_rel_path = (first.get("witness") or {}).get("path")

    # 4. If the archetype is unknown OR its ast_query is null/empty, run
    # the engine as a no-op. This is the real-impl envelope (stub: False)
    # because the engine *did* run — there just wasn't a query to evaluate.
    if not ast_query:
        return _envelope(
            {
                "stub": False,
                "stub_reason": None,
                "violations": secret_violations,
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": content_size,
                "reason": (
                    "no ast_query for archetype "
                    f"{archetype!r} — re-bootstrap via /chameleon-refresh"
                ),
            },
            truncated=truncated,
        )

    # 5. Determine language. Prefer the witness extension (the archetype's
    # actual language); fall back to the profile-level language metadata.
    language = _detect_language(witness_rel_path) or loaded.profile.get("language")
    if language not in ("typescript", "ruby"):
        language = None

    # 6. Extract dimensions + run the lint comparison.
    snapshot = _extract_dimensions(working_content, language=language)
    ast_violations = [v.to_dict() for v in _lint(snapshot, ast_query)]
    confidence = _canonical_confidence(snapshot, ast_query)

    # Merge AST violations + secret violations. Secrets first so the model
    # sees the security-critical ones at the top of the list — they take
    # priority over style/structural mismatches.
    violations = secret_violations + ast_violations

    return _envelope(
        {
            "stub": False,
            "stub_reason": None,
            "violations": violations,
            "canonical_confidence": confidence,
            "unparseable_regions": snapshot.unparseable_regions,
            "content_size": content_size,
            "archetype": archetype,
            "language": language,
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

    Phase 4.3 adds a no-op short-circuit: if `index.db` has a record for
    this repo AND no file in the discovery set has changed since the
    last bootstrap's `last_seen_at`, return `status="noop"` without
    re-bootstrapping. The response still carries `archetypes_detected`
    (populated from the cached profile) so backward-compat assertions
    like `r1["archetypes_detected"] == r2["archetypes_detected"]` keep
    passing.

    Partial re-clustering (the >10% changed path) is deferred to a
    Phase 4.3-extended deliverable; today we fall through to the full
    bootstrap as soon as any file has moved.

    `force=True` bypasses the short-circuit and always re-bootstraps.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.bootstrap.orchestrator import _glob_for_extractor, _select_extractor

    repo_path = Path(repo)
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({
            "status": "failed",
            "error": "refresh_repo expects an absolute repo path",
        })

    # Phase 4.3 no-op optimization. Skipped on `force=True`.
    if not force:
        repo_root = repo_path.resolve()
        repo_id = _compute_repo_id(repo_root)
        cached = index_db.get_repo(repo_id)
        profile_dir = repo_root / ".chameleon"
        profile_path = profile_dir / "profile.json"
        if cached and profile_path.is_file():
            try:
                extractor = _select_extractor(repo_root)
            except Exception:
                extractor = None
            if extractor is not None:
                try:
                    candidates = discover_files(
                        repo_root, glob=_glob_for_extractor(extractor)
                    )
                except Exception:
                    candidates = None
                if candidates is not None:
                    cached_files = cached.get("files_indexed") or 0
                    last_seen_iso = cached.get("last_seen_at") or ""
                    last_seen_epoch = _iso_to_epoch(last_seen_iso)
                    # Include idioms.md in the freshness check: a fresh
                    # /chameleon-teach must invalidate the no-op so the
                    # transaction re-renders `profile.summary.md` with the
                    # new idiom body (the trust-review surface). idioms.md
                    # is the only file outside the discovery glob whose
                    # content affects committed profile artifacts.
                    idioms_path = profile_dir / "idioms.md"
                    refresh_inputs = list(candidates) + [idioms_path]
                    max_mtime = index_db.max_mtime_over(refresh_inputs)
                    cardinality_match = (
                        cached_files > 0 and len(candidates) == cached_files
                    )
                    nothing_newer = (
                        last_seen_epoch > 0.0 and max_mtime <= last_seen_epoch
                    )
                    if cardinality_match and nothing_newer:
                        # Touch the row so the repo bubbles to the top of
                        # list_profiles even on a no-op refresh.
                        index_db.upsert_repo(
                            repo_id,
                            str(repo_root),
                            archetype_count=cached.get("archetype_count"),
                            files_indexed=cached_files,
                            bootstrap_ms=cached.get("bootstrap_ms"),
                            profile_sha256=cached.get("profile_sha256"),
                        )
                        return _envelope({
                            "status": "noop",
                            "reason": "no files changed since last refresh",
                            "archetypes_detected": cached.get("archetype_count") or 0,
                            "files_processed": cached_files,
                            "duration_ms": 0,
                            "profile_path": str(profile_dir),
                        })

    return bootstrap_repo(str(repo_path))


def _iso_to_epoch(ts: str) -> float:
    """Convert an ISO 8601 UTC timestamp to epoch seconds.

    Returns 0.0 on parse failure so callers treat unparseable timestamps
    as "no cached observation" rather than crashing the refresh path.

    Uses `calendar.timegm` (not `time.mktime`) because the stored timestamp
    is UTC; `mktime` interprets a parsed `time.struct_time` as local time,
    which silently shifts the value by the running machine's timezone
    offset and broke the no-op short-circuit during testing.
    """
    if not ts:
        return 0.0
    import calendar

    # Microsecond-precision path: time.strptime parses '%f' but struct_time
    # drops the fractional component, so calendar.timegm returns whole
    # seconds. Reconstruct the float ourselves.
    if "." in ts and ts.endswith("Z"):
        try:
            whole, frac = ts[:-1].split(".", 1)
            base = calendar.timegm(time.strptime(whole + "Z", "%Y-%m-%dT%H:%M:%SZ"))
            return base + float(f"0.{frac}")
        except (ValueError, TypeError):
            return 0.0
    # Second-precision fallback (v0.1/v0.2 ISO strings).
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def bootstrap_repo(path: str, mode: str = "full", paths_glob: str | None = None) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit.

    v0.4 (2D.3): for monorepos with detected workspace_paths, runs the full
    pipeline per workspace as well, producing one `.chameleon/` under each
    workspace root in addition to the root profile that catalogs them.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap
    from chameleon_mcp.profile.trust import hash_profile

    repo_root = Path(path).expanduser().resolve()
    if not repo_root.is_dir():
        return _envelope({
            "status": "failed",
            "error": f"path is not a directory: {path}",
        })
    del mode  # forward-compat for Phase 2D interview mode
    report = _bootstrap(repo_root, paths_glob=paths_glob)

    # Phase 4.4: mirror the run into the repo index. Only on success — a
    # failed_too_many_files / failed_unsupported_language run should not
    # leave a stale entry for /chameleon-list-profiles to surface.
    if report.status == "success":
        repo_id = _compute_repo_id(repo_root)
        index_db.upsert_repo(
            repo_id,
            str(repo_root),
            profile_sha256=hash_profile(repo_root / ".chameleon"),
            archetype_count=report.archetypes_detected,
            files_indexed=report.files_processed,
            bootstrap_ms=report.duration_ms,
        )
        # v0.4 2D.3: register each successfully bootstrapped workspace too
        # so `/chameleon-list-profiles` and the trust-resolution layer can
        # find them by repo_id without re-walking the workspace tree.
        for ws in report.workspace_reports or []:
            if ws.get("status") != "success":
                continue
            ws_root_str = ws.get("repo_root")
            if not ws_root_str:
                continue
            ws_root = Path(ws_root_str)
            ws_repo_id = _compute_repo_id(ws_root)
            index_db.upsert_repo(
                ws_repo_id,
                str(ws_root),
                profile_sha256=hash_profile(ws_root / ".chameleon"),
                archetype_count=ws.get("archetypes_detected"),
                files_indexed=ws.get("files_processed"),
                bootstrap_ms=ws.get("duration_ms"),
            )

    return _envelope(report.to_dict())


def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos this user has touched.

    Phase 4.4: backed by `index.db`. Ordered by last_seen_at DESC (most
    recently bootstrapped/refreshed first), then by repo_id ASC as a
    stable tiebreaker.

    For backward compat with v0.1/v0.2 installs that have ${PLUGIN_DATA}/
    populated but no index.db yet, we fall back to scanning the per-repo
    directory listing and best-effort backfill into the index. After one
    list_profiles call on an existing install, all known repos are
    represented in index.db.

    Validation behavior is preserved from v0.2:
    - `limit` must be an int in 1..1000
    - an unknown `cursor` returns an explicit failure envelope
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    if not isinstance(limit, int) or limit <= 0 or limit > 1000:
        return _envelope({
            "status": "failed",
            "error": "limit must be an integer in 1..1000",
        })

    # Backfill from legacy directory layout BEFORE serving the query so
    # existing v0.1/v0.2 installs keep working. This is a no-op once the
    # index has caught up with the on-disk state.
    _backfill_index_from_legacy_dirs()

    try:
        page_rows, next_cursor, total_known = index_db.list_repos(cursor, limit)
    except ValueError:
        return _envelope({
            "status": "failed",
            "error": (
                f"unknown cursor {cursor!r}; pass the next_cursor value from a prior page"
            ),
        })

    base = plugin_data_dir()
    profiles = []
    for row in page_rows:
        repo_id = row["repo_id"]
        # Per-repo trust state is sourced from the trust record so that
        # `granted_at` / `granted_by_user` always reflect the latest grant,
        # not whatever was snapshotted into index.db at bootstrap time.
        trust = trust_state_for(repo_id) if (base / repo_id).is_dir() else None
        profiles.append({
            "repo_id": repo_id,
            "trust_state": "trusted" if trust else "untrusted",
            "trusted_at": trust.granted_at if trust else None,
            "trusted_by": trust.granted_by_user if trust else None,
        })

    return _envelope(
        {"profiles": profiles, "total_known": total_known},
        next_cursor=next_cursor,
    )


def _backfill_index_from_legacy_dirs() -> None:
    """Mirror legacy ${PLUGIN_DATA}/<repo_id>/ trust records into index.db.

    Pre-Phase-4.4 installs only stored repo_id → repo_root in the trust
    record. The first list_profiles call after upgrade walks the per-repo
    dirs and inserts any repo_id that has a trust record but no row in
    index.db. Idempotent.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    base = plugin_data_dir()
    if not base.is_dir():
        return
    try:
        candidate_ids = [
            d.name for d in base.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
    except OSError:
        return

    for repo_id in candidate_ids:
        # Cheap skip: row already present.
        if index_db.resolve_repo_root(repo_id):
            continue
        trust = trust_state_for(repo_id)
        if trust is None or not trust.repo_root:
            continue
        # Use the trust record's granted_at as last_seen_at so the
        # backfilled rows order naturally with newly inserted rows.
        index_db.upsert_repo(
            repo_id,
            trust.repo_root,
            profile_sha256=trust.profile_sha256 or None,
            last_seen_at=trust.granted_at or None,
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


# ---------------------------------------------------------------------------
# Phase 2D.1 — Interactive ≤3-prompt rename interview
#
# The interview is driven by the chameleon-init skill (prose protocol);
# the MCP exposes two small stateless tools that the skill calls:
#
#   1. propose_archetype_renames(repo, top_n) — read the committed profile,
#      surface the top-N largest archetypes plus 3-5 better-name candidates
#      derived from canonical filename, paths_pattern tail, and top-level
#      node kinds.
#   2. apply_archetype_renames(repo, renames) — atomically rewrite
#      archetypes.json + canonicals.json + profile.summary.md keys.
#
# Both tools use the standard _envelope() shape. apply_archetype_renames
# wraps its writes in atomic_profile_commit to guarantee no half-written
# state, and re-runs hash_profile() so trust is correctly flipped to stale.
# ---------------------------------------------------------------------------


_NODE_KIND_TO_NAME = {
    "ClassDeclaration": "class",
    "ClassNode": "class",
    "ModuleNode": "module",
    "FunctionDeclaration": "function",
    "ArrowFunction": "function",
    "FunctionExpression": "function",
    "InterfaceDeclaration": "interface",
    "TypeAliasDeclaration": "type",
}


def _slugify(value: str) -> str | None:
    """Coerce an arbitrary token to the archetype-name regex shape, or None.

    Public regex: ``^[a-z][a-z0-9-]{0,63}$``. We lowercase, replace any
    non-[a-z0-9-] run with a single hyphen, strip leading/trailing hyphens,
    cap at 64 chars. Returns None on empty / leading-digit candidates.
    """
    import re as _re
    if not isinstance(value, str):
        return None
    candidate = _re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not candidate:
        return None
    if not candidate[0].isalpha():
        return None
    return candidate[:64]


def _propose_alternatives_for(
    current_name: str,
    archetype: dict,
    canonical: dict | None,
) -> list[str]:
    """Build 3-5 candidate names for an archetype rename.

    Inputs are the persisted profile artifacts (archetypes.json entry +
    canonicals.json entry), NOT live ``Cluster`` objects — by the time the
    interview runs the bootstrap has long since released its clustering
    state. We re-derive candidates from the witness path, paths_pattern,
    top-level node kinds, and the current heuristic name.
    """
    import re as _re
    candidates: list[str] = []

    def _push(c: str | None) -> None:
        s = _slugify(c) if c else None
        if s and s not in candidates:
            candidates.append(s)

    # 1. The current heuristic name (so "no rename" is a visible option).
    _push(current_name)

    # 2. Canonical filename stem (e.g., users_controller.rb → users-controller).
    witness_rel = ""
    if canonical:
        witness_rel = (canonical.get("witness") or {}).get("path", "")
    if witness_rel:
        stem = witness_rel.rsplit("/", 1)[-1]
        # strip extension(s) — handle .test.ts, .spec.tsx, etc.
        stem = _re.sub(r"\.[^.]+$", "", stem)
        stem = _re.sub(r"\.[^.]+$", "", stem)  # one more level for .test.ts
        _push(stem)

    # 3. Paths_pattern tail segment (e.g., "app/api/v1" → "v1" is junk,
    #    but "app/services" → "services" is meaningful).
    paths_pattern = archetype.get("paths_pattern", "")
    if paths_pattern:
        segments = [s for s in paths_pattern.split("/") if s]
        for seg in reversed(segments):
            # Skip version-style ids
            if _re.fullmatch(r"v\d+(?:\.\d+)*", seg):
                continue
            _push(seg)
            break

    # 4. Witness-path-tail directory (e.g., "app/controllers/admin/foo.rb"
    #    → "admin"). Distinct from paths_pattern when v5 collapses the
    #    bucket.
    if witness_rel:
        dirs = witness_rel.rsplit("/", 1)[0].split("/")
        # Skip the first segment (typically app/src/lib — too generic)
        if len(dirs) > 1:
            for seg in reversed(dirs[1:]):
                if _re.fullmatch(r"v\d+(?:\.\d+)*", seg):
                    continue
                _push(seg)
                break

    # 5. Top-level node kind → friendly noun (e.g., ClassNode → class).
    #    Only push the friendly mapping — raw kind names like
    #    "FirstStatement" are noise that confuse users.
    kinds = archetype.get("top_level_node_kinds") or []
    if kinds:
        friendly = _NODE_KIND_TO_NAME.get(kinds[0])
        if friendly:
            _push(friendly)

    # 6. JSX hint: a JSX-present cluster is likely a "component".
    if archetype.get("jsx_present"):
        _push("react-component")

    # 7. Combined "current-tail" so collisions still propose a useful split.
    if witness_rel and paths_pattern:
        stem = witness_rel.rsplit("/", 1)[-1]
        stem_clean = _re.sub(r"\.[^.]+$", "", _re.sub(r"\.[^.]+$", "", stem))
        if current_name and stem_clean and stem_clean != current_name:
            _push(f"{current_name}-{stem_clean}")

    return candidates[:5]


def propose_archetype_renames(repo: str, top_n: int = 8) -> dict:
    """Return rename suggestions for the top-N largest archetypes.

    For each archetype the response includes:
    - current_name, cluster_size, canonical_file path
    - suggested_alternatives: 3-5 candidates derived from canonical
      filename, paths_pattern tail, top-level node kinds, etc.

    Drives the chameleon-init interview prompt 1+2 (skill-side prose).
    The MCP is stateless — the skill collects the user's choices and
    submits them as a single mapping via apply_archetype_renames.
    """
    from chameleon_mcp.profile.loader import load_profile_dir

    if not isinstance(top_n, int) or top_n <= 0 or top_n > 64:
        return _envelope({"status": "failed", "error": "top_n must be an int in 1..64"})

    repo_path = Path(repo).expanduser()
    # Accept either a repo_id (resolved via index.db / trust) or an absolute repo path.
    if repo_path.is_absolute():
        if not repo_path.is_dir():
            return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
        repo_root = repo_path.resolve()
    else:
        resolved = _resolve_repo_root_by_id(repo)
        if resolved is None:
            return _envelope({"status": "failed", "error": f"could not resolve repo {repo!r}"})
        repo_root = resolved

    profile_dir = repo_root / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope({"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"})

    try:
        loaded = load_profile_dir(profile_dir)
    except Exception as e:  # pragma: no cover - defensive
        return _envelope({"status": "failed", "error": f"profile load failed: {e}"})

    archetypes = loaded.archetypes.get("archetypes", {}) or {}
    canonicals = loaded.canonicals.get("canonicals", {}) or {}

    ranked = sorted(
        archetypes.items(),
        key=lambda kv: (-int((kv[1] or {}).get("cluster_size", 0)), kv[0]),
    )
    rows = []
    for name, arch in ranked[:top_n]:
        canonical_entries = canonicals.get(name) or []
        canonical_entry = canonical_entries[0] if canonical_entries else None
        canonical_path = ""
        if canonical_entry:
            canonical_path = (canonical_entry.get("witness") or {}).get("path", "")
        alternatives = _propose_alternatives_for(name, arch or {}, canonical_entry)
        rows.append({
            "current_name": name,
            "cluster_size": int((arch or {}).get("cluster_size", 0)),
            "canonical_file": canonical_path,
            "paths_pattern": (arch or {}).get("paths_pattern", ""),
            "suggested_alternatives": alternatives,
        })

    return _envelope({
        "status": "success",
        "repo_id": _compute_repo_id(repo_root),
        "archetypes": rows,
        "total_archetypes": len(archetypes),
    })


def _validate_renames(
    renames: dict,
    existing_names: set[str],
) -> tuple[dict[str, str], str | None]:
    """Validate a user-supplied rename mapping.

    Rules:
    - Keys must be existing archetype names.
    - Values must satisfy ARCHETYPE_NAME_RE (re-validated here to be
      defense-in-depth against a skill that fails to slugify).
    - No two source names may collide to the same target.
    - A rename whose target equals the source is dropped (no-op).
    - No target may collide with an unrenamed existing archetype name.

    Returns (effective_renames, error_or_None). Effective_renames is the
    deduped no-op-stripped mapping ready to apply.
    """
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE

    if not isinstance(renames, dict):
        return {}, "renames must be a dict mapping old_name → new_name"

    effective: dict[str, str] = {}
    seen_targets: set[str] = set()
    for old, new in renames.items():
        if not isinstance(old, str) or not isinstance(new, str):
            return {}, f"rename keys/values must be strings (got {old!r} → {new!r})"
        if old not in existing_names:
            return {}, f"unknown archetype {old!r} (not in committed profile)"
        if not ARCHETYPE_NAME_RE.match(new):
            return {}, (
                f"target name {new!r} must match {ARCHETYPE_NAME_RE.pattern}"
            )
        if old == new:
            continue  # no-op
        if new in seen_targets:
            return {}, f"two renames collide on target {new!r}"
        seen_targets.add(new)
        # Reject targets that collide with an existing name that itself
        # isn't being renamed out — would clobber an unrelated archetype.
        if new in existing_names and new not in renames:
            return {}, f"target {new!r} already exists and is not being renamed away"
        effective[old] = new

    return effective, None


def _rewrite_summary_md(
    profile_data: dict,
    archetypes_data: dict,
    canonicals_data: dict,
    idioms_text: str,
) -> str:
    """Render the user-facing profile.summary.md after a rename.

    The bootstrap orchestrator owns the canonical builder. We can't import
    its private helper without coupling, so we re-emit the same shape here.
    Keep this in sync if the orchestrator's output changes.
    """
    lines = [
        "# chameleon profile summary",
        "",
        f"Generated: {profile_data.get('created_at', '')}",
        f"Engine: chameleon v{profile_data.get('engine_min_version', '')}",
        f"Language: {profile_data.get('language', '')}",
        f"Source: {profile_data.get('source', 'bootstrap')}",
        f"Generation: {profile_data.get('generation', '')}",
        f"Schema version: {profile_data.get('schema_version', '')}",
        "",
        f"## {profile_data.get('archetype_count', 0)} archetypes detected",
        "",
    ]
    for name, arch in sorted(archetypes_data.get("archetypes", {}).items()):
        canonical_entries = canonicals_data.get("canonicals", {}).get(name) or []
        canonical_path = (
            canonical_entries[0]["witness"]["path"]
            if canonical_entries and canonical_entries[0].get("witness")
            else "(none)"
        )
        lines.append(
            f"- **{name}** (cluster_size {arch.get('cluster_size', 0)}, "
            f"paths {arch.get('paths_pattern', '')}) — canonical: `{canonical_path}`"
        )
    lines.extend([
        "",
        "## Rules",
        "",
        "_Phase 2C: tool config rules + AST stats._",
        "",
        "## Idioms",
        "",
    ])

    # Mirror orchestrator's _extract_active_idioms.
    active = ""
    if "## active" in idioms_text:
        after = idioms_text.split("## active", 1)[1]
        active = after.split("\n## ", 1)[0].strip() if "\n## " in after else after.strip()
    if active and "no idioms yet" not in active:
        lines.append(
            "_The following idioms ship in this profile and will be injected "
            "into the model's context before each Edit/Write. Review carefully "
            "before granting trust._"
        )
        lines.append("")
        lines.append(active)
        lines.append("")
    else:
        lines.append(
            "_No idioms captured yet. Run /chameleon-teach to record team "
            "conventions._"
        )
        lines.append("")
    return "\n".join(lines)


def apply_archetype_renames(repo: str, renames: dict) -> dict:
    """Apply an archetype rename mapping atomically.

    Rewrites:
    - archetypes.json: rename keys under "archetypes"
    - canonicals.json: rename keys under "canonicals"
    - rules.json: rename any keys that exactly equal an old archetype name
    - profile.summary.md: regenerate from the renamed data

    Uses atomic_profile_commit so a crash mid-write leaves the previous
    profile untouched. Returns status, renames_applied, new_profile_sha256.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.profile.trust import hash_profile

    repo_path = Path(repo).expanduser()
    if repo_path.is_absolute():
        if not repo_path.is_dir():
            return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
        repo_root = repo_path.resolve()
    else:
        resolved = _resolve_repo_root_by_id(repo)
        if resolved is None:
            return _envelope({"status": "failed", "error": f"could not resolve repo {repo!r}"})
        repo_root = resolved

    profile_dir = repo_root / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope({"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"})

    try:
        loaded = load_profile_dir(profile_dir)
    except Exception as e:  # pragma: no cover - defensive
        return _envelope({"status": "failed", "error": f"profile load failed: {e}"})

    existing = set(loaded.archetypes.get("archetypes", {}).keys())
    effective, err = _validate_renames(renames, existing)
    if err is not None:
        return _envelope({"status": "failed", "error": err})

    if not effective:
        return _envelope({
            "status": "success",
            "renames_applied": 0,
            "new_profile_sha256": hash_profile(profile_dir),
            "note": "no effective renames (all no-ops or empty mapping)",
        })

    # Build the renamed artifacts. Preserve all other fields verbatim.
    archetypes_data = json.loads(json.dumps(loaded.archetypes))  # deep copy
    canonicals_data = json.loads(json.dumps(loaded.canonicals))
    rules_data = json.loads(json.dumps(loaded.rules))
    profile_data = json.loads(json.dumps(loaded.profile))

    arch_map = archetypes_data.get("archetypes", {}) or {}
    canonical_map = canonicals_data.get("canonicals", {}) or {}
    rules_map = rules_data.get("rules", {}) or {}

    new_arch_map: dict = {}
    for k, v in arch_map.items():
        new_arch_map[effective.get(k, k)] = v
    archetypes_data["archetypes"] = new_arch_map

    new_canonical_map: dict = {}
    for k, v in canonical_map.items():
        new_canonical_map[effective.get(k, k)] = v
    canonicals_data["canonicals"] = new_canonical_map

    # rules.json keys are mostly category names ("formatting", "typescript")
    # but a future build may key them by archetype. Rename when the key
    # exactly matches a renamed archetype.
    new_rules_map: dict = {}
    for k, v in rules_map.items():
        new_rules_map[effective.get(k, k)] = v
    rules_data["rules"] = new_rules_map

    idioms_path = profile_dir / "idioms.md"
    idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

    summary_md = _rewrite_summary_md(
        profile_data, archetypes_data, canonicals_data, idioms_text,
    )

    try:
        with atomic_profile_commit(profile_dir) as txn_dir:
            (txn_dir / "profile.json").write_text(
                json.dumps(profile_data, indent=2, sort_keys=True), encoding="utf-8"
            )
            (txn_dir / "archetypes.json").write_text(
                json.dumps(archetypes_data, indent=2, sort_keys=True), encoding="utf-8"
            )
            (txn_dir / "canonicals.json").write_text(
                json.dumps(canonicals_data, indent=2, sort_keys=True), encoding="utf-8"
            )
            (txn_dir / "rules.json").write_text(
                json.dumps(rules_data, indent=2, sort_keys=True), encoding="utf-8"
            )
            (txn_dir / "idioms.md").write_text(idioms_text, encoding="utf-8")
            (txn_dir / "profile.summary.md").write_text(summary_md, encoding="utf-8")
    except Exception as e:
        return _envelope({"status": "failed", "error": f"atomic commit failed: {e}"})

    # Mirror the new profile hash into index.db so list_profiles + trust
    # material-change detection both see the change.
    repo_id = _compute_repo_id(repo_root)
    new_hash = hash_profile(profile_dir)
    try:
        cached = index_db.get_repo(repo_id) or {}
        index_db.upsert_repo(
            repo_id,
            str(repo_root),
            profile_sha256=new_hash,
            archetype_count=cached.get("archetype_count") or len(new_arch_map),
            files_indexed=cached.get("files_indexed"),
            bootstrap_ms=cached.get("bootstrap_ms"),
        )
    except Exception:  # pragma: no cover - index is best-effort
        pass

    return _envelope({
        "status": "success",
        "renames_applied": len(effective),
        "new_profile_sha256": new_hash,
        "renames": effective,
    })


# ---------------------------------------------------------------------------
# Phase 2D.4 — Structured idiom comments
#
# teach_profile_structured accepts the four canonical fields a well-formed
# idiom should always carry (slug, rationale, example, counterexample) plus
# optional archetype + status. It renders to the same idioms.md format that
# the free-form teach_profile uses, then delegates to teach_profile so the
# downstream protections (advisory lock, sanitization, 50KB cap, placeholder
# strip) all apply uniformly.
# ---------------------------------------------------------------------------

_SLUG_RE = __import__("re").compile(r"^[a-z][a-z0-9-]{2,63}$")
_STRUCTURED_TOTAL_CAP = 50_000


def teach_profile_structured(
    repo: str,
    *,
    slug: str,
    rationale: str,
    example: str | None = None,
    counterexample: str | None = None,
    archetype: str | None = None,
    status: str = "active",
) -> dict:
    """Structured-form idiom capture.

    Renders to .chameleon/idioms.md as a fully-formed idiom entry that
    matches the format the chameleon-teach skill emits in free-form mode.

    Validation:
    - slug matches ``^[a-z][a-z0-9-]{2,63}$``
    - rationale must be non-empty after strip
    - len(rationale) + len(example or '') + len(counterexample or '') ≤ 50KB
    - status ∈ {active, deprecated}
    - archetype (if provided) must match the archetype name regex — we
      don't require it to exist in the current profile because the user
      may be capturing an idiom for a renamed/refreshed archetype the
      profile doesn't yet reflect.
    """
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE

    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        return _envelope({
            "status": "failed",
            "error": f"slug must match {_SLUG_RE.pattern!r}",
        })
    if not isinstance(rationale, str) or not rationale.strip():
        return _envelope({"status": "failed", "error": "rationale is required"})
    if status not in ("active", "deprecated"):
        return _envelope({
            "status": "failed",
            "error": "status must be 'active' or 'deprecated'",
        })
    if archetype is not None and not ARCHETYPE_NAME_RE.match(str(archetype)):
        return _envelope({
            "status": "failed",
            "error": (
                f"archetype {archetype!r} must match {ARCHETYPE_NAME_RE.pattern}"
            ),
        })

    total = len(rationale) + len(example or "") + len(counterexample or "")
    if total > _STRUCTURED_TOTAL_CAP:
        return _envelope({
            "status": "failed",
            "error": (
                f"rationale + example + counterexample size {total} exceeds "
                f"50KB cap ({_STRUCTURED_TOTAL_CAP})"
            ),
        })

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    lines: list[str] = [f"### {slug}"]
    if status == "active":
        lines.append(f"Status: active (added {timestamp})")
    else:
        lines.append(f"Status: deprecated {timestamp}")
    if archetype:
        lines.append(f"Archetype: {archetype}")
    lines.append(rationale.strip())
    if example:
        lines.append("")
        lines.append("Example:")
        lines.append("```")
        lines.append(example.rstrip())
        lines.append("```")
    if counterexample:
        lines.append("")
        lines.append("Counterexample:")
        lines.append("```")
        lines.append(counterexample.rstrip())
        lines.append("```")
    rendered = "\n".join(lines)

    # Delegate to teach_profile so we inherit the lock + sanitization +
    # placeholder-strip code path. The leading "### " header is honored.
    return teach_profile(repo, rendered)
