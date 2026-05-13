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
import math
import re
import secrets
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


# v0.5.2 Bug 1: Unify the `repo` argument across every MCP tool.
#
# Pre-v0.5.2 history left tools in an inconsistent state:
#   - get_canonical_excerpt, get_rules, lint_file, get_archetype accepted
#     a 64-char repo_id hex digest.
#   - pause_session, disable_session, teach_profile, teach_profile_structured,
#     refresh_repo, propose_archetype_renames, apply_archetype_renames,
#     bootstrap_repo expected an absolute repo path.
# The asymmetry forced the using-chameleon skill to track two parallel
# vocabularies, which led to four separate dogfood reports of "I passed
# the repo_id and got `expected absolute repo path`."
#
# `_resolve_repo_arg` accepts BOTH forms and returns `(repo_path, repo_id)`.
# Callers MAY rely on either component being None when only the other
# can be derived (e.g., a fresh repo_id that has never been bootstrapped
# resolves to (None, repo_id); a path that lives outside any known repo
# resolves to (path, repo_id) with the id always computable from the
# path's resolved location).

# A repo_id is a SHA-256 hex digest: exactly 64 characters, all hex.
_REPO_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _resolve_repo_arg(repo: str) -> tuple[Path | None, str | None]:
    """Shape-detecting `repo` argument resolver.

    Accepts either form:
      - An absolute or `~`-relative or `./` / `../` path → treated as a
        repo path. `repo_id` is computed via `_compute_repo_id`.
      - A 64-char lowercase hex string → treated as a repo_id. The path
        is resolved via `_resolve_repo_root_by_id`.

    Returns `(repo_path, repo_id)`. Either component may be None:
      - `(None, None)` when the input is neither shape (empty, None,
        wrong length, non-hex). Callers should surface a typed error.
      - `(path, repo_id)` when a path was supplied; both fields are
        populated whenever the path resolves to an existing directory.
      - `(None, repo_id)` when a repo_id was supplied but no row in the
        index nor a trust grant maps it back to an on-disk path.

    Path-shape detection trips on either:
      - String starts with `/`, `~`, `./`, or `../` (explicit POSIX path).
      - String is absolute after `Path.expanduser()` (handles edge cases
        where the caller passed a Windows-style path on macOS/Linux,
        which falls back to id-shape detection naturally).
    The hex check is exclusive of the path check, so a 64-char path like
    `/aaaa…` never gets mis-detected (paths start with `/`, not `[0-9a-f]`).
    """
    if not isinstance(repo, str) or not repo:
        return None, None

    # Path-shape check: explicit prefixes win. A 64-char hex repo_id
    # cannot start with `/`, `~`, or `.`, so this check is unambiguous.
    looks_pathy = repo[0] in ("/", "~") or repo.startswith("./") or repo.startswith("../")
    if not looks_pathy:
        # Hex-shape check: exactly 64 lowercase hex chars.
        if _REPO_ID_RE.match(repo):
            resolved = _resolve_repo_root_by_id(repo)
            return (resolved, repo)
        # Fall-through: maybe the caller passed an unusual path shape
        # (e.g., a Windows-style "C:\foo" or relative "src/foo.ts"). Try
        # expanduser + is_absolute as a last resort.
        try:
            candidate = Path(repo).expanduser()
        except (OSError, ValueError):
            return None, None
        if not candidate.is_absolute():
            return None, None
        # Treated as a path from here on.
        repo_path_str: str = repo
    else:
        repo_path_str = repo

    # Path branch: resolve + compute repo_id when the directory exists.
    try:
        path = Path(repo_path_str).expanduser()
    except (OSError, ValueError):
        return None, None
    if not path.is_absolute():
        return None, None
    if path.is_dir():
        try:
            resolved_path = path.resolve()
        except OSError:
            resolved_path = path
        try:
            return resolved_path, _compute_repo_id(resolved_path)
        except Exception:
            return resolved_path, None
    # Path string supplied but the directory doesn't exist on disk.
    # Return the (path, None) tuple so callers can surface a precise
    # "path is not a directory" error.
    return path, None


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

    Two distinct ``legacy_trust_hint`` surfaces are emitted, mutually
    exclusive by trigger:

    1. **Pre-v0.4 path-id migration** (string hint + ``legacy_repo_id``):
       fires when ``trust_state == "untrusted"`` because the canonical
       (git-remote-derived) id has no record, but the legacy path-derived
       id DOES. The user trusted the repo before v0.4 changed the repo_id
       derivation and just needs to re-grant under the new id.

    2. **v0.5.1 stale-clone hint** (dict, Bug H2): fires when
       ``trust_state == "stale"`` AND the trust record's recorded
       ``repo_root`` doesn't match the current ``repo_root``. Same git
       remote + same id, but the trust was granted on a different
       checkout (a prior calibration run, a teammate's clone synced via
       shared plugin-data, etc.). v0.5.0 surfaced this as a generic
       "stale" with no explanation; v0.5.1 returns a structured envelope
       so the using-chameleon skill can tell the user "you're on a fresh
       clone — re-run /chameleon-trust" instead of "something changed
       inside the profile". Genuine in-place stale (recorded_repo_root
       matches current_repo_root) deliberately does NOT surface the hint
       — that branch is already covered by the standard stale messaging.
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

    # v0.5.2 (Bug 6): path-traversal canonicalization defense. A request
    # like `/home/user/proj/../../../etc/passwd` walks up via `find_repo_root`
    # and lands on `$HOME` itself (or any ancestor of it), which is never
    # a "repo" in the chameleon sense. Pre-v0.5.2 we returned `repo_root:
    # "/Users/<user>"` and `profile_status: "no_profile"` which leaks the
    # username on the response surface — minor info-disclosure. We now
    # detect that case and report `no_repo` exactly as the missing-marker
    # path does. The check covers both `Path.home()` itself and any
    # strict ancestor (e.g., `/Users` on macOS, `/home` on Linux).
    try:
        home = Path.home().resolve()
        resolved = Path(repo_root).resolve()
    except OSError:
        home = None  # type: ignore[assignment]
        resolved = repo_root
    if home is not None and (
        resolved == home
        or resolved in home.parents
        or resolved == Path(resolved.anchor)
    ):
        return _envelope({
            "repo_id": None,
            "repo_root": None,
            "profile_status": "no_repo",
            "trust_state": "n/a",
        })

    repo_id = _compute_repo_id(repo_root)
    profile_dir = repo_root / ".chameleon"
    profile_file = profile_dir / "profile.json"
    profile_present = profile_file.exists()
    trust = trust_state_for(repo_id)

    # BUG-021: detect a corrupted profile.json (unreadable as JSON) and
    # surface a distinct status. Pre-v0.5.6 detect_repo only checked file
    # existence; consumers had no way to know the profile was unreadable
    # until a later get_pattern_context call returned silently-empty data.
    profile_corrupted = False
    profile_unsupported_schema = False
    if profile_present:
        try:
            import json as _json

            with profile_file.open("r", encoding="utf-8") as fh:
                _peek = _json.load(fh)
            # BUG-023 (v0.5.7): the profile-loader already refuses to load
            # schema_version > MAX_SUPPORTED, but detect_repo only opened
            # profile.json to test parseability. A v99 profile reported
            # profile_present and would later fail at load time. Surface
            # the mismatch as its own profile_status so consumers know
            # to upgrade chameleon-mcp rather than re-bootstrap.
            from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

            _sv = _peek.get("schema_version") if isinstance(_peek, dict) else None
            if isinstance(_sv, int) and _sv > MAX_SUPPORTED_SCHEMA_VERSION:
                profile_unsupported_schema = True
        except (OSError, ValueError):
            profile_corrupted = True

    # BUG-005: when no profile exists, "untrusted" is misleading — the schema
    # reserves "n/a" for the "no profile" case. Only consider the trust grant
    # when a profile is actually present and parseable.
    if not profile_present or profile_corrupted or profile_unsupported_schema:
        trust_state = "n/a"
    elif trust is None:
        trust_state = "untrusted"
    elif is_material_change(repo_id, profile_dir):
        trust_state = "stale"
    else:
        trust_state = "trusted"

    # Schema v6 migration helper: when the canonical (git-remote-derived) id
    # has no trust grant but a legacy path-derived id DOES, surface a hint
    # so the model can prompt the user to re-trust under the new id. Skip
    # the check when the two ids happen to be equal (no git remote — the
    # function already returned the legacy id and there's nothing to migrate).
    legacy_id = _legacy_path_repo_id(repo_root)
    legacy_trust_hint_value: str | dict | None = None
    legacy_repo_id_value: str | None = None
    if trust is None and legacy_id != repo_id and trust_state_for(legacy_id) is not None:
        legacy_trust_hint_value = (
            "Trust record found at the legacy (pre-v0.4) path-derived repo_id "
            f"{legacy_id[:8]}…; the canonical repo_id is now derived from the "
            "git remote URL. Run /chameleon-trust to re-grant under the new id."
        )
        legacy_repo_id_value = legacy_id

    # v0.5.1 Bug H2: stale trust against a different recorded repo_root
    # ⇒ this checkout inherited an older clone's trust grant via the
    # shared git-remote repo_id. Surface the recorded vs current paths so
    # the user can distinguish "fresh clone reuse" from "real material
    # change". Pre-condition: trust is not None AND it's stale, so the
    # v0.4 path above could not have fired (that one requires trust is
    # None).
    current_repo_root_str = str(repo_root)
    if (
        trust is not None
        and trust_state == "stale"
        and trust.repo_root
        and trust.repo_root != current_repo_root_str
    ):
        # Suppress the dict hint when the workspace HAS its own per-root
        # trust grant in the new map — in that case the stale flag is
        # about the workspace itself, not the legacy clone path.
        try:
            resolved_current = str(Path(repo_root).resolve())
        except OSError:
            resolved_current = current_repo_root_str
        has_workspace_grant = resolved_current in trust.repo_root_specific_hashes
        if not has_workspace_grant:
            legacy_trust_hint_value = {
                "reason": (
                    "Trust granted previously for a different repo_root "
                    "(likely a prior clone of this repo)"
                ),
                "recorded_repo_root": trust.repo_root,
                "current_repo_root": current_repo_root_str,
                "recommended_action": "Re-run /chameleon-trust on this clone",
            }

    if profile_corrupted:
        profile_status = "profile_corrupted"
    elif profile_unsupported_schema:
        profile_status = "profile_unsupported_schema_version"
    elif profile_present:
        profile_status = "profile_present"
    else:
        profile_status = "no_profile"
    data: dict = {
        "repo_id": repo_id,
        "repo_root": str(repo_root),
        "profile_status": profile_status,
        "trust_state": trust_state,
    }
    if legacy_trust_hint_value is not None:
        data["legacy_trust_hint"] = legacy_trust_hint_value
        if legacy_repo_id_value is not None:
            data["legacy_repo_id"] = legacy_repo_id_value
    return _envelope(data)


def _prefix_overlap_fallback(
    rel_str: str, archetypes: dict
) -> tuple[str | None, list[str]]:
    """BUG-015: pick the archetype that shares the longest directory prefix.

    Returns (primary, alternatives). When no archetype shares at least one
    leading directory segment with the file, returns (None, []).
    """
    file_dir = rel_str.rsplit("/", 1)[0] if "/" in rel_str else ""
    file_segments = [s for s in file_dir.split("/") if s]
    file_ext = rel_str.rsplit(".", 1)[-1] if "." in rel_str.rsplit("/", 1)[-1] else ""
    scored: list[tuple[int, int, str]] = []  # (-overlap, -cluster_size, name)
    for name, arch in archetypes.items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        # paths_pattern may carry a trailing ``:ext`` suffix (v0.5.2+); strip
        # for the prefix comparison and use it as the extension filter.
        if ":" in pattern:
            arch_dir, _, arch_ext = pattern.rpartition(":")
        else:
            arch_dir, arch_ext = pattern, ""
        arch_segments = [s for s in arch_dir.split("/") if s]
        if not arch_segments or not file_segments:
            continue
        overlap = 0
        for fs, asg in zip(file_segments, arch_segments, strict=False):
            if fs == asg:
                overlap += 1
            else:
                break
        if overlap == 0:
            continue
        # If the archetype declares an extension, prefer matches.
        if arch_ext and file_ext and arch_ext != file_ext:
            continue
        cluster_size = int(arch.get("cluster_size") or 0)
        scored.append((-overlap, -cluster_size, name))
    if not scored:
        return None, []
    scored.sort()
    primary = scored[0][2]
    alternatives = [name for _o, _c, name in scored[1:]]
    return primary, alternatives


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

    v0.5.2 (Bug 3): the response envelope's ``content_signal_match`` field
    is now populated whenever the file is readable on disk, by reading
    the first 200 bytes ourselves and calling
    ``signatures.content_signal_match_for``. Earlier versions hardcoded
    ``None`` in every return branch, which made the Phase 2C content
    signal dead code despite being computed inside the lint engine. The
    new wire-through emits a string ("none", "use_client", "use_server",
    "shebang", "ts_pragma") whenever the file head was read, and Python
    ``None`` only when we never looked (file missing, unreadable).

    v0.5.2 (Bug 1) compatibility: this function still computes the
    file's bucket with the v0.5.x extension-blind
    ``path_pattern_bucket_for`` (``include_extension=False``) so v0.5.x
    ``archetypes.json`` files continue to match. New v0.5.2 bootstraps
    write extension-aware buckets (e.g. ``"src/components:tsx"``); we
    also check the extension-aware variant as a secondary key so
    profiles written by v0.5.2 still hit the exact-match path.
    """
    from chameleon_mcp.lint_engine import (
        canonical_confidence,
        detect_language,
        extract_dimensions,
    )
    from chameleon_mcp.profile.loader import LoadedProfile, find_repo_root, load_profile_dir
    from chameleon_mcp.signatures import (
        content_signal_match_for,
        path_pattern_bucket_for,
    )

    p = Path(file_path).expanduser()

    # v0.5.2 (Bug 3): read the first 200 bytes once, up-front, so EVERY
    # return branch can populate `content_signal_match` consistently.
    # When the file is unreadable / missing the field stays None to
    # signal "we didn't look". The full-content read further down (used
    # for AST scoring) is still gated on `p.is_file()` and capped at
    # 100KB — the head-only read here is cheap, bounded by 200 bytes,
    # and runs before any repo / profile validation so the directive is
    # surfaced even when the repo isn't bootstrapped yet (the lint
    # engine and using-chameleon skill both want the signal regardless
    # of profile state).
    file_head: str | None = None
    if p.is_file():
        try:
            file_head = p.read_bytes()[:200].decode("utf-8", errors="replace")
        except OSError:
            file_head = None

    # BUG-NEW-008 (v0.5.7): content_signal_match always returns a string
    # from {"strong", "weak", "none"}. Pre-fix this could be None when the
    # file couldn't be read, contradicting the documented schema. The
    # downstream get_pattern_context envelope had a mix of "none" and null
    # across hit vs miss paths.
    content_signal_value: str = (
        content_signal_match_for(file_head) if file_head is not None else "none"
    )
    if content_signal_value is None:
        content_signal_value = "none"

    repo_root = find_repo_root(p)
    if repo_root is None or _compute_repo_id(repo_root) != repo:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": content_signal_value,
            "confidence_band": "low",
        })

    profile_dir = repo_root / ".chameleon"
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        return _envelope({
            "archetype": None,
            "alternatives": [],
            "content_signal_match": content_signal_value,
            "confidence_band": "low",
        })

    # Compute the file's bucket via the same function clustering used.
    # Match archetypes by EXACT bucket equality (not substring).
    #
    # v0.5.2: keep the v0.5.x extension-blind bucket as the primary key
    # (so old profiles still match) AND check the extension-aware bucket
    # as a secondary match against v0.5.2+ profiles. Either form is a
    # legitimate exact match — we don't prefer one over the other;
    # AST-scoring downstream picks the winner.
    #
    # Path-resolve both sides so /var <-> /private/var symlink shenanigans
    # on macOS don't push `relative_to` into the ValueError branch. The
    # pre-v0.5.2 code path tolerated this only because the substring-
    # fallback check happened to fire on the absolute path; with the
    # extension suffix added by v0.5.2 (Bug 1) that substring no longer
    # matches, so callers on macOS would silently lose all archetype
    # mappings on test-temp-dir paths.
    try:
        p_resolved = p.resolve()
    except OSError:
        p_resolved = p
    try:
        repo_root_resolved = repo_root.resolve()
    except OSError:
        repo_root_resolved = repo_root
    try:
        rel_str = str(p_resolved.relative_to(repo_root_resolved))
    except ValueError:
        try:
            rel_str = str(p.relative_to(repo_root))
        except ValueError:
            rel_str = str(p)
    file_bucket = path_pattern_bucket_for(rel_str)
    file_bucket_ext = path_pattern_bucket_for(rel_str, include_extension=True)

    exact_matches: list[str] = []
    fallback_matches: list[str] = []  # substring fallback if no exact match

    archetypes = loaded.archetypes.get("archetypes", {})
    for name, arch in archetypes.items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        if pattern == file_bucket or pattern == file_bucket_ext:
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
            # BUG-015: last-resort fallback by longest shared directory
            # prefix. A file at app/controllers/application_controller.rb
            # has no exact-bucket match for the ``controller`` cluster
            # (paths_pattern app/controllers/v1) but a user looking at
            # ApplicationController would expect chameleon to suggest the
            # controller archetype as guidance. We pick the archetype
            # whose paths_pattern shares the longest leading directory
            # prefix with the file (>= 1 segment, same extension when
            # the archetype carries one).
            primary, alternatives = _prefix_overlap_fallback(
                rel_str, archetypes
            )
            confidence = "low"
        return _envelope({
            "archetype": primary,
            "alternatives": alternatives,
            "content_signal_match": content_signal_value,
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
        # v0.5.2 (Bug 3): even when we can't run AST scoring, the
        # 200-byte file head was read at the top of the function, so we
        # still surface its directive-match here.
        return _envelope({
            "archetype": exact_matches[0],
            "alternatives": exact_matches[1:],
            "content_signal_match": content_signal_value,
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

    # v0.5.2 (Bug 3): prefer the head-only signal computed up-front so
    # all return branches agree on the {"none", "use_client",
    # "use_server", "shebang", "ts_pragma"} alphabet. The lint engine's
    # snapshot stores ``None`` for "no directive", a different alphabet
    # than `content_signal_match_for` (returns the string "none"). Fall
    # back to `snapshot.content_signal` only if the head read failed
    # despite full-content read succeeding (a contradiction in practice
    # but defensive against future refactors of the read path).
    final_signal = content_signal_value if content_signal_value is not None else (
        snapshot.content_signal if snapshot.content_signal is not None else "none"
    )
    return _envelope({
        "archetype": primary,
        "alternatives": alternatives,
        "content_signal_match": final_signal,
        "confidence_band": confidence,
    })


def _empty_pattern_envelope(
    repo_id: str | None,
    profile_status: str,
    trust_state: str,
) -> dict:
    """Shape of the get_pattern_context response when no archetype data exists.

    BUG-022: both the no-repo / no-profile / profile-corrupted early returns
    must use the same archetype envelope shape as the healthy path. Pre-v0.5.6
    we returned ``archetype.name`` (typo of ``archetype.archetype``) and
    dropped ``content_signal_match`` and ``idioms`` entirely. Consumers parsing
    the response then tripped on the key change.
    """
    return {
        "repo": {
            "id": repo_id,
            "profile_status": profile_status,
            "trust_state": trust_state,
        },
        "archetype": {
            "archetype": None,
            "alternatives": [],
            "content_signal_match": "none",
            "confidence_band": "low",
        },
        "canonical_excerpt": {
            "content": "",
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        },
        "rules": [],
        "idioms": "",
        "meta": {"mtime_token": None, "computed_at": None},
    }


def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical + rules + meta in one round trip.

    Phase 2D: returns real archetype data when profile is present + trusted.
    """
    from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir
    from chameleon_mcp.profile.trust import trust_state_for

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(
            _empty_pattern_envelope(None, "no_repo", "n/a")
        )

    repo_id = _compute_repo_id(repo_root)
    profile_dir = repo_root / ".chameleon"
    profile_file = profile_dir / "profile.json"
    if not profile_file.exists():
        return _envelope(
            _empty_pattern_envelope(repo_id, "no_profile", "n/a")
        )

    # BUG-021/022: detect corrupted profile.json here too so the response
    # carries an explicit status and the consistent envelope shape.
    try:
        import json as _json

        with profile_file.open("r", encoding="utf-8") as fh:
            _json.load(fh)
    except (OSError, ValueError):
        return _envelope(
            _empty_pattern_envelope(repo_id, "profile_corrupted", "n/a")
        )

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
        return _envelope(
            _empty_pattern_envelope(repo_id, "profile_corrupted", "n/a")
        )

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
                try:
                    from chameleon_mcp.safe_open import UnsafeFileError, safe_read_text
                    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

                    content = safe_read_text(repo_root, witness_rel, max_size_bytes=200_000)
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
                except (UnsafeFileError, FileNotFoundError, OSError):
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


def _resolve_repo_root_by_id(
    repo_id: str, repo_root_hint: str | None = None
) -> Path | None:
    """Map a repo_id back to its repo_root.

    Phase 4.4 lookup order:
      1. index.db (primary; populated by bootstrap_repo on success)
      2. trust record's repo_root (backward compat with v0.1/v0.2 installs
         that bootstrapped before index.db existed)

    v0.5.1 (Bug 1): monorepo sub-workspaces share a git-remote-derived
    repo_id with the root, so a single repo_id may now resolve to
    multiple candidate roots. When the caller knows which workspace it
    is asking about (e.g., refresh_repo just resolved the absolute path),
    it passes `repo_root_hint` so index.db returns the matching row
    instead of the freshest-overall one.

    Returns None if neither layer resolves to an existing directory.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import trust_state_for

    # Primary: index.db (with optional repo_root pinning for monorepos).
    indexed = index_db.resolve_repo_root(repo_id, repo_root_hint=repo_root_hint)
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
    """Return the annotated canonical excerpt for an archetype.

    v0.5.2 (Bug 5): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. Pre-v0.5.2 the function only accepted
    repo_ids and silently returned `{content: "", witness_path: null,
    truncated: false}` when handed a path. Now we shape-detect via
    `_resolve_repo_arg` and emit an explicit `{status: failed, error:
    "repo_id not found"}` envelope for unresolvable input so callers
    can distinguish "no archetype" from "wrong arg shape".

    v0.5.3 (Bug A): the "valid repo, valid archetype name, but the
    archetype has no canonical witness in canonicals.json" path was
    equally silent — the witness can be rejected at bootstrap time
    because every candidate contained secrets / was too long / the
    cluster fell below the confidence threshold. Callers (the
    using-chameleon skill, IDE integrations) couldn't distinguish that
    from a transient I/O failure. We now emit three typed envelopes:
      - `status: "failed", error: "repo_id not found"` — unresolvable
        `repo` argument (unchanged from v0.5.2).
      - `status: "failed", error: "archetype not found"` — the
        `archetype` name isn't in archetypes.json (was previously
        conflated with "no witness").
      - `status: "no_witness"` — archetype name resolves but
        canonicals.json carries no usable entry (bootstrap-time
        rejection).
    The legacy `content / witness_path / truncated / sha_hint` keys
    stay in every envelope so callers reading them by name don't crash;
    they're `None` / `False` when not applicable.
    """
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None and resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "repo_id not found",
            "content": None,
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })
    repo_root = resolved_path
    if repo_root is None and repo_id is not None:
        repo_root = _resolve_repo_root_by_id(repo_id)
    if repo_root is None:
        return _envelope({
            "status": "failed",
            "error": "repo_id not found",
            "content": None,
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

    # v0.5.3 Bug A: distinguish "archetype name unknown" from "archetype
    # known but witness was dropped at bootstrap". `archetypes.json` is
    # the source of truth for whether the name exists; `canonicals.json`
    # only carries entries for archetypes that survived the witness
    # selection scan (secret / injection / poisoning gates).
    known_archetypes = loaded.archetypes.get("archetypes", {}) or {}
    if archetype not in known_archetypes:
        return _envelope({
            "status": "failed",
            "error": "archetype not found",
            "archetype_name": archetype,
            "repo_id": repo_id,
            "content": None,
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })

    canonicals = loaded.canonicals.get("canonicals", {}).get(archetype, [])
    if not canonicals:
        return _envelope({
            "status": "no_witness",
            "reason": (
                "archetype has no canonical witness (below confidence "
                "threshold, or all candidates contained secrets)"
            ),
            "archetype_name": archetype,
            "repo_id": repo_id,
            "content": None,
            "witness_path": None,
            "truncated": False,
            "sha_hint": None,
        })

    first = canonicals[0]
    witness = first.get("witness", {}) or {}
    witness_rel = witness.get("path")
    if not witness_rel:
        # Canonicals row exists but lacks a usable witness path. Same
        # observable state as a dropped-at-bootstrap entry — surface the
        # typed `no_witness` envelope so the caller doesn't have to
        # reason about partially-populated rows.
        return _envelope({
            "status": "no_witness",
            "reason": (
                "archetype has no canonical witness (below confidence "
                "threshold, or all candidates contained secrets)"
            ),
            "archetype_name": archetype,
            "repo_id": repo_id,
            "content": None,
            "witness_path": None,
            "truncated": False,
            "sha_hint": witness.get("sha_hint"),
        })

    try:
        from chameleon_mcp.safe_open import UnsafeFileError, safe_read_text

        content = safe_read_text(repo_root, witness_rel, max_size_bytes=200_000)
    except (UnsafeFileError, FileNotFoundError, OSError):
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
    """Report freshness for a repo.

    Computes:
    - days_since_refresh from the trust record's granted_at
    - observed_drift_score from drift.db's recent edit_observations
      (None if no observations yet)
    - recommended_action: combines both signals

    v0.5.2 (Bug 4): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. Pre-v0.5.2, passing a path silently
    routed it to `plugin_data_dir() / <path>` which is never a real
    directory; the user got a confusing envelope echoing the path back
    as `repo_id`. Now we shape-detect via `_resolve_repo_arg`:
      - Path-shaped input  → resolve to repo_id, then proceed.
      - 64-char hex input  → keep current behavior (treat as repo_id).
      - Empty / None input → explicit error envelope.
      - Path-shaped junk (absolute path that doesn't exist) → error
        envelope (no more echoing it back as repo_id).
      - Opaque non-path non-hex string → preserved legacy behavior
        (treat as opaque plugin_data dir key) so drift-observation
        callers that construct synthetic ids keep working.
    """
    import time

    from chameleon_mcp.drift.observations import compute_drift_score
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    if not isinstance(repo, str) or not repo:
        return _envelope({
            "status": "failed",
            "error": "expected repo path or repo_id hex digest",
        })

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None and resolved_path is not None:
        # Path-shaped input that didn't resolve (directory doesn't
        # exist). Without my v0.5.2 fix this would silently echo the
        # bogus path back as repo_id; emit a typed error instead.
        return _envelope({
            "status": "failed",
            "error": "expected repo path or repo_id hex digest",
        })
    if repo_id is None:
        # Neither a path nor a 64-char hex digest. The legacy code
        # treated arbitrary strings as opaque keys into plugin_data_dir;
        # drift-recording callers (record_edit_observation) rely on
        # this. We keep that behavior — the path-shape gate above is
        # what closes the Bug 4 misrouting class without breaking the
        # opaque-id consumers. Reject path-traversal payloads explicitly
        # so an attacker-controlled opaque key cannot escape the
        # plugin-data-dir sandbox via `..` segments.
        if "/" in repo or ".." in repo or "\\" in repo:
            return _envelope({
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            })
        repo_id = repo

    repo_data = plugin_data_dir() / repo_id
    trust = trust_state_for(repo_id) if repo_data.is_dir() else None

    days_since_refresh: int | None = None
    if trust is not None and trust.granted_at:
        try:
            # BUG-NEW-023 (v0.5.7): use calendar.timegm to interpret the
            # ISO timestamp as UTC. trust.granted_at is written with
            # time.gmtime() upstream, so reading it with time.mktime
            # (local TZ) produced a tz_offset drift in days_since_refresh.
            # Confirmed 8h offset on PST in the v0.5.7 audit tests.
            import calendar as _calendar

            granted_epoch = _calendar.timegm(
                time.strptime(trust.granted_at, "%Y-%m-%dT%H:%M:%SZ")
            )
            days_since_refresh = max(0, int((time.time() - granted_epoch) / 86_400))
        except ValueError:
            days_since_refresh = None

    drift_score = compute_drift_score(repo_id)

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
        "repo_id": repo_id,
        "days_since_refresh": days_since_refresh,
        "observed_drift_score": drift_score,
        "recommended_action": recommended,
    })


# Phase 4.3-extended: change_ratio above this threshold falls through to a
# full re-bootstrap. The constraint is fixed in the design doc; expose it
# as a module-level constant so tests can verify the boundary without
# duplicating the literal.
PARTIAL_REFRESH_CHANGE_RATIO_CEILING = 0.10


def _content_sha_hint(path: Path) -> str | None:
    """xxhash64 hex digest of a file's content, or None if unreadable.

    Mirrors `extractors.typescript._parsed_file_from_record` so the
    file_clusters sha_hint stored at bootstrap time can be re-compared
    byte-for-byte during refresh without rerunning the extractor on
    unchanged files. xxhash64 is sufficient for change detection — we
    are not relying on it for cryptographic integrity (canonical
    selection runs its own scanners on every chosen witness).
    """
    try:
        import xxhash
    except ImportError:
        return None
    try:
        return xxhash.xxh64(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_cluster_key_for(key) -> str:
    """Compute the 16-char cluster_id hash for a ClusterKey.

    Mirrors `bootstrap.canonical._hash_cluster_key` exactly so the
    cluster_ids stored in file_clusters match the cluster_ids written
    into archetypes.json. Duplicating the 4-line helper here keeps
    `tools.py` independent of `canonical.py` for this code path; the
    upstream helper is private to the bootstrap layer.
    """
    canonical = json.dumps(key.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _compute_file_cluster_map(
    repo_root: Path, paths_glob: str | None = None
) -> list[tuple[str, str, str | None]] | None:
    """Re-run discover+parse+cluster to derive each file's cluster_id.

    Returns a list of `(rel_path, cluster_id, sha_hint)` rows ready to
    feed `index_db.upsert_file_clusters`, or None when the repo has no
    supported extractor / discovery raised / nothing was clustered. The
    caller treats None as "skip file_clusters population for this repo"
    — partial refresh becomes unavailable but full re-bootstrap still
    works (file_clusters is opportunistic).

    This is a second pass on top of the orchestrator's bootstrap; the
    orchestrator does not expose the per-file → cluster mapping in its
    BootstrapReport, and the file_clusters write requires it. The cost
    is bounded by REPO_SIZE_GUARD (200_000 files; v0.5.3) and runs synchronously
    after the atomic profile commit so a partial failure here cannot
    corrupt the committed profile.
    """
    from chameleon_mcp.bootstrap.clustering import cluster_files
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.bootstrap.orchestrator import (
        _glob_for_extractor,
        _select_extractor,
    )

    try:
        extractor = _select_extractor(repo_root)
    except Exception:
        return None
    if extractor is None:
        return None

    discovery_glob = paths_glob or _glob_for_extractor(extractor)
    try:
        candidates = discover_files(
            repo_root, glob=discovery_glob, paths_glob=paths_glob
        )
    except Exception:
        return None
    if not candidates:
        return []

    try:
        parse_result = extractor.parse_repo(repo_root, paths=candidates)
    except Exception:
        return None

    clustering = cluster_files(parse_result.files, repo_root=repo_root)

    rows: list[tuple[str, str, str | None]] = []
    for cluster in clustering.clusters:
        cluster_id = _hash_cluster_key_for(cluster.key)
        for pf in cluster.members:
            try:
                rel = str(pf.path.relative_to(repo_root))
            except ValueError:
                rel = str(pf.path)
            rows.append((rel, cluster_id, pf.sha_hint))
    return rows


def _reparse_changed_files(
    repo_root: Path, paths: list[Path]
) -> dict[str, tuple[str, str | None]] | None:
    """Re-parse a subset of files and return their new cluster_ids.

    Returns `{rel_path: (cluster_id, sha_hint)}` for each path that
    successfully parsed + clustered. Returns None if the extractor or
    parse step itself failed — caller should bail to full re-bootstrap.

    The relativization uses `repo_root` (which the caller resolved
    already) so the rel_paths match the keys stored in file_clusters.
    """
    from chameleon_mcp.bootstrap.clustering import cluster_files
    from chameleon_mcp.bootstrap.orchestrator import _select_extractor

    if not paths:
        return {}

    try:
        extractor = _select_extractor(repo_root)
    except Exception:
        return None
    if extractor is None:
        return None

    try:
        parse_result = extractor.parse_repo(repo_root, paths=paths)
    except Exception:
        return None

    clustering = cluster_files(parse_result.files, repo_root=repo_root)
    out: dict[str, tuple[str, str | None]] = {}
    for cluster in clustering.clusters:
        cluster_id = _hash_cluster_key_for(cluster.key)
        for pf in cluster.members:
            try:
                rel = str(pf.path.relative_to(repo_root))
            except ValueError:
                rel = str(pf.path)
            out[rel] = (cluster_id, pf.sha_hint)
    return out


def _attempt_partial_refresh(
    repo_root: Path,
    repo_id: str,
    profile_dir: Path,
    candidates: list[Path],
    prev_state: dict[str, dict[str, str | None]],
    started_at: float,
) -> dict | None:
    """Try to perform a partial re-clustering. Returns the envelope on
    success, or None to signal "fall through to full bootstrap".

    Algorithm (per Phase 4.3-extended design):
      1. Compute current sha_hint for every candidate.
      2. Diff against prev_state → {unchanged, modified, added, removed}.
      3. Compute change_ratio. If > 10% → return None (caller falls back).
      4. Re-parse only the modified+added files.
      5. If any re-parsed file lands in a NEW cluster (not in
         archetypes.json), return None — canonical selection for new
         clusters needs the full corpus.
      6. If a modified file's prev cluster_id has only one canonical
         witness AND that witness is the file itself, return None —
         canonical re-selection needs the full cluster, which we don't
         have in the partial path.
      7. Otherwise, amend archetypes.json's cluster_size (add/sub
         members), then atomic-commit profile.json + archetypes.json +
         canonicals.json + rules.json + idioms.md + summary.
      8. Update file_clusters rows and return the partial envelope.

    Returns None on ANY failure that hasn't already mutated state. The
    only state mutations happen inside `atomic_profile_commit`, which
    is self-rolling-back on exception, so a bail-out here always leaves
    the profile intact.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
    from chameleon_mcp.profile.trust import hash_profile

    # Step 1: compute current sha + index by rel_path.
    current_by_rel: dict[str, dict] = {}
    for p in candidates:
        try:
            rel = str(p.relative_to(repo_root))
        except ValueError:
            continue
        current_by_rel[rel] = {
            "path": p,
            "sha_hint": _content_sha_hint(p),
        }

    # Step 2: diff against prev_state.
    unchanged: list[str] = []
    modified: list[str] = []
    added: list[str] = []
    for rel, info in current_by_rel.items():
        prev = prev_state.get(rel)
        if prev is None:
            added.append(rel)
        elif prev.get("sha_hint") == info["sha_hint"] and info["sha_hint"] is not None:
            unchanged.append(rel)
        else:
            modified.append(rel)
    removed = [rel for rel in prev_state if rel not in current_by_rel]

    # Step 3: change ratio. Use len(prev_state) as the denominator so a
    # repo with 100 files where 9 are modified registers 9% (under the
    # 10% ceiling) rather than 9/109 = 8.3% (which would also pass but
    # for a noisier reason). The design doc fixes the formula:
    # `(modified + added + removed) / max(1, len(prev_state))`.
    change_count = len(modified) + len(added) + len(removed)
    denom = max(1, len(prev_state))
    change_ratio = change_count / denom
    if change_ratio > PARTIAL_REFRESH_CHANGE_RATIO_CEILING:
        return None
    if change_count == 0:
        # No change at all — but we got here because the no-op
        # short-circuit failed (idioms.md mtime newer, etc.). Fall
        # through so the full bootstrap re-renders summary.md.
        return None

    # Step 4: load existing archetypes + canonicals to plan the amend.
    try:
        archetypes_data = json.loads(
            (profile_dir / "archetypes.json").read_text(encoding="utf-8")
        )
        canonicals_data = json.loads(
            (profile_dir / "canonicals.json").read_text(encoding="utf-8")
        )
        profile_data = json.loads(
            (profile_dir / "profile.json").read_text(encoding="utf-8")
        )
        rules_data = json.loads(
            (profile_dir / "rules.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None

    # Build a cluster_id → archetype_name map for fast lookup.
    archetypes = archetypes_data.get("archetypes", {}) or {}
    cluster_id_to_archetype: dict[str, str] = {}
    for name, arch in archetypes.items():
        cid = (arch or {}).get("cluster_id")
        if cid:
            cluster_id_to_archetype[cid] = name

    # Step 5: re-parse modified+added.
    reparse_paths = [
        current_by_rel[rel]["path"] for rel in (modified + added)
    ]
    reparsed = _reparse_changed_files(repo_root, reparse_paths)
    if reparsed is None:
        return None

    # If any modified+added file lacks a re-parse entry (e.g. ts_dump
    # skipped it for syntax errors), we can't safely place it — bail.
    for rel in modified + added:
        if rel not in reparsed:
            return None

    # Step 6: every re-parsed file must land in a known cluster. A new
    # cluster_id means we'd need to run canonical selection on a fresh
    # cluster, which requires the full corpus.
    for rel in modified + added:
        new_cid, _ = reparsed[rel]
        if new_cid not in cluster_id_to_archetype:
            return None

    # Step 7: check canonical-witness integrity. A modified file that
    # IS the current canonical witness for its archetype forces a
    # canonical re-selection — but the partial path only has the changed
    # subset, not the full cluster. Bail unless the witness happens to
    # be unchanged among modified files (the modified file could be
    # someone else in the same cluster).
    canonicals = canonicals_data.get("canonicals", {}) or {}
    for rel in modified + removed:
        # Determine which archetype name this rel was a member of.
        prev = prev_state.get(rel)
        if prev is None:
            continue
        prev_arch = cluster_id_to_archetype.get(prev.get("cluster_id") or "")
        if prev_arch is None:
            continue
        entries = canonicals.get(prev_arch) or []
        if not entries:
            continue
        witness_rel = (entries[0].get("witness") or {}).get("path")
        if witness_rel == rel:
            # The canonical witness itself moved/changed — full re-bootstrap
            # is the only way to re-select a clean canonical for the cluster.
            return None

    # Step 8: amend archetypes.json. For each archetype, compute net
    # delta = (new members added in this cluster) - (members removed +
    # members that moved out via modification). A member that moves
    # FROM cluster A TO cluster B contributes -1 to A and +1 to B.
    #
    # Build {cluster_id: net_delta}. For each *prev_state* file, record
    # its prev cluster contribution. For each *current* file (unchanged
    # + reparsed), record its current contribution. Difference = delta.

    prev_membership: dict[str, int] = {}
    for _rel, prev in prev_state.items():
        cid = prev.get("cluster_id") or ""
        prev_membership[cid] = prev_membership.get(cid, 0) + 1

    current_membership: dict[str, int] = {}
    for rel in unchanged:
        prev = prev_state.get(rel)
        if prev is None:
            continue
        cid = prev.get("cluster_id") or ""
        current_membership[cid] = current_membership.get(cid, 0) + 1
    for rel in modified + added:
        new_cid, _ = reparsed[rel]
        current_membership[new_cid] = current_membership.get(new_cid, 0) + 1

    # Per-archetype size update.
    new_archetypes = dict(archetypes)
    for cid, archetype_name in cluster_id_to_archetype.items():
        new_size = current_membership.get(cid, 0)
        existing = dict(new_archetypes.get(archetype_name, {}) or {})
        existing["cluster_size"] = new_size
        new_archetypes[archetype_name] = existing

    archetypes_amended = sum(
        1
        for cid, name in cluster_id_to_archetype.items()
        if (current_membership.get(cid, 0) != prev_membership.get(cid, 0))
    )
    archetypes_unchanged = len(cluster_id_to_archetype) - archetypes_amended

    # Step 9: bump generation. The loader requires all four artifacts
    # to share the same generation counter. Reuse the existing
    # transaction module's pattern.
    new_generation = int(started_at)
    archetypes_data["archetypes"] = new_archetypes
    archetypes_data["generation"] = new_generation
    canonicals_data["generation"] = new_generation
    profile_data["generation"] = new_generation
    rules_data["generation"] = new_generation

    # Recompute archetype_count from the live archetypes dict (a member
    # could have moved a sparse cluster into an empty state, but we
    # don't drop the archetype — that would invalidate the cluster_id
    # reverse-lookup. cluster_size = 0 archetypes simply mean "all
    # members moved elsewhere," which the next full bootstrap cleans up).
    profile_data["archetype_count"] = len(new_archetypes)

    # Read idioms.md + summary.md so we re-emit them inside the same
    # transaction (the loader's double-fstat check requires the same
    # generation across all artifacts and idioms.md influences summary).
    idioms_text = ""
    idioms_path = profile_dir / "idioms.md"
    if idioms_path.is_file():
        try:
            idioms_text = idioms_path.read_text(encoding="utf-8")
        except OSError:
            idioms_text = ""

    summary_text = ""
    summary_path = profile_dir / "profile.summary.md"
    if summary_path.is_file():
        try:
            summary_text = summary_path.read_text(encoding="utf-8")
        except OSError:
            summary_text = ""

    # v0.5.1 (Bug 3): preserve `.chameleon/renames.json` across the
    # atomic_profile_commit's dir-replacement so the user's rename
    # mapping survives a partial refresh.
    renames_text: str | None = None
    renames_path_partial = profile_dir / "renames.json"
    if renames_path_partial.is_file():
        try:
            renames_text = renames_path_partial.read_text(encoding="utf-8")
        except OSError:
            renames_text = None

    # Step 10: atomic commit. Any exception here leaves the existing
    # profile untouched.
    try:
        with atomic_profile_commit(profile_dir) as txn_dir:
            (txn_dir / "profile.json").write_text(
                json.dumps(profile_data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (txn_dir / "archetypes.json").write_text(
                json.dumps(archetypes_data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (txn_dir / "canonicals.json").write_text(
                json.dumps(canonicals_data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (txn_dir / "rules.json").write_text(
                json.dumps(rules_data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (txn_dir / "idioms.md").write_text(idioms_text, encoding="utf-8")
            (txn_dir / "profile.summary.md").write_text(
                summary_text, encoding="utf-8"
            )
            if renames_text is not None:
                (txn_dir / "renames.json").write_text(
                    renames_text, encoding="utf-8"
                )
    except Exception:
        # atomic_profile_commit guarantees on-disk state is unchanged
        # on exception. Bail to full bootstrap.
        return None

    # Step 11: update file_clusters. Insert/update added+modified rows
    # with their new cluster_id + sha; delete removed rows; touch
    # unchanged rows so last_seen_at moves forward (helps drift
    # diagnostics).
    upsert_rows: list[tuple[str, str, str | None]] = []
    for rel in modified + added:
        new_cid, new_sha = reparsed[rel]
        upsert_rows.append((rel, new_cid, new_sha))
    for rel in unchanged:
        prev = prev_state[rel]
        upsert_rows.append((
            rel,
            prev.get("cluster_id") or "",
            current_by_rel[rel].get("sha_hint") or prev.get("sha_hint"),
        ))
    if upsert_rows:
        index_db.upsert_file_clusters(repo_id, upsert_rows)
    if removed:
        index_db.delete_file_clusters_for_paths(repo_id, removed)

    # Step 12: refresh index.db row metadata.
    duration_ms = int((time.time() - started_at) * 1000)
    files_processed = len(unchanged) + len(modified) + len(added)
    index_db.upsert_repo(
        repo_id,
        str(repo_root),
        profile_sha256=hash_profile(profile_dir),
        archetype_count=profile_data["archetype_count"],
        files_indexed=files_processed,
        bootstrap_ms=duration_ms,
    )

    return _envelope({
        "status": "partial_refresh",
        "files_changed": len(modified),
        "files_added": len(added),
        "files_removed": len(removed),
        "files_processed": files_processed,
        "duration_ms": duration_ms,
        "archetypes_unchanged": archetypes_unchanged,
        "archetypes_amended": archetypes_amended,
        "archetypes_detected": profile_data["archetype_count"],
        "profile_path": str(profile_dir),
        "change_ratio": round(change_ratio, 4),
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

    Phase 4.3-extended adds a partial-refresh path for repos where
    ≤10% of files have changed since the last bootstrap. The partial
    path re-parses only the modified+added files and amends
    archetypes.json / canonicals.json / profile.json in place via the
    same atomic_profile_commit pattern. Repos without per-file cluster
    state in index.db (legacy v0.4 profiles, or any repo where the
    initial bootstrap predates this feature) fall through to full
    re-bootstrap unconditionally.

    `force=True` bypasses BOTH short-circuits and always re-bootstraps.

    v0.5.2 (Bug 1): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.bootstrap.orchestrator import (
        _glob_for_extractor,
        _select_extractor,
    )

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    repo_path = resolved_path
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope({
            "status": "failed",
            "error": "refresh_repo expects an absolute repo path",
        })

    started_at = time.time()

    if force:
        # BUG-026: refresh_repo's internal bootstrap calls always overwrite
        # the existing profile (that's the whole point of refresh); skip the
        # already_bootstrapped guard meant for accidental re-invocation.
        return bootstrap_repo(str(repo_path), force=True)

    # Phase 4.3 no-op optimization.
    repo_root = repo_path.resolve()
    repo_id = _compute_repo_id(repo_root)
    # v0.5.1 (Bug 1): pin the lookup to this repo_root so a monorepo
    # sub-workspace doesn't surface a sibling workspace's cached row.
    cached = index_db.get_repo(repo_id, repo_root_hint=str(repo_root))
    profile_dir = repo_root / ".chameleon"
    profile_path = profile_dir / "profile.json"

    if not (cached and profile_path.is_file()):
        # BUG-026: refresh_repo's internal bootstrap calls always overwrite
        # the existing profile (that's the whole point of refresh); skip the
        # already_bootstrapped guard meant for accidental re-invocation.
        return bootstrap_repo(str(repo_path), force=True)

    try:
        extractor = _select_extractor(repo_root)
    except Exception:
        extractor = None
    if extractor is None:
        # BUG-026: refresh_repo's internal bootstrap calls always overwrite
        # the existing profile (that's the whole point of refresh); skip the
        # already_bootstrapped guard meant for accidental re-invocation.
        return bootstrap_repo(str(repo_path), force=True)

    try:
        candidates = discover_files(
            repo_root, glob=_glob_for_extractor(extractor)
        )
    except Exception:
        # BUG-026: refresh_repo's internal bootstrap calls always overwrite
        # the existing profile (that's the whole point of refresh); skip the
        # already_bootstrapped guard meant for accidental re-invocation.
        return bootstrap_repo(str(repo_path), force=True)

    cached_files = cached.get("files_indexed") or 0
    last_seen_iso = cached.get("last_seen_at") or ""
    last_seen_epoch = _iso_to_epoch(last_seen_iso)
    # Include idioms.md in the freshness check: a fresh /chameleon-teach
    # must invalidate the no-op so the transaction re-renders
    # profile.summary.md with the new idiom body (the trust-review
    # surface). idioms.md is the only file outside the discovery glob
    # whose content affects committed profile artifacts.
    idioms_path = profile_dir / "idioms.md"
    refresh_inputs = list(candidates) + [idioms_path]
    max_mtime = index_db.max_mtime_over(refresh_inputs)
    cardinality_match = cached_files > 0 and len(candidates) == cached_files
    nothing_newer = last_seen_epoch > 0.0 and max_mtime <= last_seen_epoch

    if cardinality_match and nothing_newer:
        # Touch the row so the repo bubbles to the top of list_profiles
        # even on a no-op refresh.
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

    # Phase 4.3-extended: try partial refresh before falling back to
    # full re-bootstrap. Requires (a) file_clusters rows for this repo,
    # and (b) a change_ratio in (0, 0.10]. Repos without rows or above
    # the ceiling go straight to bootstrap_repo (the legacy path).
    prev_state = index_db.get_file_clusters(repo_id)
    if prev_state:
        partial_envelope = _attempt_partial_refresh(
            repo_root,
            repo_id,
            profile_dir,
            list(candidates),
            prev_state,
            started_at,
        )
        if partial_envelope is not None:
            return partial_envelope

    # BUG-026: full re-bootstrap from inside refresh — always overwrite.
    return bootstrap_repo(str(repo_path), force=True)


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


def bootstrap_repo(
    path: str,
    mode: str = "full",
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit.

    v0.4 (2D.3): for monorepos with detected workspace_paths, runs the full
    pipeline per workspace as well, producing one `.chameleon/` under each
    workspace root in addition to the root profile that catalogs them.

    v0.5.2 (Bug 1): `path` accepts either an absolute repo path or a
    64-char repo_id hex digest (for repos previously bootstrapped). See
    `_resolve_repo_arg`.

    v0.5.6 (BUG-026): refuses to overwrite a committed profile unless
    ``force=True``. Pre-v0.5.6 a second call silently clobbered the
    existing profile; the /chameleon-init skill warned the model but the
    MCP had no defense in depth.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap
    from chameleon_mcp.profile.trust import hash_profile

    resolved_path, _resolved_id = _resolve_repo_arg(path)
    if resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    try:
        repo_root = resolved_path.resolve()
    except OSError:
        repo_root = resolved_path
    if not repo_root.is_dir():
        return _envelope({
            "status": "failed",
            "error": f"path is not a directory: {path}",
        })

    # Validate the `now` parameter: reject NaN, +/-inf, negative values, and
    # non-numeric types. Otherwise NaN/inf raise OverflowError/ValueError deep
    # in clustering; negative values produce nonsense recency weights silently.
    if now is not None:
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            return _envelope({
                "status": "failed",
                "error": f"now must be a finite non-negative float; got {type(now).__name__}",
            })
        now_f = float(now)
        if math.isnan(now_f) or math.isinf(now_f) or now_f < 0:
            return _envelope({
                "status": "failed",
                "error": f"now must be a finite non-negative float; got {now_f!r}",
            })

    # BUG-026: guard against accidental overwrite. A committed profile is
    # marked by the COMMITTED sentinel inside .chameleon/ (atomic write).
    if not force:
        committed_marker = repo_root / ".chameleon" / "COMMITTED"
        if committed_marker.is_file():
            profile_path = str(repo_root / ".chameleon")
            # Register the repo in index.db so list_profiles and other
            # index-backed tools see it. New team members who clone a repo
            # with a checked-in .chameleon/ hit this path and would otherwise
            # be invisible to the index until they ran a full bootstrap.
            try:
                repo_id = _compute_repo_id(repo_root)
                profile_dir = repo_root / ".chameleon"
                _arch_count: int | None = None
                try:
                    import json as _json
                    _arc_data = _json.loads(
                        (profile_dir / "archetypes.json").read_text(encoding="utf-8")
                    )
                    _arch_count = len(_arc_data.get("archetypes", {}))
                except Exception:
                    pass
                index_db.upsert_repo(
                    repo_id,
                    str(repo_root),
                    profile_sha256=hash_profile(profile_dir),
                    archetype_count=_arch_count,
                    files_indexed=None,
                    bootstrap_ms=None,
                )
            except Exception:
                pass
            return _envelope({
                "status": "already_bootstrapped",
                "profile_path": profile_path,
                "message": (
                    "A committed profile already exists at this path. "
                    "Pass force=true to overwrite, or run /chameleon-refresh "
                    "to re-analyze without clearing trust state."
                ),
            })

    del mode  # forward-compat for Phase 2D interview mode
    report = _bootstrap(repo_root, paths_glob=paths_glob, now=now)

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
        # Phase 4.3-extended: populate file_clusters so a subsequent
        # /chameleon-refresh can take the partial path. We replace the
        # whole repo's rows so a re-bootstrap after a major refactor
        # doesn't leave stale (cluster_id, rel_path) entries pointing at
        # clusters that no longer exist in the new profile. Errors here
        # are non-fatal — the worst case is that the next refresh falls
        # through to full bootstrap.
        try:
            file_cluster_rows = _compute_file_cluster_map(
                repo_root, paths_glob=paths_glob
            )
        except Exception:
            file_cluster_rows = None
        if file_cluster_rows is not None:
            index_db.delete_all_file_clusters(repo_id)
            if file_cluster_rows:
                index_db.upsert_file_clusters(repo_id, file_cluster_rows)
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
            # Mirror file_clusters at the workspace level too so per-
            # workspace refresh can take the partial path.
            try:
                ws_rows = _compute_file_cluster_map(
                    ws_root, paths_glob=paths_glob
                )
            except Exception:
                ws_rows = None
            if ws_rows is not None:
                index_db.delete_all_file_clusters(ws_repo_id)
                if ws_rows:
                    index_db.upsert_file_clusters(ws_repo_id, ws_rows)

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
        # v0.5.2 (Bug 3): JOIN against index.db fields so the user can
        # tell repos apart in /chameleon-list-profiles output. Three
        # dogfood passes flagged "repo_id hash alone isn't enough info
        # to know which is which". The legacy four fields stay verbatim
        # for backward compat; the new fields are additive.
        profiles.append({
            "repo_id": repo_id,
            "trust_state": "trusted" if trust else "untrusted",
            "trusted_at": trust.granted_at if trust else None,
            "trusted_by": trust.granted_by_user if trust else None,
            "repo_root": row.get("repo_root"),
            "archetype_count": row.get("archetype_count"),
            "files_indexed": row.get("files_indexed"),
            "bootstrap_ms": row.get("bootstrap_ms"),
            "last_seen_at": row.get("last_seen_at"),
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

    v0.5.2 (Bug 1): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.

    v0.5.2 (Bug 2 — slug-collision): the auto-generated idiom slug is
    `idiom-YYYY-MM-DD-{epoch_seconds}-{3hex}`. The 4-hex random suffix
    closes the 1-second collision window where two `/chameleon-teach`
    calls landed in the same epoch second (observed twice in dogfood).
    If the proposed slug already exists in idioms.md we retry once with
    a fresh suffix; the second collision is statistically negligible
    (4096^2 chance per second).

    v0.5.2 (Bug 7 — suspicious_input): natural-language prompt-injection
    preambles ("ignore previous instructions", "you are now in DAN
    mode", `eval(…)`, `rm -rf`, etc.) are still STORED — the trust gate
    is the defensive boundary — but the response envelope now carries
    `suspicious_input: True` plus the matched pattern so the using-
    chameleon skill can surface a UI warning.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope({"status": "failed", "error": "no profile in this repo (run /chameleon-init)"})

    # v0.5.2 Bug 7: check the RAW feedback for suspicious patterns before
    # sanitization. Sanitization strips ANSI/zero-width/etc.; the
    # heuristic operates on the natural-language form which survives.
    suspicious, suspicious_pattern = _looks_suspicious(feedback)

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
        # v0.5.2 Bug 2: append a 4-hex random suffix to defeat same-
        # epoch-second collisions. Read idioms.md once to detect a
        # collision; if present, retry once with a fresh suffix. The
        # second collision probability is ~4096^-2 per second so a
        # single retry is more than enough.
        existing_text = (
            idioms_path.read_text(encoding="utf-8")
            if idioms_path.exists()
            else ""
        )

        # BUG-NEW-006 (v0.5.7): derive a human slug from the rationale's
        # first non-empty line. Strips markdown formatting, takes the
        # first ~5 words, kebab-cases them. Pre-fix the slug was always
        # idiom-<date>-<epoch>-<rand>, giving idioms.md entries no
        # visible meaning until the reader opened the body.
        def _slug_from_rationale(text: str) -> str:
            import re as _re

            first_line = ""
            for line in text.splitlines():
                stripped = line.strip()
                # Skip markdown headers, empty lines, code-fence markers.
                if not stripped or stripped.startswith(("#", "```", ">", "*", "-")):
                    continue
                first_line = stripped
                break
            if not first_line:
                return ""
            # Lower-case, replace non-alphanumeric with hyphens, collapse, trim.
            slugged = _re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
            words = slugged.split("-")[:5]
            candidate = "-".join(w for w in words if w)
            # Bound length and reject too-short or non-alpha slugs.
            if len(candidate) < 4 or candidate.isdigit():
                return ""
            return candidate[:40]

        def _new_slug() -> str:
            return (
                f"idiom-{timestamp}-{int(time.time())}-"
                f"{secrets.token_hex(2)}"
            )

        # Prefer rationale-derived slug; fall back to timestamp slug on
        # collision or unsuitable input.
        rationale_slug = _slug_from_rationale(body)
        if rationale_slug and (
            f"### {rationale_slug}\n" not in existing_text
            and f"### {rationale_slug} " not in existing_text
        ):
            slug = rationale_slug
        else:
            slug = _new_slug()
            if f"### {slug}\n" in existing_text or f"### {slug} " in existing_text:
                slug = _new_slug()
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

    response: dict = {
        "status": "success",
        "idioms_added": 1,
        "idioms_deprecated": 0,
    }
    if suspicious:
        response["suspicious_input"] = True
        response["suspicious_input_reason"] = f"matched {suspicious_pattern!r}"
    return _envelope(response)


def _escape_markdown_section_headings(text: str) -> str:
    """Escape `#` / `##` ATX headings at start of line.

    idioms.md uses `## active` / `## deprecated` as section markers; an
    unsanitized `## deprecated` line in a user idiom body would otherwise
    split the active section. CommonMark renders `\\##` as literal text.

    Only levels 1 and 2 are escaped — `###`, `####`, … are valid idiom
    sub-headers and stay untouched.

    BUG-NEW-007 (v0.5.7): don't escape inside fenced code blocks. A
    rationale that includes `# frozen_string_literal: true` inside a
    triple-backtick block must render literally. Pre-fix the escape
    produced `\\# frozen_string_literal: true` visible to the reader,
    cosmetic-broken in the canonical Ruby comment convention.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        # Toggle on/off when we see a fence marker (```), with or without
        # an info string. Treat the line as fence-boundary regardless of
        # leading whitespace.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
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

    v0.5.2 (Bug 1): `repo` now accepts either an absolute repo path or
    a 64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp.optouts import write_session_disable

    if not session_id or not isinstance(session_id, str):
        return _envelope({"status": "failed", "error": "session_id required"})

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })

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

    v0.5.2 (Bug 1): `repo` now accepts either an absolute repo path
    or a 64-char repo_id hex digest. The asymmetry across MCP tools
    surfaced 4 separate dogfood complaints about pause/disable rejecting
    repo_ids. `_resolve_repo_arg` performs the shape detection.
    """
    from chameleon_mcp.optouts import write_pause

    if not isinstance(minutes, int) or minutes <= 0 or minutes > 240:
        return _envelope({"status": "failed", "error": "minutes must be 1..240"})

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })

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

    BUG-004 (v0.5.6): ``repo`` accepts either an absolute repo path or
    a 64-char repo_id hex digest, matching the behavior of get_archetype,
    refresh_repo, propose_archetype_renames, etc. Pre-v0.5.6 the function
    only accepted a path and rejected repo_id with "repo path must be
    absolute" even though every other tool documented repo_id as the
    canonical handle.
    """
    from chameleon_mcp.profile.trust import grant_trust

    # BUG-004: shape-detect via _resolve_repo_arg so a repo_id resolves
    # back to its absolute path via index.db / trust records.
    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    repo_path = resolved_path
    if not repo_path.exists():
        return _envelope({"status": "failed", "error": f"repo path does not exist: {repo!r}"})
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    profile_dir = repo_path / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope({"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"})
    if not (profile_dir / "profile.json").is_file():
        return _envelope({"status": "failed", "error": "no profile.json in .chameleon/ (run /chameleon-init first)"})

    # BUG-NEW-020 (v0.5.7): trust validates that the profile is actually
    # loadable. Pre-fix trust succeeded on corrupted profile.json or on
    # an unsupported schema version, leaving the user with a "trusted"
    # state for something the engine can't read.
    from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir

    try:
        load_profile_dir(profile_dir)
    except (ProfileLoadError, json.JSONDecodeError) as exc:
        return _envelope({
            "status": "failed",
            "error": f"profile is not loadable: {exc}",
        })

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


# v0.5.2 Bug 7 — prompt-injection signal detection.
#
# The patterns below detect natural-language preambles that survive the
# token-level sanitizer (ANSI/zero-width/tag-boundary). Hits never reject
# the idiom: the trust gate is the defensive boundary. Instead we surface
# `suspicious_input: True` in the response envelope so the UI / consumer
# skill can warn the user. Each pattern is documented to make audits easy:
#
#   - "ignore (all )?previous instructions" — canonical jailbreak preamble.
#   - "disregard (the )?(above|prior)" — variant phrasing.
#   - "you are now (in )?\w* mode" — DAN / jailbreak persona triggers.
#   - "system:" / "<system>" / "</system>" — fake system-role prefix.
#   - "eval(", "exec(", "rm -rf" — explicit dangerous code/shell.
#   - "reveal (the )?(secret|api key|prompt|system prompt)" — exfil ask.
#
# Patterns are intentionally permissive: false positives are cheap (the
# user just sees a "are you sure?" UI hint); false negatives waste the
# warning. Each pattern is case-insensitive.
_SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore previous instructions",
     re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE)),
    ("disregard above/prior",
     re.compile(r"disregard\s+(the\s+)?(above|prior)", re.IGNORECASE)),
    ("you are now <mode>",
     re.compile(r"you\s+are\s+now\s+(in\s+)?[\w\s]{0,32}mode", re.IGNORECASE)),
    ("system role injection",
     re.compile(r"(<\s*/?\s*system\s*>|system\s*:\s*)", re.IGNORECASE)),
    ("eval()",
     re.compile(r"\beval\s*\(", re.IGNORECASE)),
    ("exec()",
     re.compile(r"\bexec\s*\(", re.IGNORECASE)),
    ("rm -rf",
     re.compile(r"\brm\s+-rf\b", re.IGNORECASE)),
    ("reveal secrets/prompt",
     re.compile(
         r"reveal\s+(the\s+)?(secret|api\s*key|prompt|system\s+prompt)",
         re.IGNORECASE,
     )),
)


def _looks_suspicious(text: str) -> tuple[bool, str | None]:
    """Return `(matched, label)` if `text` matches a known injection
    pattern, else `(False, None)`.

    The label corresponds to a human-readable handle for the matched
    pattern (e.g., "ignore previous instructions"). It's surfaced in the
    `suspicious_input_reason` envelope field so consumers can route on
    the specific category of suspicion without parsing free text.
    """
    if not isinstance(text, str) or not text:
        return False, None
    for label, regex in _SUSPICIOUS_PATTERNS:
        if regex.search(text):
            return True, label
    return False, None


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
    current_slug = _slugify(current_name) if current_name else None

    def _push(c: str | None) -> None:
        s = _slugify(c) if c else None
        if not s or s in candidates:
            return
        # BUG-006: never propose the current name back at the user, and
        # never propose a derivative that just decorates the current name
        # (e.g. ``cluster-a2cfb565-comments`` when the current name is
        # already ``cluster-a2cfb565``). The "keep current name" affordance
        # lives in the slash-command UI, not in the candidate list.
        if current_slug and (s == current_slug or s.startswith(current_slug + "-")):
            return
        candidates.append(s)

    # 1. Canonical filename stem (e.g., users_controller.rb → users-controller).
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

    v0.5.2 (Bug 1): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp.profile.loader import load_profile_dir

    if not isinstance(top_n, int) or top_n <= 0 or top_n > 64:
        return _envelope({"status": "failed", "error": "top_n must be an int in 1..64"})

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    if not resolved_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
    repo_root = resolved_path.resolve()

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
    rules_data: dict | None = None,
) -> str:
    """Render the user-facing profile.summary.md after a rename.

    The bootstrap orchestrator owns the canonical builder. We can't import
    its private helper without coupling, so we re-emit the same shape here.
    Keep this in sync if the orchestrator's output changes.

    v0.5.4: the Rules section now renders the actual contents of
    ``rules.json`` instead of the v0.4-era placeholder
    ``_Phase 2C: tool config rules + AST stats._``. ``rules_data`` is the
    parsed ``rules.json`` bundle. When omitted (legacy callers), the
    section explains that rules.json wasn't passed.
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
    ]
    # v0.5.1 (Bug 2): keep the secondary-language section in lockstep with
    # the orchestrator's _build_summary_md so a rename doesn't drop the
    # warning from the trust-gate surface.
    hint = profile_data.get("language_hint")
    if isinstance(hint, dict) and hint.get("secondary_detected"):
        lines.extend([
            "## Secondary language",
            "",
            (
                f"This bootstrap scanned **{hint.get('primary', '?')}** only. "
                f"A secondary **{hint['secondary_detected']}** sidecar with "
                f"~{hint.get('secondary_file_count', 0)} files was detected at "
                f"`{hint.get('secondary_path', '')}` and **not included** in "
                "this profile."
            ),
            "",
            f"_{hint.get('note', '')}_",
            "",
        ])
    lines.extend([
        f"## {profile_data.get('archetype_count', 0)} archetypes detected",
        "",
    ])
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
    ])
    # v0.5.4 — mirror the orchestrator's rules rendering. Without
    # ``rules_data`` we fall back to a "no rules captured" note rather
    # than the v0.4 placeholder that read as an unfinished feature.
    rules_block = (rules_data or {}).get("rules") if rules_data else None
    detected_tools = sorted(rules_block.keys()) if isinstance(rules_block, dict) else []
    if detected_tools:
        # Import here to keep this module decoupled from orchestrator at
        # module-import time (preserves the cold-start budget).
        from chameleon_mcp.bootstrap.orchestrator import _count_terminal_rules
        lines.append(
            f"_Auto-derived from {len(detected_tools)} tool config file(s): "
            f"{', '.join(f'`{t}`' for t in detected_tools)}._"
        )
        lines.append("")
        for tool in detected_tools:
            tool_block = rules_block[tool]
            if not isinstance(tool_block, dict):
                continue
            rule_count = _count_terminal_rules(tool_block)
            lines.append(f"- **{tool}** — {rule_count} rule(s) extracted")
    else:
        lines.append(
            "_No tool-config rules detected._ The bootstrap looked for "
            "`eslint`, `tsconfig`, `prettier`, `rubocop`, and `.editorconfig` "
            "and found none of them. Auto-derived rules will appear here "
            "once those configs exist."
        )
    lines.extend([
        "",
        "## Idioms",
        "",
    ])

    # Mirror orchestrator's _extract_active_idioms — extract BOTH active and
    # deprecated sections so the renamed summary doesn't pretend the
    # deprecated section is always empty (v0.5.4: the "## deprecated\n
    # _(none)_" rendering was a placeholder that looked like a bug).
    def _extract_section(text: str, marker: str) -> str:
        if marker not in text:
            return ""
        after = text.split(marker, 1)[1]
        return after.split("\n## ", 1)[0].strip() if "\n## " in after else after.strip()

    active = _extract_section(idioms_text, "## active")
    deprecated = _extract_section(idioms_text, "## deprecated")

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

    if deprecated and deprecated.strip() and "no idioms yet" not in deprecated:
        # Only render the deprecated section if it actually carries content.
        # Skipping the "_(none)_" placeholder removes a confusing visual
        # artifact from clean profiles (cycle-3 dogfood observation).
        lines.append("## Deprecated idioms")
        lines.append("")
        lines.append(
            "_The following idioms were retired by `/chameleon-teach`. They "
            "are kept here for audit history and are NOT injected into "
            "context._"
        )
        lines.append("")
        lines.append(deprecated)
        lines.append("")
    return "\n".join(lines)


# v0.5.1 (Bug 3): the rename overlay file lives at `.chameleon/renames.json`
# and is meant to be committed to the repo so the team shares the mapping.
# Format:
#   { "schema_version": 1,
#     "renames": {<auto_name_from_naming_py>: <user_chosen_name>, ...},
#     "updated_at": "<ISO 8601 Z>" }


def _read_renames_overlay(profile_dir: Path) -> dict[str, str]:
    """Return the current `.chameleon/renames.json` mapping, or {}.

    Tolerant of missing / malformed files — the next `apply_archetype_renames`
    rewrites the file from scratch, so a malformed renames.json self-heals.
    """
    path = profile_dir / "renames.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv > 1:
        return {}
    raw = data.get("renames", {})
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _merge_rename_overlay(
    existing: dict[str, str],
    incoming: dict[str, str],
) -> dict[str, str]:
    """Merge `incoming` user renames into `existing` overlay.

    `existing` is keyed by AUTO-name → user-name. `incoming` is keyed by
    whatever name is currently in `archetypes.json` → new user-name.

    Merge rules:
      1. If incoming.source already appears as a key in existing, the
         incoming.source is an auto-name → overwrite value with the new
         user-name.
      2. If incoming.source appears as a VALUE in existing, the user is
         renaming an already-renamed archetype → walk back to the auto-name
         key and overwrite its value.
      3. Otherwise the incoming.source is itself an auto-name → add a
         brand-new (source, target) entry.

    Returns a new dict; the inputs are not mutated.
    """
    merged = dict(existing)
    value_to_key = {v: k for k, v in existing.items()}
    for source, target in incoming.items():
        if source in merged:
            merged[source] = target
        elif source in value_to_key:
            auto_key = value_to_key[source]
            merged[auto_key] = target
        else:
            merged[source] = target
    return merged


def apply_archetype_renames(repo: str, renames: dict) -> dict:
    """Apply an archetype rename mapping atomically.

    Rewrites:
    - archetypes.json: rename keys under "archetypes"
    - canonicals.json: rename keys under "canonicals"
    - rules.json: rename any keys that exactly equal an old archetype name
    - profile.summary.md: regenerate from the renamed data

    Uses atomic_profile_commit so a crash mid-write leaves the previous
    profile untouched. Returns status, renames_applied, new_profile_sha256.

    v0.5.2 (Bug 1): `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.profile.trust import hash_profile

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope({
            "status": "failed",
            "error": "expected absolute repo path or 64-char repo_id hex digest",
        })
    if not resolved_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
    repo_root = resolved_path.resolve()

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

    # v0.5.1 (Bug 3): merge the new renames into `.chameleon/renames.json`
    # so the next bootstrap re-applies them. Existing renames.json keys
    # are AUTO-derived names (what naming.py would produce on a fresh
    # bootstrap). When the incoming rename's source matches a key, we
    # update the value. When it matches a value (i.e., the user is
    # renaming an already-renamed archetype), we walk back to the
    # original auto-name and update that entry. Otherwise the incoming
    # source is itself an auto-name and gets a brand-new entry.
    existing_renames = _read_renames_overlay(profile_dir)
    merged_renames = _merge_rename_overlay(existing_renames, effective)
    renames_payload = {
        "schema_version": 1,
        "renames": merged_renames,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

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
            (txn_dir / "renames.json").write_text(
                json.dumps(renames_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"atomic commit failed: {e}"})

    # Mirror the new profile hash into index.db so list_profiles + trust
    # material-change detection both see the change.
    repo_id = _compute_repo_id(repo_root)
    new_hash = hash_profile(profile_dir)
    try:
        # v0.5.1 (Bug 1): pin to this repo_root so a monorepo sibling
        # workspace's cached row doesn't leak its archetype_count here.
        cached = index_db.get_repo(repo_id, repo_root_hint=str(repo_root)) or {}
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


def daemon_status() -> dict:
    """Return current status of the chameleon-mcp daemon (Phase 4.5).

    Returns an envelope with:
      alive            — True iff the pidfile points at a live process.
      pid              — recorded PID, or null when not running.
      socket           — UNIX socket path the daemon listens on.
      uptime_s         — seconds since the daemon process started, or null.
      last_request_at  — ISO 8601 timestamp of the most recent socket
                         request (None when the daemon hasn't served any
                         requests yet, or when ping fails). Determined via
                         a lightweight `ping` round-trip; only set when
                         the daemon answers.

    Users invoke this through `/chameleon-status` to see whether the
    fast-path is engaged. The tool is read-only — it does not start or
    stop the daemon as a side effect.
    """
    from chameleon_mcp import daemon as _daemon
    from chameleon_mcp import daemon_client as _daemon_client

    info = _daemon.daemon_info()
    last_request_at = None
    if info.get("alive"):
        # Probe with a 0.5s timeout — non-blocking enough that an idle
        # daemon doesn't slow down /chameleon-status.
        pong = _daemon_client.call("ping", {}, timeout=0.5)
        if isinstance(pong, dict) and "ts" in pong:
            try:
                last_request_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(pong["ts"]))
                )
            except (TypeError, ValueError):
                last_request_at = None

    # BUG-NEW-001 (v0.5.7): surface the running MCP version so
    # /chameleon-status can detect "installed v0.5.7 but session bound
    # to v0.5.6 cache" and tell the user to restart Claude Code.
    try:
        from importlib.metadata import version as _pkg_version

        running_version = _pkg_version("chameleon-mcp")
    except Exception:  # pragma: no cover - defensive
        running_version = None

    return _envelope({
        "alive": bool(info.get("alive")),
        "pid": info.get("pid"),
        "socket": info.get("socket"),
        "uptime_s": info.get("uptime_s"),
        "last_request_at": last_request_at,
        "running_version": running_version,
    })


def _chameleon_version_or_unknown() -> str:
    try:
        from importlib.metadata import version
        return version("chameleon-mcp")
    except Exception:
        return "unknown"


def doctor() -> dict:
    """Triage report for chameleon installation health.

    Returns a structured envelope with subsystem checks. Each check
    has a status (ok | warn | error) and a brief message.
    """
    import os
    import platform
    import shutil
    import sys
    from pathlib import Path

    checks: list[dict] = []

    # 1. Python version
    py = sys.version_info
    if py >= (3, 11):
        checks.append({"name": "python_version", "status": "ok", "detail": f"{py.major}.{py.minor}.{py.micro}"})
    else:
        checks.append({"name": "python_version", "status": "error", "detail": f"{py.major}.{py.minor}.{py.micro} (need >= 3.11)"})

    # 2. bash on PATH (hooks need it)
    bash_path = shutil.which("bash")
    if bash_path:
        checks.append({"name": "bash_on_path", "status": "ok", "detail": bash_path})
    else:
        checks.append({"name": "bash_on_path", "status": "error", "detail": "bash not on PATH; hooks will not run"})

    # 3. timeout(1) on PATH (hooks use it)
    timeout_path = shutil.which("timeout")
    if timeout_path:
        checks.append({"name": "timeout_on_path", "status": "ok", "detail": timeout_path})
    else:
        checks.append({"name": "timeout_on_path", "status": "warn", "detail": "timeout(1) not on PATH; hook may hang Claude on stuck python"})

    # 4. Plugin data dir writable
    try:
        from chameleon_mcp.profile.trust import plugin_data_dir
        data_dir = plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"name": "plugin_data_writable", "status": "ok", "detail": str(data_dir)})
    except Exception as exc:
        checks.append({"name": "plugin_data_writable", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})

    # 5. Hook scripts exist + executable
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CURSOR_PLUGIN_ROOT")
    if plugin_root_env:
        plugin_root = Path(plugin_root_env)
        hook_dir = plugin_root / "hooks"
        for hook_name in ("preflight-and-advise", "posttool-recorder", "session-start", "callout-detector"):
            hpath = hook_dir / hook_name
            if hpath.is_file() and os.access(hpath, os.X_OK):
                checks.append({"name": f"hook_{hook_name}", "status": "ok", "detail": "executable"})
            elif hpath.is_file():
                checks.append({"name": f"hook_{hook_name}", "status": "error", "detail": "exists but not executable"})
            else:
                checks.append({"name": f"hook_{hook_name}", "status": "error", "detail": "missing"})
    else:
        checks.append({"name": "hooks", "status": "warn", "detail": "CLAUDE_PLUGIN_ROOT not set; cannot locate hook scripts"})

    # 6. HMAC key sane (no exception means file present and ownership ok)
    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key
        _ensure_hmac_key()
        checks.append({"name": "hmac_key", "status": "ok", "detail": "exists and owner-readable"})
    except Exception as exc:
        checks.append({"name": "hmac_key", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"})

    # 7. Daemon liveness (re-uses existing daemon_status)
    try:
        ds = daemon_status()
        if ds.get("data", {}).get("alive"):
            checks.append({"name": "daemon", "status": "ok", "detail": f"alive (pid={ds['data'].get('pid')})"})
        else:
            checks.append({"name": "daemon", "status": "warn", "detail": "not running (will spawn on next hook)"})
    except Exception as exc:
        checks.append({"name": "daemon", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"})

    # 8. Recent hook errors (last 5 from .hook_errors.log)
    log = Path.home() / ".local" / "share" / "chameleon" / ".hook_errors.log"
    if log.is_file():
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
            checks.append({"name": "recent_hook_errors", "status": "warn" if tail else "ok", "detail": tail})
        except Exception as exc:
            checks.append({"name": "recent_hook_errors", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"})
    else:
        checks.append({"name": "recent_hook_errors", "status": "ok", "detail": "no errors logged"})

    # 9. Per-known-repo profile_status + trust_state (from list_profiles)
    try:
        lp = list_profiles(limit=20)
        profiles = lp.get("data", {}).get("profiles", [])
        repo_states = [
            {
                "repo_root": r.get("repo_root"),
                "profile_status": r.get("profile_status"),
                "trust_state": r.get("trust_state"),
            }
            for r in profiles
        ]
        checks.append({"name": "known_repos", "status": "ok", "detail": repo_states})
    except Exception as exc:
        checks.append({"name": "known_repos", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"})

    # Roll up
    error_count = sum(1 for c in checks if c["status"] == "error")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    if error_count:
        overall = "error"
    elif warn_count:
        overall = "warn"
    else:
        overall = "ok"

    return _envelope({
        "overall": overall,
        "platform": {"system": platform.system(), "release": platform.release()},
        "chameleon_version": _chameleon_version_or_unknown(),
        "checks": checks,
        "summary": {
            "total": len(checks),
            "ok": len(checks) - error_count - warn_count,
            "warn": warn_count,
            "error": error_count,
        },
    })
