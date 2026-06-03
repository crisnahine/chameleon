"""MCP tool implementations for chameleon.

All 20 tools are fully implemented. Each tool returns the standard API
versioning envelope:
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.profile.loader import LoadedProfile


_REPO_ID_CACHE: dict[str, tuple[float, str]] = {}
_REPO_ID_CACHE_TTL = 300


def _clear_repo_id_cache() -> None:
    """Invalidate the repo-id cache (called by bootstrap_repo / refresh_repo)."""
    _REPO_ID_CACHE.clear()


def _notify_daemon_cache_invalidation() -> None:
    """Tell the long-lived daemon to drop its process-local profile cache.

    BUG-029: profile-mutating operations (bootstrap, refresh, teach) run
    in the MCP server process, not the daemon. Without this notification
    the daemon keeps serving stale cached data until the mtime token
    happens to change enough to trigger a reload.

    Fail-open: if the daemon is unreachable the notification is a no-op;
    the next hook call will either hit the in-process fallback or the
    daemon's mtime check will eventually detect the change.
    """
    try:
        from chameleon_mcp import daemon_client

        daemon_client.call("invalidate_cache", {})
    except Exception:
        pass


def _envelope(data: dict, truncated: bool = False, next_cursor: str | None = None) -> dict:
    """Standard response envelope for all tools."""
    out: dict = {"api_version": "1", "data": data}
    if truncated:
        out["truncated"] = True
    if next_cursor is not None:
        out["next_cursor"] = next_cursor
    return out


# Witness excerpt read ceiling. Real canonical witnesses are a few KB; this
# 5 MB ceiling (matching the profile-artifact cap) lets even an unusually large
# hand-written exemplar inject in FULL — quality over token cost — while still
# bounding a pathological/generated witness from flooding context or memory.
# Over the ceiling the excerpt is FLAGGED (truncated/oversize) rather than
# silently returning nothing, so the model still learns a witness exists.
WITNESS_MAX_BYTES = 5 * 1024 * 1024

_REPO_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_WS_PRUNE_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        ".next",
        "out",
        "vendor",
        "tmp",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        "coverage",
    }
)
_WS_MAX_DEPTH = 4


def _iter_workspace_chameleon_dirs(root: Path):
    """Yield child ``.chameleon`` dirs under ``root`` (excluding the root's own).

    Bounded-depth, heavy-dir-pruned walk so a workspace lookup on a large
    monorepo doesn't rglob through node_modules / .git / build output.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if child.name == ".chameleon":
                if child != root / ".chameleon":
                    yield child
                continue
            if child.name in _WS_PRUNE_DIRS or child.name.startswith("."):
                continue
            if depth + 1 <= _WS_MAX_DEPTH:
                stack.append((child, depth + 1))


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

    looks_pathy = repo[0] in ("/", "~") or repo.startswith("./") or repo.startswith("../")
    if not looks_pathy:
        if _REPO_ID_RE.match(repo):
            resolved = _resolve_repo_root_by_id(repo)
            return (resolved, repo)
        try:
            candidate = Path(repo).expanduser()
        except (OSError, ValueError):
            return None, None
        if not candidate.is_absolute():
            return None, None
        repo_path_str: str = repo
    else:
        repo_path_str = repo

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
    return path, None


_MAX_PATH_LEN = 4096
# NAME_MAX: the kernel raises ENAMETOOLONG on a single path component over this
# many bytes, independent of the total length. macOS and Linux both use 255.
_NAME_MAX_BYTES = 255


def _validate_file_path_arg(file_path: object) -> bool:
    """Return True if `file_path` is a safe-to-process string.

    Tools that take a `file_path` argument should call this first and
    return their documented no_repo / failed envelope on False. Catches:
    - non-str (None, int, dict, etc.)
    - empty string
    - null-byte (lstat raises ValueError mid-resolution)
    - over-length total path (kernel ENAMETOOLONG before resolution completes)
    - over-length single component (>255 bytes): ENAMETOOLONG fires per
      component too, so a 300-byte filename in a short dir passes the total
      check then raises errno 63 in the FS walk (is_file/lstat/resolve). Reject
      it up front so the downstream guards never see the uncaught OSError.
    """
    if not isinstance(file_path, str):
        return False
    if not file_path:
        return False
    if "\x00" in file_path:
        return False
    if len(file_path) > _MAX_PATH_LEN:
        return False
    for component in file_path.split("/"):
        if len(component.encode("utf-8", "surrogatepass")) > _NAME_MAX_BYTES:
            return False
    return True


_CASE_INSENSITIVE_HOSTS: frozenset[str] = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com"}
)

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

    m = _SSH_URL_RE.match(s)
    if m and "://" not in s:
        host, path = m.group(1), m.group(2)
        s = f"ssh://git@{host}/{path}"

    s = re.sub(r"\.git/?$", "", s)
    s = s.rstrip("/")

    proto_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*)://([^/]+)(/.*)?$", s)
    if not proto_match:
        return s
    scheme, host, path = proto_match.group(1), proto_match.group(2), proto_match.group(3) or ""

    if "@" in host:
        host = host.split("@", 1)[1]

    host_l = host.lower()
    if host_l in _CASE_INSENSITIVE_HOSTS:
        host = host_l
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


def _effective_profile_dir(repo_root: Path) -> Path:
    """Return the profile dir READS should use for this repo.

    Branch pinning: when ``.chameleon/config.json`` sets ``canonical_ref`` (e.g.
    ``"origin/main"``), reads come from a canonical-ref cache instead
    of the working tree — so a developer on a feature branch sees the
    team's main-branch conventions regardless of what their local
    checkout has. Writes (bootstrap_repo, refresh_repo, apply_renames,
    teach_profile_*, grant_trust) always target the working tree;
    only reads (get_pattern_context, get_archetype, get_rules,
    get_canonical_excerpt, lint_file, etc.) follow the pin.

    Falls back to the working tree when:
      - the repo isn't chameleon-aware (no .chameleon/config.json)
      - config.json is malformed
      - canonical_ref isn't set
      - the ref can't be resolved or the materialize fails
      - any unexpected error
    """
    working = repo_root / ".chameleon"
    config_file = working / "config.json"
    if not config_file.is_file():
        return working
    try:
        from chameleon_mcp.profile.canonical_loader import materialize_canonical
        from chameleon_mcp.profile.config import (
            ChameleonConfigError,
            load_config,
        )

        try:
            cfg = load_config(working)
        except ChameleonConfigError as cfg_exc:
            _log_effective_profile_dir_fallback(
                repo_root,
                "config_invalid",
                f"{type(cfg_exc).__name__}: {cfg_exc}",
            )
            return working
        if not cfg.branch_pinning_enabled:
            return working
        repo_id = _compute_repo_id(repo_root)
        canonical = materialize_canonical(repo_root, repo_id, cfg.canonical_ref)
        if canonical is None:
            _log_effective_profile_dir_fallback(
                repo_root,
                "canonical_unresolvable",
                f"canonical_ref={cfg.canonical_ref!r} could not be materialized "
                "(unresolvable ref / missing .chameleon at ref / scan-rejected); "
                "falling back to working tree",
            )
            return working
        return canonical
    except Exception as exc:  # noqa: BLE001
        _log_effective_profile_dir_fallback(
            repo_root,
            "unexpected_error",
            f"{type(exc).__name__}: {exc}",
        )
        return working


def _log_effective_profile_dir_fallback(repo_root: Path, reason: str, detail: str) -> None:
    """Write a single-line note to stderr when branch pinning falls back.

    Best-effort: any failure here is silently ignored (logging must
    not break the hot path). The bash hook wrappers redirect stderr
    to ``~/.local/share/chameleon/.hook_errors.log`` so doctor's
    ``recent_hook_errors`` check surfaces these to the user.
    """
    try:
        import sys as _sys
        import time as _time

        ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        print(
            f"[{ts}] chameleon: branch-pinning fallback "
            f"(repo={str(repo_root)!r}, reason={reason!r}): {detail!r}",
            file=_sys.stderr,
        )
    except Exception:  # noqa: BLE001
        pass


def _compute_repo_id(repo_root: Path) -> str:
    """Canonical repo_id.

    Schema v6+: prefer git remote URL (stable across moved checkouts);
    fall back to the resolved absolute path when no git remote exists.

    Two checkouts of the same repository — even on different machines or
    after moving the working tree — get the same id, so the per-user trust
    grant and drift observations follow the project rather than the
    filesystem location. Repos without `origin` (fresh `git init`, vendored
    snapshots, archive extracts) keep the early path-based behavior.
    """
    key = str(repo_root.resolve())
    cached = _REPO_ID_CACHE.get(key)
    if cached is not None:
        cached_at, repo_id = cached
        if (time.monotonic() - cached_at) < _REPO_ID_CACHE_TTL:
            return repo_id

    url = _git_remote_url(repo_root)
    if url:
        canonical = _normalize_git_url(url)
        if canonical:
            repo_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            _REPO_ID_CACHE[key] = (time.monotonic(), repo_id)
            return repo_id
    repo_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
    _REPO_ID_CACHE[key] = (time.monotonic(), repo_id)
    return repo_id


def _legacy_path_repo_id(repo_root: Path) -> str:
    """The pre-v6 path-derived repo_id.

    Used by `detect_repo` to look up trust grants made by early engines.
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

    1. **Earlier path-id migration** (string hint + ``legacy_repo_id``):
       fires when ``trust_state == "untrusted"`` because the canonical
       (git-remote-derived) id has no record, but the legacy path-derived
       id DOES. The user trusted the repo before the engine changed the repo_id
       derivation and just needs to re-grant under the new id.

    2. **Stale-clone hint** (dict, Bug H2): fires when
       ``trust_state == "stale"`` AND the trust record's recorded
       ``repo_root`` doesn't match the current ``repo_root``. Same git
       remote + same id, but the trust was granted on a different
       checkout (a prior calibration run, a teammate's clone synced via
       shared plugin-data, etc.). An earlier engine surfaced this as a generic
       "stale" with no explanation; the engine now returns a structured envelope
       so the using-chameleon skill can tell the user "you're on a fresh
       clone — re-run /chameleon-trust" instead of "something changed
       inside the profile". Genuine in-place stale (recorded_repo_root
       matches current_repo_root) deliberately does NOT surface the hint
       — that branch is already covered by the standard stale messaging.
    """
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import is_material_change, trust_state_for

    if not _validate_file_path_arg(file_path):
        return _envelope(
            {
                "repo_id": None,
                "repo_root": None,
                "profile_status": "no_repo",
                "trust_state": "n/a",
            }
        )

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(
            {
                "repo_id": None,
                "repo_root": None,
                "profile_status": "no_repo",
                "trust_state": "n/a",
            }
        )

    try:
        home = Path.home().resolve()
        resolved = Path(repo_root).resolve()
    except OSError:
        home = None  # type: ignore[assignment]
        resolved = repo_root
    if home is not None and (
        resolved == home or resolved in home.parents or resolved == Path(resolved.anchor)
    ):
        return _envelope(
            {
                "repo_id": None,
                "repo_root": None,
                "profile_status": "no_repo",
                "trust_state": "n/a",
            }
        )

    repo_id = _compute_repo_id(repo_root)
    profile_dir = repo_root / ".chameleon"
    profile_file = profile_dir / "profile.json"
    profile_present = profile_file.exists()
    trust = trust_state_for(repo_id)

    profile_corrupted = False
    profile_unsupported_schema = False
    if profile_present:
        try:
            import json as _json

            with profile_file.open("r", encoding="utf-8") as fh:
                _peek = _json.load(fh)
            from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

            _sv = _peek.get("schema_version") if isinstance(_peek, dict) else None
            if isinstance(_sv, int) and _sv > MAX_SUPPORTED_SCHEMA_VERSION:
                profile_unsupported_schema = True
        except (OSError, ValueError):
            profile_corrupted = True

    if not profile_present or profile_corrupted or profile_unsupported_schema:
        trust_state = "n/a"
    elif trust is None or not trust.grants_root(profile_dir.parent):
        # No record, or a record that covers a different root under the same
        # (monorepo-shared) repo_id -- this workspace profile was never
        # granted, so it is untrusted, not stale.
        trust_state = "untrusted"
    elif is_material_change(repo_id, profile_dir):
        trust_state = "stale"
    else:
        trust_state = "trusted"

    legacy_id = _legacy_path_repo_id(repo_root)
    legacy_trust_hint_value: str | dict | None = None
    legacy_repo_id_value: str | None = None
    if trust is None and legacy_id != repo_id and trust_state_for(legacy_id) is not None:
        legacy_trust_hint_value = (
            "Trust record found at the legacy path-derived repo_id "
            f"{legacy_id[:8]}…; the canonical repo_id is now derived from the "
            "git remote URL. Run /chameleon-trust to re-grant under the new id."
        )
        legacy_repo_id_value = legacy_id

    current_repo_root_str = str(repo_root)
    if (
        trust is not None
        and trust_state == "stale"
        and trust.repo_root
        and trust.repo_root != current_repo_root_str
    ):
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


def _prefix_overlap_fallback(rel_str: str, archetypes: dict) -> tuple[str | None, list[str]]:
    """BUG-015: pick the archetype that shares the longest directory prefix.

    Returns (primary, alternatives). When no archetype shares at least one
    leading directory segment with the file, returns (None, []).
    """
    file_dir = rel_str.rsplit("/", 1)[0] if "/" in rel_str else ""
    file_segments = [s for s in file_dir.split("/") if s]
    file_ext = rel_str.rsplit(".", 1)[-1] if "." in rel_str.rsplit("/", 1)[-1] else ""
    scored: list[tuple[int, int, str]] = []
    for name, arch in archetypes.items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        if ":" in pattern:
            arch_dir, _, arch_ext = pattern.rpartition(":")
        else:
            arch_dir, arch_ext = pattern, ""
        arch_segments = [s for s in arch_dir.split("/") if s]
        if not arch_segments or not file_segments:
            continue
        overlap = 0
        for fs, asg in zip(file_segments, arch_segments):  # noqa: B905
            if fs == asg:
                overlap += 1
            else:
                break
        if overlap == 0:
            continue
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


def _nearest_canonical_entry(rel_str: str, entries: list) -> dict:
    """Pick the canonical entry whose witness shares the most leading directory
    segments with the edited file.

    A dense archetype can carry several merged sub-buckets (e.g. services across
    amazon_s3/, hubspot/, llm/), each with its own canonical witness. Resolving
    by nearest path means a hubspot/ edit is shown a hubspot/ witness instead of
    always the first (e.g. amazon_s3/) one. Falls back to entries[0].
    """
    if not entries:
        return {}
    q_parts = rel_str.split("/")[:-1]
    best = entries[0] or {}
    best_overlap = -1
    for e in entries:
        w_parts = (((e or {}).get("witness") or {}).get("path") or "").split("/")[:-1]
        overlap = 0
        for a, b in zip(q_parts, w_parts):  # noqa: B905
            if a == b:
                overlap += 1
            else:
                break
        if overlap > best_overlap:
            best_overlap = overlap
            best = e or {}
    return best


def _witness_path_overlap(rel_str: str, canonicals: dict, archetype_name: str) -> int:
    """Count leading directory segments shared between `rel_str` (the
    query file's repo-relative path) and the archetype's canonical
    witness path. Used as a tiebreak after AST scoring when multiple
    archetypes share a paths_pattern. Excludes the filename segment so
    files in the same directory get the same overlap regardless of name.
    """
    entries = canonicals.get(archetype_name) or []
    if not entries:
        return 0
    witness_path = ((entries[0] or {}).get("witness") or {}).get("path") or ""
    if not witness_path:
        return 0
    q_parts = rel_str.split("/")[:-1]
    w_parts = witness_path.split("/")[:-1]
    common = 0
    for a, b in zip(q_parts, w_parts):  # noqa: B905
        if a == b:
            common += 1
        else:
            break
    return common


def _content_signal_for_path(p: Path) -> str:
    """Read up to 200 bytes of `p` and classify the content signal.

    Extracted from get_archetype (Bug 3 logic) so the public
    get_archetype and get_pattern_context's inlined archetype resolution
    share one implementation. Returns one of
    {"none","use_client","use_server","shebang","ts_pragma"}; never None.
    """
    from chameleon_mcp.signatures import content_signal_match_for

    file_head: str | None = None
    try:
        if p.is_file():
            file_head = p.read_bytes()[:200].decode("utf-8", errors="replace")
    except OSError:
        # includes ENAMETOOLONG (errno 63) on an over-NAME_MAX component, which
        # is_file() raises before read_bytes() is even reached.
        file_head = None
    value = content_signal_match_for(file_head) if file_head is not None else "none"
    return value if value is not None else "none"


def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Tiebreaks among multiple path-bucket matches by AST shape.
    When the file exists on disk we extract its dimensions via the lint
    engine's pure-function `extract_dimensions` and score each path-bucket
    candidate by how many `ast_query` dimensions align. Higher score wins;
    ties fall back to the cluster-size ordering.

    The confidence band reflects how strong the AST signal was:
      "high"   — score >= 4 of 5 ast_query dimensions agreed
      "medium" — at least one dimension agreed
      "low"    — no AST signal (file missing on disk, no ast_query, or
                 substring-only fallback match)

    Backwards compat: files without on-disk content (deleted, just-detected
    from a hook input that doesn't carry content) fall back to the
    path-bucket-only behavior so the function stays callable on hypothetical
    paths.

    Bug 3: the response envelope's ``content_signal_match`` field
    is now populated whenever the file is readable on disk, by reading
    the first 200 bytes ourselves and calling
    ``signatures.content_signal_match_for``. Earlier versions hardcoded
    ``None`` in every return branch, which made the Phase 2C content
    signal dead code despite being computed inside the lint engine. The
    new wire-through emits a string ("none", "use_client", "use_server",
    "shebang", "ts_pragma") whenever the file head was read, and Python
    ``None`` only when we never looked (file missing, unreadable).

    Extension-blind compatibility: this function still computes the
    file's bucket with the extension-blind
    ``path_pattern_bucket_for`` (``include_extension=False``) so older
    ``archetypes.json`` files continue to match. Newer bootstraps
    write extension-aware buckets (e.g. ``"src/components:tsx"``); we
    also check the extension-aware variant as a secondary key so
    profiles written by older bootstraps still hit the exact-match path.
    """
    from chameleon_mcp.profile.loader import find_repo_root, load_profile_dir

    if not _validate_file_path_arg(file_path):
        return _envelope(
            {
                "archetype": None,
                "alternatives": [],
                "content_signal_match": "none",
                "confidence_band": "low",
                "match_quality": "none",
            }
        )

    p = Path(file_path).expanduser()

    content_signal_value: str = _content_signal_for_path(p)

    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(
            {
                "archetype": None,
                "alternatives": [],
                "content_signal_match": content_signal_value,
                "confidence_band": "low",
                "match_quality": "none",
            }
        )

    expected_repo_id = _compute_repo_id(repo_root)
    if _REPO_ID_RE.match(repo) if isinstance(repo, str) else False:
        if expected_repo_id != repo:
            return _envelope(
                {
                    "archetype": None,
                    "alternatives": [],
                    "content_signal_match": content_signal_value,
                    "confidence_band": "low",
                    "match_quality": "none",
                }
            )
    else:
        _resolved_path, resolved_repo_id = _resolve_repo_arg(repo)
        if resolved_repo_id is None or resolved_repo_id != expected_repo_id:
            return _envelope(
                {
                    "archetype": None,
                    "alternatives": [],
                    "content_signal_match": content_signal_value,
                    "confidence_band": "low",
                    "match_quality": "none",
                }
            )

    profile_dir = _effective_profile_dir(repo_root)
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        return _envelope(
            {
                "archetype": None,
                "alternatives": [],
                "content_signal_match": content_signal_value,
                "confidence_band": "low",
                "match_quality": "none",
            }
        )

    return _get_archetype_with_loaded(p, repo_root, loaded, content_signal_value)


def _get_archetype_with_loaded(
    p: Path,
    repo_root: Path,
    loaded: LoadedProfile,
    content_signal_value: str,
) -> dict:
    """Archetype scoring tail shared by get_archetype and
    get_pattern_context. Assumes repo_root + a successfully loaded
    profile; does no find_repo_root / load_profile_dir of its own.
    """
    from chameleon_mcp.lint_engine import (
        canonical_confidence,
        detect_language,
        extract_dimensions,
    )
    from chameleon_mcp.signatures import path_pattern_bucket_for

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
    file_bucket, _sub = path_pattern_bucket_for(rel_str)
    file_bucket_ext, _sub_ext = path_pattern_bucket_for(rel_str, include_extension=True)

    exact_matches: list[str] = []
    fallback_matches: list[str] = []

    archetypes = loaded.archetypes.get("archetypes", {})
    for name, arch in archetypes.items():
        pattern = arch.get("paths_pattern", "")
        if not pattern:
            continue
        if pattern == file_bucket or pattern == file_bucket_ext:
            exact_matches.append(name)
        elif pattern in rel_str:
            fallback_matches.append(name)

    if not exact_matches:
        if fallback_matches:
            primary = fallback_matches[0]
            alternatives = fallback_matches[1:]
            confidence = "low"
        else:
            primary, alternatives = _prefix_overlap_fallback(rel_str, archetypes)
            confidence = "low"
        return _envelope(
            {
                "archetype": primary,
                "alternatives": alternatives,
                "content_signal_match": content_signal_value,
                "confidence_band": confidence,
                "match_quality": "fallback" if primary is not None else "none",
            }
        )

    canonicals_for_locality = loaded.canonicals.get("canonicals", {}) or {}
    exact_matches.sort(
        key=lambda n: (
            -_witness_path_overlap(rel_str, canonicals_for_locality, n),
            -archetypes.get(n, {}).get("cluster_size", 0),
        )
    )

    content: str | None = None
    if p.is_file():
        try:
            raw = p.read_bytes()[:100_000]
            content = raw.decode("utf-8", errors="replace")
        except OSError:
            content = None

    if content is None:
        return _envelope(
            {
                "archetype": exact_matches[0],
                "alternatives": exact_matches[1:],
                "content_signal_match": content_signal_value,
                "confidence_band": "high" if len(exact_matches) == 1 else "low",
                "match_quality": "exact",
            }
        )

    language = detect_language(str(p)) or loaded.profile.get("language")
    if language not in ("typescript", "ruby"):
        language = None
    snapshot = extract_dimensions(content, language=language)

    canonicals = loaded.canonicals.get("canonicals", {}) or {}
    scored: list[tuple[str, float, int]] = []
    for name in exact_matches:
        entries = canonicals.get(name) or []
        ast_query: dict | None = None
        if entries:
            first = entries[0] or {}
            ast_query = (first.get("normative_shape") or {}).get("ast_query")
        if not ast_query:
            scored.append((name, -1.0, 0))
            continue
        constrained = sum(
            1
            for k in (
                "default_export_kind",
                "top_level_node_kinds",
                "named_export_count_bucket",
                "jsx_present",
                "content_signal",
            )
            if ast_query.get(k) not in (None, [], "")
        )
        ratio = canonical_confidence(snapshot, ast_query)
        absolute_matches = ratio * constrained
        scored.append((name, absolute_matches, constrained))

    if any(s > -1.0 for _, s, _ in scored):
        scored.sort(
            key=lambda item: (
                -item[1],
                -_witness_path_overlap(rel_str, canonicals, item[0]),
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
        match_quality = "ast"
    else:
        primary = exact_matches[0]
        alternatives = exact_matches[1:]
        confidence = "high" if len(exact_matches) == 1 else "low"
        match_quality = "exact"

    final_signal = (
        content_signal_value
        if content_signal_value is not None
        else (snapshot.content_signal if snapshot.content_signal is not None else "none")
    )
    return _envelope(
        {
            "archetype": primary,
            "alternatives": alternatives,
            "content_signal_match": final_signal,
            "confidence_band": confidence,
            "match_quality": match_quality,
        }
    )


def _empty_pattern_envelope(
    repo_id: str | None,
    profile_status: str,
    trust_state: str,
) -> dict:
    """Shape of the get_pattern_context response when no archetype data exists.

    BUG-022: both the no-repo / no-profile / profile-corrupted early returns
    must use the same archetype envelope shape as the healthy path. Earlier
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
            "match_quality": "none",
            "sub_buckets_count": 0,
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

    if not _validate_file_path_arg(file_path):
        return _envelope(_empty_pattern_envelope(None, "no_repo", "n/a"))

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(_empty_pattern_envelope(None, "no_repo", "n/a"))

    repo_id = _compute_repo_id(repo_root)
    profile_dir = _effective_profile_dir(repo_root)
    profile_file = profile_dir / "profile.json"
    if not profile_file.exists():
        return _envelope(_empty_pattern_envelope(repo_id, "no_profile", "n/a"))

    from chameleon_mcp.profile.trust import is_material_change

    trust = trust_state_for(repo_id)
    trust_check_dir = repo_root / ".chameleon"
    if trust is None or not trust.grants_root(trust_check_dir.parent):
        trust_state_str = "untrusted"
    elif is_material_change(repo_id, trust_check_dir):
        trust_state_str = "stale"
    else:
        trust_state_str = "trusted"

    try:
        loaded = load_profile_dir(profile_dir)
    except Exception:
        return _envelope(_empty_pattern_envelope(repo_id, "profile_corrupted", "n/a"))

    content_signal_value = _content_signal_for_path(p)
    arch_response = _get_archetype_with_loaded(p, repo_root, loaded, content_signal_value)
    arch_data = arch_response["data"]

    if arch_data.get("archetype"):
        arch_entry = loaded.archetypes.get("archetypes", {}).get(arch_data["archetype"], {}) or {}
        sub_buckets = arch_entry.get("sub_buckets") or {}
        arch_data["sub_buckets_count"] = len(sub_buckets) if isinstance(sub_buckets, dict) else 0
        summary = arch_entry.get("summary") or ""
        if summary:
            arch_data["summary"] = summary
    else:
        arch_data["sub_buckets_count"] = 0

    canonical_data = {"content": "", "witness_path": None, "truncated": False, "sha_hint": None}
    if arch_data["archetype"]:
        canonicals = loaded.canonicals.get("canonicals", {}).get(arch_data["archetype"], [])
        if canonicals:
            try:
                rel_str = p.resolve().relative_to(repo_root.resolve()).as_posix()
            except (ValueError, OSError):
                rel_str = p.name
            first = _nearest_canonical_entry(rel_str, canonicals)
            witness_rel = first.get("witness", {}).get("path")
            if witness_rel and trust_state_str == "untrusted":
                # Untrusted: the witness content is redacted below anyway, so
                # skip the file read + sanitize + cache on the hot path. Keep
                # the metadata so a caller knows a witness exists.
                canonical_data = {
                    "content": "",
                    "witness_path": witness_rel,
                    "truncated": False,
                    "sha_hint": first.get("witness", {}).get("sha_hint"),
                }
            elif witness_rel:
                try:
                    import os as _os

                    from chameleon_mcp import _excerpt_cache
                    from chameleon_mcp.safe_open import (
                        FileTooLargeError,
                        UnsafeFileError,
                        safe_open_fd,
                    )
                    from chameleon_mcp.sanitization import (
                        sanitize_for_chameleon_context,
                    )

                    fd, st, safe_path = safe_open_fd(
                        repo_root, witness_rel, max_size_bytes=WITNESS_MAX_BYTES
                    )
                    key = (
                        str(safe_path),
                        st.st_dev,
                        st.st_ino,
                        st.st_size,
                        st.st_mtime_ns,
                        st.st_ctime_ns,
                        _excerpt_cache.CONTEXT_TRANSFORM_VERSION,
                    )

                    def _build() -> tuple[str, bool]:
                        chunks = []
                        try:
                            while True:
                                buf = _os.read(fd, 65_536)
                                if not buf:
                                    break
                                chunks.append(buf)
                        except OSError as e:
                            raise OSError(f"read failed: {e}") from e
                        raw_bytes = b"".join(chunks)
                        raw = raw_bytes.decode("utf-8", errors="replace")
                        try:
                            st2 = _os.fstat(fd)
                        except OSError as e:
                            raise OSError(f"fstat after read failed: {e}") from e
                        if (
                            st2.st_size != st.st_size
                            or st2.st_mtime_ns != st.st_mtime_ns
                            or st2.st_ctime_ns != st.st_ctime_ns
                        ):
                            raise OSError("witness changed mid-read; failing open")
                        return sanitize_for_chameleon_context(raw), False

                    try:
                        content, truncated = _excerpt_cache.get_or_build(key, _build)
                    finally:
                        try:
                            _os.close(fd)
                        except OSError:
                            pass

                    canonical_data = {
                        "content": content,
                        "witness_path": witness_rel,
                        "truncated": truncated,
                        "sha_hint": first.get("witness", {}).get("sha_hint"),
                    }
                except FileTooLargeError:
                    # A witness over the 5 MB ceiling (a pathological/generated file
                    # mis-selected as canonical) must not silently vanish: flag it
                    # (a direct get_canonical_excerpt caller then knows a witness
                    # exists and can read it by path).
                    canonical_data = {
                        "content": "",
                        "witness_path": witness_rel,
                        "truncated": True,
                        "oversize": True,
                        "sha_hint": first.get("witness", {}).get("sha_hint"),
                    }
                except (UnsafeFileError, FileNotFoundError, OSError):
                    # Security rejection (traversal/symlink) or read error: leave empty.
                    pass

    idioms_text = loaded.idioms_text or ""
    if idioms_text:
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        idioms_text = sanitize_for_chameleon_context(idioms_text)

    rules_out = list(loaded.rules.get("rules", {}).items())

    # Trust gate at the data layer. An untrusted .chameleon profile is
    # attacker-controllable, and this tool is model-callable, so ALL
    # profile-derived content must be withheld on a direct call — not only via
    # the hook presentation layer (preflight_and_advise), which is one of
    # several callers. That means the witness content, team idioms, the
    # archetype summary (free prose from archetypes.json), and the rules map.
    # Stale (trusted-then-changed) still flows, matching the documented
    # contract: the hook injects stale content with a warning. Metadata
    # (archetype name, witness_path, sha_hint, trust_state) is preserved so a
    # caller can tell "gated for trust" from "no witness found".
    if trust_state_str == "untrusted":
        canonical_data = {**canonical_data, "content": "", "redacted_reason": "untrusted"}
        idioms_text = ""
        arch_data.pop("summary", None)
        rules_out = []

    return _envelope(
        {
            "repo": {
                "id": repo_id,
                "profile_status": "profile_present",
                "trust_state": trust_state_str,
            },
            "archetype": arch_data,
            "canonical_excerpt": canonical_data,
            "rules": rules_out,
            "idioms": idioms_text,
            "meta": {
                "mtime_token": loaded.mtime_token,
                "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }
    )


def _resolve_repo_root_by_id(repo_id: str, repo_root_hint: str | None = None) -> Path | None:
    """Map a repo_id back to its repo_root.

    Phase 4.4 lookup order:
      1. index.db (primary; populated by bootstrap_repo on success)
      2. trust record's repo_root (backward compat with early installs
         that bootstrapped before index.db existed)

    Bug 1: monorepo sub-workspaces share a git-remote-derived
    repo_id with the root, so a single repo_id may now resolve to
    multiple candidate roots. When the caller knows which workspace it
    is asking about (e.g., refresh_repo just resolved the absolute path),
    it passes `repo_root_hint` so index.db returns the matching row
    instead of the freshest-overall one.

    Returns None if neither layer resolves to an existing directory.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import trust_state_for

    indexed = index_db.resolve_repo_root(repo_id, repo_root_hint=repo_root_hint)
    if indexed:
        p = Path(indexed)
        if p.is_dir():
            return p.resolve()

    record = trust_state_for(repo_id)
    if record is None or not record.repo_root:
        return None
    p = Path(record.repo_root)
    return p.resolve() if p.is_dir() else None


def get_canonical_excerpt(repo: str, archetype: str) -> dict:
    """Return the annotated canonical excerpt for an archetype.

    Bug 5: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. Earlier the function only accepted
    repo_ids and silently returned `{content: "", witness_path: null,
    truncated: false}` when handed a path. Now we shape-detect via
    `_resolve_repo_arg` and emit an explicit `{status: failed, error:
    "repo_id not found"}` envelope for unresolvable input so callers
    can distinguish "no archetype" from "wrong arg shape".

    Bug A: the "valid repo, valid archetype name, but the
    archetype has no canonical witness in canonicals.json" path was
    equally silent — the witness can be rejected at bootstrap time
    because every candidate contained secrets / was too long / the
    cluster fell below the confidence threshold. Callers (the
    using-chameleon skill, IDE integrations) couldn't distinguish that
    from a transient I/O failure. We now emit three typed envelopes:
      - `status: "failed", error: "repo_id not found"` — unresolvable
        `repo` argument (unchanged).
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
        return _envelope(
            {
                "status": "failed",
                "error": "repo_id not found",
                "content": None,
                "witness_path": None,
                "truncated": False,
                "sha_hint": None,
            }
        )
    repo_root = resolved_path
    if repo_root is None and repo_id is not None:
        repo_root = _resolve_repo_root_by_id(repo_id)
    if repo_root is None:
        return _envelope(
            {
                "status": "failed",
                "error": "repo_id not found",
                "content": None,
                "witness_path": None,
                "truncated": False,
                "sha_hint": None,
            }
        )

    try:
        loaded = load_profile_dir(_effective_profile_dir(repo_root))
    except Exception:
        return _envelope(
            {
                "content": "",
                "witness_path": None,
                "truncated": False,
                "sha_hint": None,
            }
        )

    known_archetypes = loaded.archetypes.get("archetypes", {}) or {}
    if archetype not in known_archetypes:
        found_in_workspace = False
        for ws_cham in _iter_workspace_chameleon_dirs(repo_root):
            if not (ws_cham / "profile.json").is_file():
                continue
            try:
                ws_loaded = load_profile_dir(ws_cham)
                ws_archs = ws_loaded.archetypes.get("archetypes", {}) or {}
                if archetype in ws_archs:
                    loaded = ws_loaded
                    repo_root = ws_cham.parent
                    known_archetypes = ws_archs
                    found_in_workspace = True
                    break
            except Exception:
                continue
        if not found_in_workspace:
            return _envelope(
                {
                    "status": "failed",
                    "error": "archetype not found",
                    "archetype_name": archetype,
                    "repo_id": repo_id,
                    "content": None,
                    "witness_path": None,
                    "truncated": False,
                    "sha_hint": None,
                }
            )

    canonicals = loaded.canonicals.get("canonicals", {}).get(archetype, [])
    if not canonicals:
        return _envelope(
            {
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
            }
        )

    first = canonicals[0]
    witness = first.get("witness", {}) or {}
    witness_rel = witness.get("path")
    if not witness_rel:
        return _envelope(
            {
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
            }
        )

    # Trust gate at the data layer, before any witness read. This tool is
    # model-callable and previously returned full witness content for ANY trust
    # state. An untrusted profile is attacker-controllable, so block it here
    # (the repo arg may be a path, so repo_id is None — derive it from the final
    # repo_root, which already accounts for the workspace fallback above). Stale
    # still flows, matching get_pattern_context and the documented contract.
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    gate_repo_id = _compute_repo_id(repo_root)
    _gate_rec = _trust_state_for(gate_repo_id)
    if _gate_rec is None or not _gate_rec.grants_root(repo_root):
        return _envelope(
            {
                "status": "untrusted",
                "reason": "profile is not trusted for this user; grant with /chameleon-trust",
                "archetype_name": archetype,
                "repo_id": gate_repo_id,
                "content": None,
                "witness_path": witness_rel,
                "truncated": False,
                "sha_hint": witness.get("sha_hint"),
            }
        )

    try:
        from chameleon_mcp.safe_open import (
            FileTooLargeError,
            UnsafeFileError,
            safe_read_text,
        )

        content = safe_read_text(repo_root, witness_rel, max_size_bytes=WITNESS_MAX_BYTES)
    except FileTooLargeError:
        # Over the 5 MB ceiling: flag it (truncated/oversize) instead of an empty
        # success, so an explicit agent pull still learns the witness exists.
        return _envelope(
            {
                "status": "oversize",
                "content": "",
                "witness_path": witness_rel,
                "truncated": True,
                "sha_hint": witness.get("sha_hint"),
            }
        )
    except (UnsafeFileError, FileNotFoundError, OSError):
        return _envelope(
            {
                "content": "",
                "witness_path": witness_rel,
                "truncated": False,
                "sha_hint": witness.get("sha_hint"),
            }
        )

    # The witness passed the secret/injection scan at bootstrap time, but the
    # working-tree file may have been edited since. Re-scan the freshly-read
    # content before it reaches model context; drop it on a hit. Fail-open if
    # the scanner can't be imported (matches the canonical-ref materialize path).
    try:
        from chameleon_mcp.bootstrap.canonical_scanner import is_safe_canonical

        if not is_safe_canonical(content):
            return _envelope(
                {
                    "status": "no_witness",
                    "reason": (
                        "live canonical witness now contains a secret or "
                        "injection pattern; run /chameleon-refresh"
                    ),
                    "archetype_name": archetype,
                    "repo_id": repo_id,
                    "content": None,
                    "witness_path": witness_rel,
                    "truncated": False,
                    "sha_hint": witness.get("sha_hint"),
                }
            )
    except Exception:
        pass

    content = sanitize_for_chameleon_context(content)
    return _envelope(
        {
            "content": content,
            "witness_path": witness_rel,
            "truncated": False,
            "sha_hint": witness.get("sha_hint"),
        }
    )


def get_rules(repo: str, source: str | None = None, **kwargs) -> dict:
    """Return repo-global rules (eslint, prettier, rubocop, tsconfig) keyed
    by source/tool.

    Parameters:
      ``repo``: absolute path or 64-char hex repo_id.
      ``source``: optional tool/source filter (``"eslint"``,
                  ``"rubocop"``, ``"formatting"``, etc.). When omitted,
                  returns all rules.

    Bug 1 follow-up: the historical ``archetype=`` kwarg has
    been REMOVED from the public schema. The signature accepts
    ``**kwargs`` so a stale caller passing ``archetype=`` gets a clear
    deprecation error envelope instead of a TypeError — but the kwarg
    is no longer advertised in the MCP tool description. Callers must
    migrate to ``source=``.

    Usage:
      - ``get_rules(repo)`` → all rules (full source map).
      - ``get_rules(repo, "eslint")`` → only the eslint source block.
      - ``get_rules(repo, "lint")`` → substring match against source
        keys; matches ``eslint``. Kept for callers that relied on
        partial matching.
      - ``get_rules(repo, "component")`` where ``"component"`` matches
        an entry in ``archetypes.json`` → ``{status: failed, error: ...}``
        envelope explaining rules are source-scoped.
    """
    from chameleon_mcp.profile.loader import load_profile_dir

    deprecation_note = None
    legacy_archetype = kwargs.pop("archetype", None)
    if kwargs:
        unknown = sorted(kwargs.keys())
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"get_rules got unexpected keyword argument(s): {unknown!r}. "
                    "Use `repo` and (optionally) `source`."
                ),
            }
        )
    if legacy_archetype is not None and source is None:
        source = legacy_archetype
        deprecation_note = (
            "the 'archetype' parameter was removed; the call "
            "still resolves but rename it to 'source' — rules are "
            "tool-scoped (eslint / rubocop / etc), not archetype-scoped."
        )
    elif legacy_archetype is not None and source is not None:
        deprecation_note = (
            "both 'source' and 'archetype' were passed; 'archetype' is "
            "removed. Drop it; 'source' wins."
        )

    repo_root, repo_id = _resolve_repo_arg(repo)
    if repo_root is None and repo_id is not None:
        repo_root = _resolve_repo_root_by_id(repo_id)
    if repo_root is None or not repo_root.is_dir():
        env = {"rules": []}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    # Trust gate: rules.json is derived from committed (attacker-controllable)
    # eslint/rubocop/tsconfig config. This tool is model-callable, so withhold
    # it for an untrusted profile. Stale still flows (trusted once).
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    _gate_rec = _trust_state_for(_compute_repo_id(repo_root))
    if _gate_rec is None or not _gate_rec.grants_root(repo_root):
        env = {"status": "untrusted", "rules": []}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    try:
        loaded = load_profile_dir(_effective_profile_dir(repo_root))
    except Exception:
        env = {"rules": []}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    rules_dict = loaded.rules.get("rules", {}) or {}
    if source is None:
        env = {"rules": list(rules_dict.items())}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    if source in rules_dict:
        env = {"rules": [(source, rules_dict[source])]}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    if source in loaded.archetype_names:
        sources = sorted(rules_dict.keys())
        env = {
            "status": "failed",
            "error": (
                f"{source!r} is an archetype name, but rules are "
                "source-scoped (eslint / formatting / typescript / "
                "rubocop), not archetype-scoped. Omit the argument to "
                "get all rules, or pass a source key. "
                f"Available sources in this profile: {sources}"
            ),
            "rules": [],
        }
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    filtered = [(k, v) for k, v in rules_dict.items() if source in str(k)]
    env = {"rules": filtered}
    if deprecation_note:
        env["deprecation"] = deprecation_note
    return _envelope(env)


def lint_file(repo: str, archetype: str, content: str, file_path: str | None = None) -> dict:
    """Compare `content` against the archetype's canonical AST shape; return
    structural violations.

    Phase 4.1: real implementation. The engine extracts the file's
    shape dimensions via language-aware regex heuristics (see
    `lint_engine.extract_dimensions`) and compares them against the
    archetype's `ast_query` block in canonicals.json.

    **Heuristic, not a parser.** `lint_file` runs regex-based extraction,
    not a real TS/Ruby parser. It detects top-level structural patterns
    (export shape, class/module/function counts, content directives, JSX
    presence) but will NOT flag syntax errors. `unparseable_regions` is
    always `[]` in the current implementation. A file with unclosed
    braces, mid-token cuts, or invalid syntax may still score against an
    archetype's structural fingerprint — the engine reports what it sees,
    not whether the file parses. Real-parser validation happens at
    bootstrap time via `scripts/ts_dump.mjs` / `scripts/prism_dump.rb`,
    not on the per-edit hot path.

    Resolution rules:
    - If `repo` can be resolved to a profile dir AND the archetype has a
      non-null ast_query, run the real engine and return `"stub": False`.
    - If the archetype exists but its ast_query is null / missing, return
      a real-envelope shape with `"stub": False` and a `"noop_reason"`
      field explaining the no-op (the engine ran; it just had nothing to
      check). Earlier this field was named `reason`; rename is
      internal-consistency only.
    - If the repo / profile cannot be resolved at all, fall back to the
      legacy stub envelope (`"stub": True`) so callers without a real
      profile continue to see the no-op semantics they have always relied on.

    The 100 KB content cap is preserved: oversized content is
    flagged via the `truncated` envelope field and the engine processes
    the truncated buffer (not the full content). The engine is pure
    except for the advisory phantom-import check, which probes the
    filesystem for unresolved relative / tsconfig-alias imports; that
    check is silent unless `file_path` is an absolute path under `repo`.
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
        recalibrate_ast_query as _recalibrate_ast_query,
    )
    from chameleon_mcp.lint_engine import (
        scan_secrets as _scan_secrets,
    )
    from chameleon_mcp.profile.loader import load_profile_dir

    if not isinstance(content, str):
        return _envelope(
            {
                "stub": True,
                "stub_reason": (f"content must be a string; got {type(content).__name__}"),
                "violations": [],
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": 0,
            },
        )
    if not isinstance(archetype, str):
        return _envelope(
            {
                "stub": True,
                "stub_reason": (f"archetype must be a string; got {type(archetype).__name__}"),
                "violations": [],
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": len(content),
            },
        )

    content_size = len(content)
    truncated = content_size > 100_000
    working_content = content[:100_000] if truncated else content

    secret_violations = [v.to_dict() for v in _scan_secrets(working_content)]

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

    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    _gate_rec = _trust_state_for(_compute_repo_id(repo_root))
    if _gate_rec is None or not _gate_rec.grants_root(repo_root):
        # Untrusted profile: withhold convention/AST checks — their messages
        # embed attacker-controllable conventions.json / witness strings. Secret
        # detection on the caller's OWN submitted content is independent of
        # profile trust, so it still runs. Stale flows. Mirrors the data-layer
        # gate on the other model-callable tools and posttool_verify.
        return _envelope(
            {
                "stub": True,
                "status": "untrusted",
                "stub_reason": (
                    "profile is not trusted for this user; grant with "
                    "/chameleon-trust (convention/AST checks withheld)"
                ),
                "violations": secret_violations,
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": content_size,
            },
            truncated=truncated,
        )

    try:
        loaded = load_profile_dir(_effective_profile_dir(repo_root))
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

    _ws_fallback_used = False
    if not entries:
        for ws_cham in _iter_workspace_chameleon_dirs(repo_root):
            if not (ws_cham / "profile.json").is_file():
                continue
            try:
                ws_loaded = load_profile_dir(ws_cham)
                ws_entries = (ws_loaded.canonicals.get("canonicals", {}) or {}).get(archetype) or []
                if ws_entries:
                    loaded = ws_loaded
                    repo_root = ws_cham.parent
                    canonicals = ws_loaded.canonicals.get("canonicals", {}) or {}
                    entries = ws_entries
                    _ws_fallback_used = True
                    break
            except Exception:
                continue

    if _ws_fallback_used:
        # The fallback reassigned repo_root to a workspace whose committed
        # profile is attacker-controllable. The early gate only covered the
        # top-level root, so re-gate against the final workspace root before its
        # conventions/AST queries reach the model surface.
        _gate_rec2 = _trust_state_for(_compute_repo_id(repo_root))
        if _gate_rec2 is None or not _gate_rec2.grants_root(repo_root):
            return _envelope(
                {
                    "stub": True,
                    "status": "untrusted",
                    "stub_reason": (
                        "profile is not trusted for this user; grant with "
                        "/chameleon-trust (convention/AST checks withheld)"
                    ),
                    "violations": secret_violations,
                    "canonical_confidence": 0.0,
                    "unparseable_regions": [],
                    "content_size": content_size,
                },
                truncated=truncated,
            )

    ast_query: dict | None = None
    witness_rel_path: str | None = None
    if entries:
        first = entries[0] or {}
        ast_query = (first.get("normative_shape") or {}).get("ast_query")
        witness_rel_path = (first.get("witness") or {}).get("path")

    candidate_queries: list[dict] = []
    if ast_query and entries:
        profile_lang = loaded.profile.get("language")
        for entry in entries:
            entry = entry or {}
            e_ast = (entry.get("normative_shape") or {}).get("ast_query")
            e_witness = (entry.get("witness") or {}).get("path")
            if not e_ast or not e_witness:
                continue
            try:
                # Route through safe_open for path-safety (reject traversal /
                # symlinks / non-regular files — a committed witness path is
                # attacker-controllable), but preserve the old truncate-and-use
                # semantics: only the first 100KB feeds the AST recalibration, so
                # a legitimately large witness must NOT be dropped on size.
                from chameleon_mcp.safe_open import safe_open

                _wpath = safe_open(repo_root, e_witness, max_size_bytes=WITNESS_MAX_BYTES)
                w_raw = _wpath.read_bytes()[:100_000].decode("utf-8", errors="replace")
                w_lang = _detect_language(e_witness) or profile_lang
                w_snap = _extract_dimensions(w_raw, language=w_lang)
                candidate_queries.append(_recalibrate_ast_query(w_snap))
            except Exception:
                continue
    if not candidate_queries and ast_query:
        candidate_queries = [ast_query]

    if not candidate_queries:
        return _envelope(
            {
                "stub": False,
                "stub_reason": None,
                "violations": secret_violations,
                "canonical_confidence": 0.0,
                "unparseable_regions": [],
                "content_size": content_size,
                "noop_reason": (
                    "no ast_query for archetype "
                    f"{archetype!r} — re-bootstrap via /chameleon-refresh"
                ),
            },
            truncated=truncated,
        )

    language = (
        _detect_language(file_path)
        or _detect_language(witness_rel_path)
        or loaded.profile.get("language")
    )
    if language not in ("typescript", "ruby"):
        language = None

    snapshot = _extract_dimensions(working_content, language=language)
    best_ast_violations: list = []
    best_confidence = 0.0
    best_struct_count = float("inf")
    for cq in candidate_queries:
        v_list = _lint(snapshot, cq)
        c = _canonical_confidence(snapshot, cq)
        struct_count = sum(1 for v in v_list if v.rule == "top-level-node-kinds-mismatch")
        if struct_count < best_struct_count or (
            struct_count == best_struct_count and c > best_confidence
        ):
            best_ast_violations = [v.to_dict() for v in v_list]
            best_confidence = c
            best_struct_count = struct_count

    convention_violations: list[dict] = []
    try:
        from chameleon_mcp.lint_engine import lint_conventions as _lint_conventions

        conv_data = loaded.conventions.get("conventions", {})
        arch_conv: dict = {}
        if conv_data.get("imports", {}).get(archetype):
            arch_conv["imports"] = conv_data["imports"][archetype]
        if conv_data.get("naming", {}).get(archetype):
            arch_conv["naming"] = conv_data["naming"][archetype]
        if conv_data.get("inheritance", {}).get(archetype):
            arch_conv["inheritance"] = conv_data["inheritance"][archetype]
        if arch_conv:
            convention_violations = [
                v.to_dict()
                for v in _lint_conventions(working_content, arch_conv, language=language)
            ]
    except Exception:
        pass

    # Phantom-import check (filesystem-touching; advisory). Needs the edited
    # file's absolute path; silent when file_path is absent or unresolvable.
    phantom_violations: list[dict] = []
    try:
        from chameleon_mcp.phantom_imports import lint_phantom_imports

        phantom_violations = [
            v.to_dict()
            for v in lint_phantom_imports(
                working_content,
                file_path=file_path,
                repo_root=repo_root,
                language=language,
                rules=loaded.rules,
            )
        ]
    except Exception:
        phantom_violations = []

    # Sanitize profile-derived violation messages (they embed conventions.json /
    # witness-derived strings) before returning on a model-callable surface,
    # matching posttool_verify. Secret violations come from the caller's own
    # content and are left as-is.
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    for _v in best_ast_violations + convention_violations + phantom_violations:
        for _k, _val in list(_v.items()):
            if isinstance(_val, str):
                _v[_k] = _sanitize(_val)

    violations = (
        secret_violations + best_ast_violations + convention_violations + phantom_violations
    )

    return _envelope(
        {
            "stub": False,
            "stub_reason": None,
            "violations": violations,
            "canonical_confidence": best_confidence,
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

    Bug 4: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. Earlier, passing a path silently
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
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None and resolved_path is not None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )
    if repo_id is None:
        if "/" in repo or ".." in repo or "\\" in repo:
            return _envelope(
                {
                    "status": "failed",
                    "error": "expected repo path or repo_id hex digest",
                }
            )
        repo_id = repo

    repo_data = plugin_data_dir() / repo_id
    trust = trust_state_for(repo_id) if repo_data.is_dir() else None

    days_since_refresh: int | None = None
    if trust is not None and trust.granted_at:
        try:
            import calendar as _calendar

            granted_epoch = _calendar.timegm(time.strptime(trust.granted_at, "%Y-%m-%dT%H:%M:%SZ"))
            days_since_refresh = max(0, int((time.time() - granted_epoch) / 86_400))
        except ValueError:
            days_since_refresh = None

    drift_score = compute_drift_score(repo_id)

    # Engine-version mismatch is the strongest staleness signal: the analysis
    # logic, not just the codebase, changed. It outranks drift/age because a
    # refresh re-derives the profile regardless. This is the user-facing half of
    # the version-aware refresh (the refresh itself re-clusters on mismatch).
    engine_version_mismatch = False
    if resolved_path is not None:
        from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION

        engine_version_mismatch = _engine_version_changed(
            resolved_path / ".chameleon", ENGINE_MIN_VERSION
        )

    if engine_version_mismatch:
        recommended = "engine upgraded since this profile was built; run /chameleon-refresh"
    elif days_since_refresh is None:
        recommended = "no trust grant found; run /chameleon-trust first"
    elif drift_score is not None and drift_score > 0.5:
        recommended = f"observed drift is high ({drift_score:.2f}); run /chameleon-refresh"
    elif days_since_refresh > 90:
        recommended = "profile may be stale; run /chameleon-refresh"
    elif days_since_refresh > 30:
        recommended = "consider /chameleon-refresh if codebase has materially changed"
    else:
        recommended = "fresh"

    return _envelope(
        {
            "repo_id": repo_id,
            "days_since_refresh": days_since_refresh,
            "observed_drift_score": drift_score,
            "engine_version_mismatch": engine_version_mismatch,
            "recommended_action": recommended,
        }
    )


def get_status(repo: str) -> dict:
    """Report enforcement state for a repo's chameleon profile.

    Surfaces the three things the user needs to reason about blocking:
    - ``mode`` — the configured enforcement master switch
      (``off`` advisory only / ``shadow`` log-but-never-block / ``enforce``).
    - ``active`` — block rules that calibration kept active against this
      repo's own committed files (zero / near-zero false positives).
    - ``demoted`` — block rules calibration kept advisory, each with the
      false-positive rate that demoted it, so a user can see *why* a rule
      that blocks elsewhere is silent here.
    - ``idiom_review`` — whether the once-per-session Stop-hook idiom/principle
      self-review fires (default on in enforce mode).
    - ``idiom_judge`` — opt-in flag that strengthens the idiom-review directive.

    Fail-open: a missing/corrupt config or enforcement.json degrades to the
    safest default (advisory mode, no active rules) rather than raising. The
    richer profile/trust/drift surface stays in the dedicated tools the
    /chameleon-status skill already calls; this returns only the enforcement
    section those tools do not cover.
    """
    from chameleon_mcp.enforcement_calibration import load_block_rules
    from chameleon_mcp.profile.loader import find_repo_root

    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    try:
        repo_root = find_repo_root(Path(repo).expanduser())
    except (OSError, ValueError):
        repo_root = None
    if repo_root is None:
        return _envelope({"status": "no_repo"})

    profile_dir = _effective_profile_dir(repo_root)

    mode = "off"
    idiom_review = True
    idiom_judge = False
    try:
        from chameleon_mcp.profile.config import load_config

        _enf = load_config(profile_dir).enforcement
        mode = _enf.mode
        idiom_review = _enf.idiom_review
        idiom_judge = _enf.idiom_judge
    except Exception:
        # Malformed config.json: enforcement features are inactive until
        # fixed. Report the safest mode rather than crashing the status call.
        mode = "off"

    active: list[str] = []
    demoted: list[dict] = []
    for rule, meta in load_block_rules(profile_dir).items():
        if not isinstance(meta, dict):
            continue
        if meta.get("active") is True:
            active.append(rule)
        else:
            demoted.append({"rule": rule, "fp_rate": meta.get("fp_rate")})
    active.sort()
    demoted.sort(key=lambda d: d["rule"])

    return _envelope(
        {
            "enforcement": {
                "mode": mode,
                "active": active,
                "demoted": demoted,
                "idiom_review": idiom_review,
                "idiom_judge": idiom_judge,
            }
        }
    )


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


def _hash_cluster_key_for(key, split_tag: str = "") -> str:
    """Compute the 16-char cluster_id hash for a ClusterKey (+ split_tag).

    Mirrors `bootstrap.canonical._hash_cluster_key` EXACTLY so the
    cluster_ids stored in file_clusters match the cluster_ids written
    into archetypes.json. Duplicating the helper here keeps `tools.py`
    independent of `canonical.py` for this code path; the upstream helper
    is private to the bootstrap layer. The split_tag handling MUST stay
    byte-identical to canonical._hash_cluster_key.
    """
    key_dict = key.to_dict()
    split_tag = split_tag or ""
    if split_tag:
        payload = {"k": key_dict, "s": split_tag}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    else:
        canonical = json.dumps(key_dict, sort_keys=True, separators=(",", ":"))
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
    is bounded by REPO_SIZE_GUARD (200_000 files) and runs synchronously
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
        candidates = discover_files(repo_root, glob=discovery_glob, paths_glob=paths_glob)
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
        cluster_id = _hash_cluster_key_for(cluster.key, getattr(cluster, "split_tag", ""))
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
        cluster_id = _hash_cluster_key_for(cluster.key, getattr(cluster, "split_tag", ""))
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

    change_count = len(modified) + len(added) + len(removed)
    denom = max(1, len(prev_state))
    change_ratio = change_count / denom
    if change_ratio > PARTIAL_REFRESH_CHANGE_RATIO_CEILING:
        return None
    if change_count == 0:
        return None

    try:
        from chameleon_mcp.safe_open import (
            UnsafeFileError,
            safe_read_profile_artifact,
        )

        archetypes_data = json.loads(safe_read_profile_artifact(profile_dir / "archetypes.json"))
        canonicals_data = json.loads(safe_read_profile_artifact(profile_dir / "canonicals.json"))
        profile_data = json.loads(safe_read_profile_artifact(profile_dir / "profile.json"))
        rules_data = json.loads(safe_read_profile_artifact(profile_dir / "rules.json"))
    except (OSError, json.JSONDecodeError, UnsafeFileError):
        return None

    archetypes = archetypes_data.get("archetypes", {}) or {}
    cluster_id_to_archetype: dict[str, str] = {}
    for name, arch in archetypes.items():
        cid = (arch or {}).get("cluster_id")
        if cid:
            cluster_id_to_archetype[cid] = name

    reparse_paths = [current_by_rel[rel]["path"] for rel in (modified + added)]
    reparsed = _reparse_changed_files(repo_root, reparse_paths)
    if reparsed is None:
        return None

    for rel in modified + added:
        if rel not in reparsed:
            return None

    for rel in modified + added:
        new_cid, _ = reparsed[rel]
        if new_cid not in cluster_id_to_archetype:
            return None

    canonicals = canonicals_data.get("canonicals", {}) or {}
    for rel in modified + removed:
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
            return None

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

    new_generation = int(started_at)
    archetypes_data["archetypes"] = new_archetypes
    archetypes_data["generation"] = new_generation
    canonicals_data["generation"] = new_generation
    profile_data["generation"] = new_generation
    rules_data["generation"] = new_generation

    profile_data["archetype_count"] = len(new_archetypes)

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

    renames_text: str | None = None
    renames_path_partial = profile_dir / "renames.json"
    if renames_path_partial.is_file():
        from chameleon_mcp.safe_open import (
            UnsafeFileError as _UnsafeFileError,
        )
        from chameleon_mcp.safe_open import (
            safe_read_profile_artifact as _safe_read_profile_artifact,
        )

        try:
            renames_text = _safe_read_profile_artifact(renames_path_partial)
        except (OSError, FileNotFoundError, _UnsafeFileError):
            renames_text = None

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
            (txn_dir / "profile.summary.md").write_text(summary_text, encoding="utf-8")
            if renames_text is not None:
                (txn_dir / "renames.json").write_text(renames_text, encoding="utf-8")
    except Exception:
        return None

    upsert_rows: list[tuple[str, str, str | None]] = []
    for rel in modified + added:
        new_cid, new_sha = reparsed[rel]
        upsert_rows.append((rel, new_cid, new_sha))
    for rel in unchanged:
        prev = prev_state[rel]
        upsert_rows.append(
            (
                rel,
                prev.get("cluster_id") or "",
                current_by_rel[rel].get("sha_hint") or prev.get("sha_hint"),
            )
        )
    if upsert_rows:
        index_db.upsert_file_clusters(repo_id, upsert_rows)
    if removed:
        index_db.delete_file_clusters_for_paths(repo_id, removed)

    duration_ms = int((time.time() - started_at) * 1000)
    files_processed = len(unchanged) + len(modified) + len(added)

    # The partial path rewrites canonicals.json (the witness set), so the
    # block-rule verdict in enforcement.json must be re-measured against the
    # new profile; otherwise it stays pinned to the pre-refresh witnesses.
    # Calibrate before the hash snapshot so enforcement.json (part of the
    # trust-hashed surface) is reflected in the index.db profile_sha256 mirror.
    _calibrate_block_rules_for_repo(repo_root)

    index_db.upsert_repo(
        repo_id,
        str(repo_root),
        profile_sha256=hash_profile(profile_dir),
        archetype_count=profile_data["archetype_count"],
        files_indexed=files_processed,
        bootstrap_ms=duration_ms,
    )

    return _envelope(
        {
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
        }
    )


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
    state in index.db (legacy profiles, or any repo where the
    initial bootstrap predates this feature) fall through to full
    re-bootstrap unconditionally.

    `force=True` bypasses BOTH short-circuits and always re-bootstraps.

    Bug 1: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.

    Concurrency: acquires .chameleon/.refresh.lock (non-blocking flock) at
    the top of the function. A second concurrent /chameleon-refresh call
    returns a fast "failed" envelope instead of serializing on the 30s
    rename flock inside atomic_profile_commit.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    _clear_repo_id_cache()

    if not _validate_file_path_arg(repo):
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    repo_path = resolved_path
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope(
            {
                "status": "failed",
                "error": "refresh_repo expects an absolute repo path",
            }
        )

    # Lock lives in plugin-data, NOT inside .chameleon/: atomic_profile_commit
    # renames the whole .chameleon/ dir away during refresh, which orphaned a
    # lock held inside it — a second /chameleon-refresh starting after the rename
    # flocked a DIFFERENT inode and ran concurrently. A stable per-repo
    # plugin-data path keeps the lock inode constant across the profile swap.
    from chameleon_mcp.profile.trust import repo_data_dir

    _lock_dir = repo_data_dir(_compute_repo_id(repo_path))
    _lock_dir.mkdir(parents=True, exist_ok=True)
    refresh_lock_path = _lock_dir / ".refresh.lock"
    try:
        with acquire_advisory_lock(refresh_lock_path):
            pre_state = _capture_pre_refresh_state(repo_path)
            envelope = _refresh_repo_locked(repo_path, force=force)
            _inject_archetype_diff(envelope, repo_path, pre_state)
            _maybe_preserve_trust_across_refresh(repo_path, pre_state, envelope)
            # Keep the status line in sync with the post-refresh trust state
            # (a refresh can flip trusted->stale; the cache otherwise lags a session).
            try:
                _ts = (
                    detect_repo(str(repo_path / "profile.json")).get("data", {}).get("trust_state")
                )
                if isinstance(_ts, str) and _ts in ("trusted", "stale", "untrusted"):
                    _update_statusline_trust(repo_path, _ts)
            except Exception:
                pass
            _notify_daemon_cache_invalidation()
            return envelope
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another /chameleon-refresh is in progress (PID {e.holder_pid}); retry shortly"
                ),
            }
        )


def _capture_pre_refresh_state(repo_path: Path) -> dict | None:
    """Snapshot the pre-refresh archetypes for rename-aware diff in the
    response envelope, plus the structural hashes + trust record so we
    can preserve trust across no-op refreshes (rec-2).
    Tolerant: returns None on any error so the diff silently degrades
    to empty rather than breaking refresh.
    """
    from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir
    from chameleon_mcp.profile.trust import trust_state_for

    profile_dir = repo_path / ".chameleon"
    if not profile_dir.is_dir():
        return None
    try:
        loaded = load_profile_dir(profile_dir)
    except (ProfileLoadError, OSError):
        return None
    archetypes = loaded.archetypes.get("archetypes", {}) or {}
    repo_id = _compute_repo_id(repo_path)
    trust = trust_state_for(repo_id)
    return {
        "names": set(archetypes.keys()),
        "renames_overlay": _read_renames_overlay(profile_dir),
        "structural_hashes": _structural_hashes(profile_dir),
        "trust_record_existed": trust is not None,
        "repo_id": repo_id,
    }


def _structural_hashes(profile_dir: Path) -> dict[str, str]:
    """Hash the LLM-visible content of each artifact, EXCLUDING the
    generation counter + created_at timestamp.

    Used by `_maybe_preserve_trust_across_refresh` to detect a refresh
    that produced byte-identical content (modulo the always-bumping
    generation field). When all structural hashes match pre/post, the
    refresh is materially a no-op and trust should be preserved per
    the chameleon-init skill ("Run /chameleon-refresh to re-analyze
    without clearing trust state").
    """
    import hashlib

    from chameleon_mcp.safe_open import (
        UnsafeFileError,
        safe_read_profile_artifact_bytes,
    )

    _STRIP_KEYS = frozenset(
        {
            "generation",
            "created_at",
            "updated_at",
            "computed_at",
            "scanned_at",
        }
    )

    def _strip_volatile(obj):
        if isinstance(obj, dict):
            return {k: _strip_volatile(v) for k, v in obj.items() if k not in _STRIP_KEYS}
        if isinstance(obj, list):
            return [_strip_volatile(item) for item in obj]
        return obj

    def _hash_json(path: Path) -> str | None:
        try:
            raw = safe_read_profile_artifact_bytes(path)
        except (FileNotFoundError, OSError, UnsafeFileError):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return hashlib.sha256(raw).hexdigest()
        stripped = _strip_volatile(data)
        canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _hash_text(path: Path) -> str | None:
        try:
            raw = safe_read_profile_artifact_bytes(path)
        except (FileNotFoundError, OSError, UnsafeFileError):
            return None
        return hashlib.sha256(raw).hexdigest()

    out: dict[str, str] = {}
    for fname in ("archetypes.json", "canonicals.json", "rules.json"):
        h = _hash_json(profile_dir / fname)
        if h is not None:
            out[fname] = h
    h = _hash_text(profile_dir / "idioms.md")
    if h is not None:
        out["idioms.md"] = h
    return out


def _maybe_preserve_trust_across_refresh(
    repo_path: Path, pre_state: dict | None, envelope: dict
) -> None:
    """Re-grant trust if refresh produced a materially-identical profile.

    Bug 2 / rec-2 follow-up: the chameleon-init skill states
    /chameleon-refresh re-analyzes "without clearing trust state", but
    the implementation invalidated trust on every refresh because the
    generation counter bumped on each run (changing the trust hash).

    Three paths re-grant trust:
      1. Structural-equality: pre/post hashes match AND
         archetype_diff is empty AND a trust record existed.
      2. Pulled-from-remote: the profile change came from a
         git pull by a different author AND
         ``config.trust.auto_preserve_when == "pulled_from_remote"``.
         Lets a teammate's profile update flow through without forcing
         the user to re-trust every time someone else pushes a refresh.

    Any real content change made by the same user still invalidates
    trust (the user typed it; they should re-trust their own change).
    """
    if pre_state is None:
        return
    if not pre_state.get("trust_record_existed"):
        return
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return

    profile_dir = repo_path / ".chameleon"
    diff = data.get("archetype_diff") or {}
    structurally_identical = not (
        diff.get("added")
        or diff.get("removed")
        or diff.get("renamed")
        or diff.get("dropped_invalid_names")
    ) and _structural_hashes(profile_dir) == (pre_state.get("structural_hashes") or {})

    preserve_reason: str | None = None
    if structurally_identical:
        preserve_reason = "structural_equality"
    else:
        try:
            from chameleon_mcp.profile.config import load_config

            cfg = load_config(profile_dir)
            if cfg.trust.auto_preserve_when == "always":
                # User opted into auto-trust across every refresh (manual or
                # auto), so they aren't re-prompted on their own repo.
                preserve_reason = "always"
            elif cfg.trust.auto_preserve_when == "pulled_from_remote":
                if _profile_change_came_from_remote_pull(repo_path):
                    preserve_reason = "pulled_from_remote"
        except Exception:  # noqa: BLE001
            pass

    if preserve_reason is None:
        return

    try:
        from chameleon_mcp.profile.trust import grant_trust

        grant_trust(pre_state["repo_id"], profile_dir)
        data["trust_preserved"] = True
        data["trust_preserve_reason"] = preserve_reason
    except Exception:
        pass


def _profile_change_came_from_remote_pull(repo_path: Path) -> bool:
    """Heuristic: was the latest ``.chameleon/profile.json`` change pulled?

    Returns True when the most-recent commit touching ``profile.json`` was
    authored by someone OTHER than the current local user. That's the
    shape of a teammate's profile update flowing in via ``git pull``.

    Returns False when:
      - the repo isn't a git repo (``git`` returns non-zero)
      - the latest commit author matches the current user (local edit)
      - any subprocess call fails

    The git work is bounded by a 2-second timeout so a hung subprocess
    can never block trust preservation indefinitely.
    """
    import subprocess as _sp

    try:
        current = _sp.run(
            ["git", "-C", str(repo_path), "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if current.returncode != 0:
            return False
        current_email = (current.stdout or "").strip().lower()
        if not current_email:
            return False

        commit_author = _sp.run(
            [
                "git",
                "-C",
                str(repo_path),
                "log",
                "-1",
                "--format=%ae",
                "--",
                ".chameleon/profile.json",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if commit_author.returncode != 0:
            return False
        latest_author = (commit_author.stdout or "").strip().lower()
        if not latest_author:
            return False

        return latest_author != current_email
    except Exception:  # noqa: BLE001 (subprocess.TimeoutExpired, OSError)
        return False


def _inject_archetype_diff(
    envelope: dict,
    repo_path: Path,
    pre_state: dict | None,
) -> None:
    """Mutate ``envelope`` to add a sanitized ``archetype_diff`` field.

    Rec 6: surface what actually changed across a refresh
    (added / removed / renamed / unchanged) instead of just an
    archetype_count delta. Renames are detected via the
    ``renames.json`` overlay: a name that appears in
    ``renames.json[old] == new`` is reported as a rename instead of
    an add + remove pair.

    All archetype names returned are filtered through
    ``ARCHETYPE_NAME_RE``; non-conformant entries are dropped and
    counted in ``dropped_invalid_names`` so a hand-edited
    archetypes.json can't smuggle prompt-injection text through the
    refresh response. Empty diff is safe to emit (no-op refresh).
    """
    from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE

    if pre_state is None:
        return
    data = envelope.get("data")
    if not isinstance(data, dict):
        return

    profile_dir = repo_path / ".chameleon"
    try:
        loaded = load_profile_dir(profile_dir)
    except (ProfileLoadError, OSError):
        return
    post_names_raw = set((loaded.archetypes.get("archetypes", {}) or {}).keys())
    pre_names = pre_state.get("names") or set()

    renames_after = _read_renames_overlay(profile_dir)
    renamed_pairs: list[tuple[str, str]] = []
    rename_old: set[str] = set()
    rename_new: set[str] = set()
    for old, new in renames_after.items():
        if old in pre_names and new in post_names_raw and old not in post_names_raw:
            renamed_pairs.append((old, new))
            rename_old.add(old)
            rename_new.add(new)

    def _sane(names: set[str]) -> tuple[list[str], int]:
        good: list[str] = []
        dropped = 0
        for n in sorted(names):
            if isinstance(n, str) and ARCHETYPE_NAME_RE.match(n):
                good.append(n)
            else:
                dropped += 1
        return good, dropped

    added_raw = post_names_raw - pre_names - rename_new
    removed_raw = pre_names - post_names_raw - rename_old
    unchanged_raw = pre_names & post_names_raw

    added, d_added = _sane(added_raw)
    removed, d_removed = _sane(removed_raw)
    unchanged, d_unchanged = _sane(unchanged_raw)

    renamed: list[dict] = []
    d_renamed = 0
    for old, new in sorted(renamed_pairs):
        if (
            isinstance(old, str)
            and isinstance(new, str)
            and ARCHETYPE_NAME_RE.match(old)
            and ARCHETYPE_NAME_RE.match(new)
        ):
            renamed.append({"from": old, "to": new})
        else:
            d_renamed += 1

    diff: dict = {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "unchanged_count": len(unchanged),
    }
    dropped = d_added + d_removed + d_unchanged + d_renamed
    if dropped:
        diff["dropped_invalid_names"] = dropped
    data["archetype_diff"] = diff


def _persisted_paths_glob(profile_dir: Path) -> str | None:
    """Return the persisted user-supplied paths_glob from profile.json, or None.

    Bug 1 / rec-1 follow-up: bootstrap_repo persists the
    user-supplied paths_glob under profile_data["discovery"]["paths_glob"];
    /chameleon-refresh reads it here so the same scope re-applies on a
    full re-bootstrap. Tolerant: any error returns None and refresh
    falls back to the default extractor-driven glob (status-quo
    pre-fix behavior).
    """
    profile_path = profile_dir / "profile.json"
    if not profile_path.is_file():
        return None
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    discovery = data.get("discovery") if isinstance(data, dict) else None
    if not isinstance(discovery, dict):
        return None
    pg = discovery.get("paths_glob")
    return pg if isinstance(pg, str) and pg else None


def _profile_engine_version(profile_dir) -> str:
    """Return the engine_min_version stamped in the profile, or '' if absent."""
    import json as _json

    for fname in ("archetypes.json", "profile.json"):
        p = profile_dir / fname
        if p.is_file():
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            v = data.get("engine_min_version") if isinstance(data, dict) else None
            if v:
                return str(v)
    return ""


def _engine_version_changed(profile_dir, current_version: str) -> bool:
    """True when the profile was generated by a different engine version.

    Used to bypass the refresh noop short-circuit after an engine upgrade: the
    files may be unchanged, but the clustering/analysis logic isn't, so the
    profile must be re-derived. Absent/unreadable stamp -> False (can't prove a
    mismatch; don't force needless work).
    """
    pv = _profile_engine_version(profile_dir)
    return bool(pv) and bool(current_version) and pv != current_version


def _principles_incomplete(profile_dir) -> bool:
    """True if principles.md is absent or missing the always-on anti-hallucination
    protocol.

    principles.md is generated content, but the refresh noop and partial paths
    preserve it verbatim, so a stale profile (pre-1.4.0, or one whose principles
    were hand-edited / dropped) would never regain the protocol. Detecting that
    lets refresh force a full re-derive instead.
    """
    p = profile_dir / "principles.md"
    try:
        return "anti-hallucination protocol" not in p.read_text(encoding="utf-8").lower()
    except OSError:
        return True


def _profile_needs_rederive(profile_dir) -> bool:
    """True if the profile is structurally incomplete or corrupt.

    The refresh noop and partial paths preserve existing artifacts verbatim, so a
    profile damaged by a crashed bootstrap, partial write, bad merge, or manual
    edit would never be repaired by a normal refresh. This forces a full
    re-derive when any core generated artifact is missing or unparseable:

    - ``archetypes/canonicals/rules/conventions.json`` must exist and parse as
      JSON objects;
    - ``profile.summary.md`` must exist;
    - ``principles.md`` must carry the anti-hallucination protocol.

    ``idioms.md`` is user-taught content (preserved across a re-derive), so a
    missing idioms file does NOT force a rebuild.
    """
    import json as _json

    for name in ("archetypes.json", "canonicals.json", "rules.json", "conventions.json"):
        try:
            obj = _json.loads((profile_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(obj, dict):
            return True
    if not (profile_dir / "profile.summary.md").is_file():
        return True
    return _principles_incomplete(profile_dir)


def _update_statusline_trust(repo_path, trust_state: str) -> None:
    """Best-effort: update the per-project statusline cache trust value so the
    status line reflects a /chameleon-trust (or refresh) immediately, instead of
    the SessionStart snapshot — which kept showing ``(stale)`` after a successful
    trust until the next session. The cache profile is keyed by the repo
    directory name (matches the SessionStart writer). Never raises.
    """
    try:
        cache = Path(repo_path) / ".claude" / ".chameleon-statusline-cache"
        if not cache.is_file():
            return
        data = json.loads(cache.read_text(encoding="utf-8"))
        name = Path(repo_path).name
        changed = False
        for prof in data.get("profiles", []) or []:
            if prof.get("name") == name:
                prof["trust"] = trust_state
                changed = True
        if changed:
            cache.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _refresh_repo_locked(repo_path, *, force: bool) -> dict:
    """Execute refresh logic. Called while .chameleon/.refresh.lock is held."""
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.bootstrap.orchestrator import (
        _glob_for_extractor,
        _select_extractor,
    )

    started_at = time.time()
    profile_dir = repo_path / ".chameleon"
    persisted_pg = _persisted_paths_glob(profile_dir)

    if force:
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    repo_root = repo_path.resolve()
    repo_id = _compute_repo_id(repo_root)
    cached = index_db.get_repo(repo_id, repo_root_hint=str(repo_root))
    profile_path = profile_dir / "profile.json"

    if not (cached and profile_path.is_file()):
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    try:
        extractor = _select_extractor(repo_root)
    except Exception:
        extractor = None
    if extractor is None:
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    try:
        discovery_glob = persisted_pg or _glob_for_extractor(extractor)
        candidates = discover_files(repo_root, glob=discovery_glob, paths_glob=persisted_pg)
    except Exception:
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    cached_files = cached.get("files_indexed") or 0
    last_seen_iso = cached.get("last_seen_at") or ""
    last_seen_epoch = _iso_to_epoch(last_seen_iso)
    idioms_path = profile_dir / "idioms.md"
    refresh_inputs = list(candidates) + [idioms_path]
    max_mtime = index_db.max_mtime_over(refresh_inputs)
    cardinality_match = cached_files > 0 and len(candidates) == cached_files
    nothing_newer = last_seen_epoch > 0.0 and max_mtime <= last_seen_epoch

    missing_artifacts = (
        not (profile_dir / "conventions.json").is_file()
        or not (profile_dir / "principles.md").is_file()
    )

    # Engine-upgrade guard: a profile written by an older engine can have
    # unchanged files yet stale clustering/analysis. Re-derive fully instead of
    # noop-ing so the profile reflects the current engine.
    from chameleon_mcp.bootstrap.orchestrator import ENGINE_MIN_VERSION

    if _engine_version_changed(profile_dir, ENGINE_MIN_VERSION):
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    # Repair guard: the noop and partial paths preserve artifacts verbatim, so a
    # structurally incomplete or corrupt profile (missing/unparseable core JSON,
    # missing summary, or principles lacking the protocol) would never be fixed by
    # a normal refresh. Re-derive fully to repair it. A full re-derive preserves
    # user-taught idioms.md.
    if _profile_needs_rederive(profile_dir):
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    if cardinality_match and nothing_newer and not missing_artifacts:
        index_db.upsert_repo(
            repo_id,
            str(repo_root),
            archetype_count=cached.get("archetype_count"),
            files_indexed=cached_files,
            bootstrap_ms=cached.get("bootstrap_ms"),
            profile_sha256=cached.get("profile_sha256"),
        )
        return _envelope(
            {
                "status": "noop",
                "reason": "no files changed since last refresh",
                "archetypes_detected": cached.get("archetype_count") or 0,
                "files_processed": cached_files,
                "duration_ms": 0,
                "profile_path": str(profile_dir),
            }
        )

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

    return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)


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

    if "." in ts and ts.endswith("Z"):
        try:
            whole, frac = ts[:-1].split(".", 1)
            base = calendar.timegm(time.strptime(whole + "Z", "%Y-%m-%dT%H:%M:%SZ"))
            return base + float(f"0.{frac}")
        except (ValueError, TypeError):
            return 0.0
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def bootstrap_repo(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict:
    """First-time analysis, serialized by a per-repo advisory lock.

    Acquires a ``.bootstrap.lock`` in plugin-data so two concurrent inits on the
    same repo don't both run the clusterer and race the COMMITTED check. The
    lock is SEPARATE from ``.refresh.lock``: refresh calls this while holding
    its own lock, so the two distinct locks nest without re-entering the same
    flock (no deadlock). The actual work lives in ``_bootstrap_repo_unlocked``.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    resolved_path, _ = _resolve_repo_arg(path)
    if resolved_path is None or not resolved_path.is_dir():
        # Degenerate input (or by-id): let the core emit the precise envelope.
        return _bootstrap_repo_unlocked(path, paths_glob, force, now)
    try:
        repo_root = resolved_path.resolve()
    except (OSError, ValueError):
        repo_root = resolved_path
    from chameleon_mcp.profile.trust import repo_data_dir

    lock_dir = repo_data_dir(_compute_repo_id(repo_root))
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        with acquire_advisory_lock(lock_dir / ".bootstrap.lock"):
            return _bootstrap_repo_unlocked(path, paths_glob, force, now)
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another bootstrap is in progress for this repo (PID {e.holder_pid}); "
                    "retry shortly"
                ),
            }
        )


def _calibrate_block_rules_for_repo(repo_root: Path) -> None:
    """Measure block-eligible rules against the repo's own files and persist the
    verdict to ``.chameleon/enforcement.json``.

    Best-effort: a calibration failure must never fail bootstrap/refresh. When
    the artifact is absent or empty no rule is allowed to block (advisory only),
    which is the safe default.
    """
    try:
        from chameleon_mcp.enforcement_calibration import (
            calibrate_block_rules,
            write_block_rules,
        )
        from chameleon_mcp.profile.loader import load_profile_dir

        profile_dir = repo_root / ".chameleon"
        loaded = load_profile_dir(profile_dir)
        write_block_rules(profile_dir, calibrate_block_rules(repo_root, loaded))
    except Exception:
        pass


def _bootstrap_repo_unlocked(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit.

    For monorepos with detected workspace_paths, runs the full
    pipeline per workspace as well, producing one `.chameleon/` under each
    workspace root in addition to the root profile that catalogs them.

    Bug 1: `path` accepts either an absolute repo path or a
    64-char repo_id hex digest (for repos previously bootstrapped). See
    `_resolve_repo_arg`.

    BUG-026: refuses to overwrite a committed profile unless
    ``force=True``. Earlier a second call silently clobbered the
    existing profile; the /chameleon-init skill warned the model but the
    MCP had no defense in depth.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.orchestrator import bootstrap_repo as _bootstrap
    from chameleon_mcp.profile.trust import hash_profile

    _clear_repo_id_cache()

    if not _validate_file_path_arg(path):
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )

    resolved_path, _resolved_id = _resolve_repo_arg(path)
    if resolved_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    try:
        repo_root = resolved_path.resolve()
    except (OSError, ValueError):
        repo_root = resolved_path
    if not repo_root.is_dir():
        return _envelope(
            {
                "status": "failed",
                "error": f"path is not a directory: {path}",
            }
        )

    if now is not None:
        if not isinstance(now, int | float) or isinstance(now, bool):
            return _envelope(
                {
                    "status": "failed",
                    "error": f"now must be a finite non-negative float; got {type(now).__name__}",
                }
            )
        now_f = float(now)
        if math.isnan(now_f) or math.isinf(now_f) or now_f < 0:
            return _envelope(
                {
                    "status": "failed",
                    "error": f"now must be a finite non-negative float; got {now_f!r}",
                }
            )

    try:
        from chameleon_mcp.bootstrap.transaction import cleanup_orphan_tmp_dirs

        cleanup_orphan_tmp_dirs(repo_root)
    except Exception:
        pass

    if not force:
        committed_marker = repo_root / ".chameleon" / "COMMITTED"
        if committed_marker.is_file():
            profile_path = str(repo_root / ".chameleon")
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
            return _envelope(
                {
                    "status": "already_bootstrapped",
                    "profile_path": profile_path,
                    "message": (
                        "A committed profile already exists at this path. "
                        "Pass force=true to overwrite, or run /chameleon-refresh "
                        "to re-analyze without clearing trust state."
                    ),
                }
            )

    report = _bootstrap(repo_root, paths_glob=paths_glob, now=now)

    if report.status == "success":
        repo_id = _compute_repo_id(repo_root)
        # Calibrate before the hash snapshot: enforcement.json is part of the
        # trust-hashed surface, so writing it first keeps the index.db mirror
        # of profile_sha256 consistent with the hash a later trust grant
        # captures (otherwise the repo would read stale by one artifact).
        _calibrate_block_rules_for_repo(repo_root)
        index_db.upsert_repo(
            repo_id,
            str(repo_root),
            profile_sha256=hash_profile(repo_root / ".chameleon"),
            archetype_count=report.archetypes_detected,
            files_indexed=report.files_processed,
            bootstrap_ms=report.duration_ms,
        )
        try:
            file_cluster_rows = _compute_file_cluster_map(repo_root, paths_glob=paths_glob)
        except Exception:
            file_cluster_rows = None
        if file_cluster_rows is not None:
            index_db.delete_all_file_clusters(repo_id)
            if file_cluster_rows:
                index_db.upsert_file_clusters(repo_id, file_cluster_rows)
    # Index successfully-bootstrapped workspaces regardless of the root's
    # status: a coordinator-only root (non-standard package dir, no own
    # language) still produces working workspace profiles above.
    for ws in report.workspace_reports or []:
        if ws.get("status") != "success":
            continue
        ws_root_str = ws.get("repo_root")
        if not ws_root_str:
            continue
        ws_root = Path(ws_root_str)
        ws_repo_id = _compute_repo_id(ws_root)
        # Calibrate before the hash snapshot so enforcement.json is included in
        # the index.db mirror of profile_sha256 (see the root path above).
        _calibrate_block_rules_for_repo(ws_root)
        index_db.upsert_repo(
            ws_repo_id,
            str(ws_root),
            profile_sha256=hash_profile(ws_root / ".chameleon"),
            archetype_count=ws.get("archetypes_detected"),
            files_indexed=ws.get("files_processed"),
            bootstrap_ms=ws.get("duration_ms"),
        )
        try:
            ws_rows = _compute_file_cluster_map(ws_root, paths_glob=paths_glob)
        except Exception:
            ws_rows = None
        if ws_rows is not None:
            index_db.delete_all_file_clusters(ws_repo_id)
            if ws_rows:
                index_db.upsert_file_clusters(ws_repo_id, ws_rows)

    _notify_daemon_cache_invalidation()
    return _envelope(report.to_dict())


def list_profiles(cursor: str | None = None, limit: int = 100) -> dict:
    """List all known repos this user has touched.

    Phase 4.4: backed by `index.db`. Ordered by last_seen_at DESC (most
    recently bootstrapped/refreshed first), then by repo_id ASC as a
    stable tiebreaker.

    For backward compat with early installs that have ${PLUGIN_DATA}/
    populated but no index.db yet, we fall back to scanning the per-repo
    directory listing and best-effort backfill into the index. After one
    list_profiles call on an existing install, all known repos are
    represented in index.db.

    Validation behavior is preserved:
    - `limit` must be an int in 1..1000
    - an unknown `cursor` returns an explicit failure envelope
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    if not isinstance(limit, int) or limit <= 0 or limit > 1000:
        return _envelope(
            {
                "status": "failed",
                "error": "limit must be an integer in 1..1000",
            }
        )

    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"unknown cursor {cursor!r}; pass the next_cursor value from a prior page"
                    ),
                }
            )
        if cursor.count("|") not in (1, 2):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"unknown cursor {cursor!r}; pass the next_cursor value from a prior page"
                    ),
                }
            )

    _backfill_index_from_legacy_dirs()

    _prune_dead_temp_repos()

    try:
        page_rows, next_cursor, total_known = index_db.list_repos(cursor, limit)
    except ValueError:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"unknown cursor {cursor!r}; pass the next_cursor value from a prior page"
                ),
            }
        )

    base = plugin_data_dir()
    profiles = []
    for row in page_rows:
        repo_id = row["repo_id"]
        trust = trust_state_for(repo_id) if (base / repo_id).is_dir() else None
        profiles.append(
            {
                "repo_id": repo_id,
                "trust_state": "trusted" if trust else "untrusted",
                "trusted_at": trust.granted_at if trust else None,
                "trusted_by": trust.granted_by_user if trust else None,
                "repo_root": row.get("repo_root"),
                "archetype_count": row.get("archetype_count"),
                "files_indexed": row.get("files_indexed"),
                "bootstrap_ms": row.get("bootstrap_ms"),
                "last_seen_at": row.get("last_seen_at"),
            }
        )

    return _envelope(
        {"profiles": profiles, "total_known": total_known},
        next_cursor=next_cursor,
    )


_TEMP_PATH_PREFIXES: tuple[str, ...] = (
    "/private/var/folders/",
    "/var/folders/",
    "/tmp/",
    "/private/tmp/",
)


def _is_dead_temp_repo_root(repo_root: str | None) -> bool:
    """True if ``repo_root`` is a temp-dir path that no longer exists.

    Used by `_prune_dead_temp_repos` to safely identify
    list_profiles entries left behind by prior test runs. Returns
    False for any non-temp path so a user who moved or detached a
    real repo doesn't lose its cached state.
    """
    import os as _os

    if not repo_root or not isinstance(repo_root, str):
        return False
    if not any(repo_root.startswith(p) for p in _TEMP_PATH_PREFIXES):
        tmp_env = _os.environ.get("TMPDIR", "").rstrip("/")
        if not tmp_env or not repo_root.startswith(tmp_env + "/"):
            return False
    return not Path(repo_root).is_dir()


def _is_dead_chameleon_profile(repo_root: str | None) -> bool:
    """True if ``repo_root`` exists but its ``.chameleon/profile.json`` is gone.

    An external report flagged that a user who deletes
    ``.chameleon/`` from a still-extant repo (``rm -rf .chameleon``)
    leaves a tombstone row in index_db that surfaces in
    list_profiles forever. Pruning ANY repo whose profile no longer
    exists is a stronger sweep than the temp-only variant — the user
    has explicitly removed the profile, so the index row should
    follow suit.
    """
    if not repo_root or not isinstance(repo_root, str):
        return False
    root = Path(repo_root)
    if not root.is_dir():
        return False
    return not (root / ".chameleon" / "profile.json").is_file()


def _prune_dead_temp_repos() -> int:
    """Remove index_db rows for repos whose profile no longer exists.

    Two prune rules:
      1. ``repo_root`` is a temp-dir path AND no longer exists on disk
         (handles dogfood / test-run leftovers).
      2. ``repo_root`` is a real path AND exists BUT
         ``<repo_root>/.chameleon/profile.json`` is missing (user
         deleted the profile via ``rm -rf .chameleon``).

    Returns the number of rows removed. Best-effort: any error returns
    the running count and leaves the index alone.
    """
    from chameleon_mcp import index_db

    removed = 0
    try:
        rows, _next, _total = index_db.list_repos(None, 1000)
    except (ValueError, Exception):  # noqa: BLE001
        return 0
    for row in rows:
        repo_root = row.get("repo_root")
        if not (_is_dead_temp_repo_root(repo_root) or _is_dead_chameleon_profile(repo_root)):
            continue
        try:
            if index_db.forget_repo(row["repo_id"], repo_root=repo_root):
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


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
            d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")
        ]
    except OSError:
        return

    for repo_id in candidate_ids:
        if index_db.resolve_repo_root(repo_id):
            continue
        trust = trust_state_for(repo_id)
        if trust is None or not trust.repo_root:
            continue
        index_db.upsert_repo(
            repo_id,
            trust.repo_root,
            profile_sha256=trust.profile_sha256 or None,
            last_seen_at=trust.granted_at or None,
        )


_SAFE_TOP_LEVEL_KEYS = {
    "schema_version",
    "engine_min_version",
    "repo_id",
    "language",
    "language_hint",
    "source",
    "workspace",
    "workspaces",
    "platforms",
    "archetypes",
    "archetypes_detected",
    "archetype_count",
    "tool_configs",
    "generation",
    "created_at",
    "updated_at",
    "clustering_algorithm_version",
    "discovery",
}


def merge_profiles(repo: str, base: str, ours: str, theirs: str) -> dict:
    """Three-way merge for git merge driver use.

    Per docs/architecture.md "merge_profiles algorithm": the canonical-correct
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
    del base

    ours_path = Path(ours)
    theirs_path = Path(theirs)
    if not ours_path.is_file() or not theirs_path.is_file():
        return _envelope(
            {
                "status": "failed",
                "error": "ours and theirs must point to existing profile JSON files",
                "merged_profile_path": None,
            }
        )

    try:
        ours_data = json.loads(ours_path.read_text(encoding="utf-8"))
        theirs_data = json.loads(theirs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _envelope(
            {
                "status": "failed",
                "error": f"profile JSON parse error: {e}",
                "merged_profile_path": None,
            }
        )

    # The merge driver runs per-file over profile.json / archetypes.json /
    # rules.json / canonicals.json (each a different shape). Branch on the data
    # key the file actually carries instead of assuming "archetypes" — otherwise
    # a canonicals.json/rules.json conflict gets filtered to _SAFE_TOP_LEVEL_KEYS
    # (which lacks 'canonicals'/'rules'), wiping the real payload, and a
    # profile.json conflict gets its archetype_count zeroed.
    data_key = None
    for key in ("archetypes", "canonicals", "rules", "conventions"):
        if isinstance(ours_data.get(key), dict) or isinstance(theirs_data.get(key), dict):
            data_key = key
            break

    if data_key == "archetypes":
        ours_archs = ours_data.get("archetypes", {}) or {}
        theirs_archs = theirs_data.get("archetypes", {}) or {}

        merged: dict[str, dict] = dict(ours_archs)
        for name, arch in theirs_archs.items():
            if name not in merged:
                merged[name] = arch
                continue
            ours_size = (merged[name] or {}).get("cluster_size", 0)
            theirs_size = (arch or {}).get("cluster_size", 0)
            if theirs_size > ours_size:
                merged[name] = arch
            elif theirs_size == ours_size:
                ours_witness = (merged[name] or {}).get("canonical_witness", "")
                theirs_witness = (arch or {}).get("canonical_witness", "")
                if theirs_witness < ours_witness:
                    merged[name] = arch

        unioned = {**ours_data, **theirs_data}
        merged_data = {
            k: v for k, v in unioned.items() if k in _SAFE_TOP_LEVEL_KEYS or k.startswith("_")
        }
        merged_data["archetypes"] = merged
        # Keep the denormalized counts consistent with the merged set.
        merged_data["archetype_count"] = len(merged)
        merged_data["archetypes_detected"] = len(merged)
        payload_count = len(merged)
        ours_count = len(ours_archs)
        theirs_count = len(theirs_archs)
    elif data_key in ("canonicals", "rules", "conventions"):
        # Union the keyed payload (ours wins on key conflict) and preserve ALL
        # of the file's own metadata; never drop the payload key.
        ours_payload = ours_data.get(data_key) or {}
        theirs_payload = theirs_data.get(data_key) or {}
        merged_payload = {**theirs_payload, **ours_payload}
        # Carry forward the metadata of whichever side is newer (higher
        # generation) so counts/timestamps stay self-consistent.
        if theirs_data.get("generation", 0) > ours_data.get("generation", 0):
            merged_data = dict(theirs_data)
        else:
            merged_data = dict(ours_data)
        merged_data[data_key] = merged_payload
        payload_count = len(merged_payload)
        ours_count = len(ours_payload)
        theirs_count = len(theirs_payload)
    else:
        # profile.json (metadata only — no data-payload key) or an unrecognized
        # shape. Take the newer profile wholesale rather than synthesizing
        # archetypes={}/count=0; if neither side has a generation we can compare,
        # fail so the driver exits non-zero and git leaves conflict markers.
        ours_gen = ours_data.get("generation")
        theirs_gen = theirs_data.get("generation")
        if not isinstance(ours_gen, int) and not isinstance(theirs_gen, int):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        "unrecognized profile shape (no archetypes/canonicals/rules/"
                        "conventions key and no generation to compare); leaving the "
                        "conflict for manual resolution"
                    ),
                    "merged_profile_path": None,
                }
            )
        if (theirs_gen or 0) > (ours_gen or 0):
            merged_data = dict(theirs_data)
        else:
            merged_data = dict(ours_data)
        payload_count = merged_data.get("archetype_count", 0)
        ours_count = ours_data.get("archetype_count", 0)
        theirs_count = theirs_data.get("archetype_count", 0)

    ours_path.write_text(json.dumps(merged_data, indent=2, sort_keys=True), encoding="utf-8")

    return _envelope(
        {
            "status": "success",
            "merged_profile_path": str(ours_path),
            "merged_data_key": data_key or "profile",
            "merged_archetype_count": payload_count,
            "ours_archetype_count": ours_count,
            "theirs_archetype_count": theirs_count,
            "note": (
                "merged by key union; run /chameleon-refresh after accepting "
                "the merge to re-cluster from the actual merged repo state"
            ),
        }
    )


_IDIOMS_FILE_CAP = 200_000


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

    Bug 1: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.

    Bug 2 — slug-collision: the auto-generated idiom slug is
    `idiom-YYYY-MM-DD-{epoch_seconds}-{3hex}`. The 4-hex random suffix
    closes the 1-second collision window where two `/chameleon-teach`
    calls landed in the same epoch second (observed twice in dogfood).
    If the proposed slug already exists in idioms.md we retry once with
    a fresh suffix; the second collision is statistically negligible
    (4096^2 chance per second).

    Bug 7 — suspicious_input: natural-language prompt-injection
    preambles ("ignore previous instructions", "you are now in DAN
    mode", `eval(…)`, `rm -rf`, etc.) are still STORED — the trust gate
    is the defensive boundary — but the response envelope now carries
    `suspicious_input: True` plus the matched pattern so the using-
    chameleon skill can surface a UI warning.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope(
            {"status": "failed", "error": "no profile in this repo (run /chameleon-init)"}
        )

    suspicious, suspicious_pattern = _looks_suspicious(feedback)

    sanitized = _sanitize_user_input(feedback)
    if not sanitized.strip():
        return _envelope({"status": "failed", "error": "feedback is empty after sanitization"})
    if len(sanitized) > 50_000:
        return _envelope({"status": "failed", "error": "feedback exceeds 50KB cap"})

    body = _escape_markdown_section_headings(sanitized)

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    if body.lstrip().startswith("### "):
        addition = f"\n{body.rstrip()}\n"
    else:
        existing_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

        def _slug_from_rationale(text: str) -> str:
            import re as _re

            first_line = ""
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "```", ">", "*", "-")):
                    continue
                first_line = stripped
                break
            if not first_line:
                return ""
            slugged = _re.sub(r"[^a-z0-9]+", "-", first_line.lower()).strip("-")
            words = slugged.split("-")[:5]
            candidate = "-".join(w for w in words if w)
            if len(candidate) < 4 or candidate.isdigit():
                return ""
            return candidate[:40]

        def _new_slug() -> str:
            return f"idiom-{timestamp}-{int(time.time())}-{secrets.token_hex(2)}"

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
        try:
            from chameleon_mcp.safe_open import safe_read_profile_artifact

            profile_data = json.loads(
                safe_read_profile_artifact(repo_path / ".chameleon" / "profile.json")
            )
            language = profile_data.get("language", "any")
        except Exception as exc:
            import sys

            print(
                f"[chameleon] WARNING: profile.json read failed in teach_profile;"
                f" defaulting language to 'any'. Detail: {exc}",
                file=sys.stderr,
            )
            language = "any"
        addition = (
            f"\n### {slug}\nLanguage: {language}\nStatus: active (added {timestamp})\n{body}\n"
        )

    from chameleon_mcp.profile.trust import repo_data_dir as _rdd

    lock_path = _rdd(_compute_repo_id(idioms_path.parent.parent)) / ".idioms.lock"
    try:
        with acquire_advisory_lock(lock_path):
            current = (
                idioms_path.read_text(encoding="utf-8")
                if idioms_path.exists()
                else "# idioms\n\n## active\n\n## deprecated\n"
            )
            current = current.replace(
                "_(no idioms yet — run /chameleon-teach to capture team conventions)_\n\n",
                "",
                1,
            )
            if "## active" in current:
                new_content = current.replace("## active\n", f"## active\n{addition}", 1)
            else:
                new_content = current + addition
            if len(new_content.encode("utf-8")) > _IDIOMS_FILE_CAP:
                return _envelope(
                    {
                        "status": "failed",
                        "error": (
                            f"idioms.md would exceed {_IDIOMS_FILE_CAP // 1000}KB "
                            f"cumulative cap ({len(new_content.encode('utf-8'))} bytes "
                            f"after append). Move older idioms to '## deprecated', "
                            f"trim the file, or run /chameleon-refresh before "
                            f"capturing more."
                        ),
                    }
                )
            idioms_path.write_text(new_content, encoding="utf-8")
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another /chameleon-teach is in progress (PID {e.holder_pid}); retry shortly"
                ),
            }
        )

    _notify_daemon_cache_invalidation()

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

    BUG-NEW-007: don't escape inside fenced code blocks. A
    rationale that includes `# frozen_string_literal: true` inside a
    triple-backtick block must render literally. Pre-fix the escape
    produced `\\# frozen_string_literal: true` visible to the reader,
    cosmetic-broken in the canonical Ruby comment convention.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("## ") or stripped.startswith("# ") or stripped in ("##", "#"):
            out.append(f"{indent}\\{stripped}")
        else:
            out.append(line)
    return "\n".join(out)


def disable_session(repo: str, session_id: str, force: bool = False) -> dict:
    """Mark chameleon disabled for the given session_id.

    Writes an HMAC-signed `.session_disabled.<session_id>` marker under
    the per-repo plugin data dir. preflight-and-advise checks this
    marker before injecting context — when present AND validly signed,
    no <chameleon-context> content is added to Edit/Write/NotebookEdit
    operations for that session.

    Used by the /chameleon-disable slash command.

    Bug 1: `repo` now accepts either an absolute repo path or
    a 64-char repo_id hex digest. See `_resolve_repo_arg`.

    Bug 8 partial follow-up: chameleon-mcp cannot
    cryptographically authenticate the caller — MCP doesn't pass
    process identity, so any client can claim any session_id. The
    HMAC-signed marker defends against an OUT-OF-PROCESS
    attacker who writes the marker file directly without the key.
    For IN-PROCESS attackers (anything that can call this MCP tool),
    we add two defenses:

    1. REQUIRE a trust grant: disable_session fails when the repo
       has no `.trust` record — a caller has to demonstrate they can
       go through `/chameleon-trust` (which validates against the
       repo basename / yes-trust-<short8> token) before they can
       suppress chameleon. Limits the attack surface to callers who
       have already authenticated against the repo.
    2. SURFACE a `session_unknown_to_chameleon` warning in the response
       when this session_id has never invoked another chameleon tool
       (per the exec_log). The legitimate user / their review tooling
       can flag that as suspicious.

    Bug 2 follow-up: unknown sessions are now REFUSED by
    default. The reporter pointed out that the earlier warning is
    a useful audit signal but the marker is still written, so an
    attacker who learned the session_id silently suppressed chameleon
    until the legitimate user happened to disable themselves. Now the
    marker is only written when:
      - the session_id has invoked chameleon before (exec_log hit), OR
      - the caller passes ``force=True`` (explicit override for
        legitimate "first-time disable from a brand-new session" cases,
        which intentionally requires the caller to opt past the gate).
    """
    from chameleon_mcp.optouts import write_session_disable
    from chameleon_mcp.profile.trust import trust_state_for

    if not session_id or not isinstance(session_id, str):
        return _envelope({"status": "failed", "error": "session_id required"})

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )

    if trust_state_for(repo_id) is None:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "disable_session requires a trust grant for the repo. "
                    "Run /chameleon-trust first."
                ),
            }
        )

    session_unknown = _session_unseen_for_repo(repo_id, session_id)

    if session_unknown and not force:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "session_id has not invoked any other chameleon tool "
                    "for this repo (session_unknown_to_chameleon). The "
                    "marker was NOT written. If this is a legitimate "
                    "first-time disable from a brand-new session, retry "
                    "with force=True."
                ),
                "session_unknown_to_chameleon": True,
                "session_id": session_id,
            }
        )

    marker = write_session_disable(repo_id, session_id)
    envelope: dict = {
        "status": "success",
        "marker_path": str(marker),
        "session_id": session_id,
        "scope": "session",
    }
    if session_unknown:
        envelope["session_unknown_to_chameleon"] = True
        envelope["forced"] = True
        envelope["warning"] = (
            "Marker written despite the session_id being unknown to "
            "chameleon, because force=True was passed. If you did not "
            "call /chameleon-disable yourself, investigate — your "
            "session_id may have leaked."
        )
    return _envelope(envelope)


def _session_unseen_for_repo(repo_id: str, session_id: str) -> bool:
    """True if `session_id` has never appeared in the exec_log for `repo_id`.

    Best-effort: any error returns False (don't false-warn on a system
    where exec_log isn't readable).
    """
    try:
        from chameleon_mcp.exec_log import _exec_log_dir
    except Exception:
        return False
    try:
        exec_dir = _exec_log_dir(repo_id)
    except Exception:
        return False
    if not exec_dir.is_dir():
        return True
    import json as _json

    needle = f'"session_id":"{session_id}"'
    try:
        log_files = sorted(
            (p for p in exec_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
    except OSError:
        return False
    for log_file in log_files:
        try:
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    if needle in line:
                        return False
                    try:
                        rec = _json.loads(line)
                        if rec.get("session_id") == session_id:
                            return False
                    except (ValueError, _json.JSONDecodeError):
                        continue
        except OSError:
            continue
    return True


def pause_session(repo: str, minutes: int = 15) -> dict:
    """Pause chameleon advisory injections for `minutes` minutes.

    Writes a `.pause_until` file with an ISO 8601 expiry timestamp
    under the per-repo plugin data dir. preflight-and-advise auto-
    expires the marker; no manual cleanup needed.

    Used by the /chameleon-pause-15m slash command (and any future
    /chameleon-pause-<N> variants).

    Bug 1: `repo` now accepts either an absolute repo path
    or a 64-char repo_id hex digest. The asymmetry across MCP tools
    surfaced 4 separate dogfood complaints about pause/disable rejecting
    repo_ids. `_resolve_repo_arg` performs the shape detection.
    """
    from chameleon_mcp.optouts import write_pause
    from chameleon_mcp.profile.trust import trust_state_for

    if not isinstance(minutes, int) or minutes <= 0 or minutes > 240:
        return _envelope({"status": "failed", "error": "minutes must be 1..240"})

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )

    if trust_state_for(repo_id) is None:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "pause_session requires a trust grant for the repo. Run /chameleon-trust first."
                ),
            }
        )

    expiry_iso = write_pause(repo_id, minutes)
    return _envelope(
        {
            "status": "success",
            "expires_at": expiry_iso,
            "minutes": minutes,
        }
    )


def trust_profile(repo: str, confirmation_token: str) -> dict:
    """Mark a committed profile as trusted for the current user.

    Phase 2D: validates `confirmation_token` matches the repo's basename
    (typed repo name) or `yes-trust-<repo_id_short>`. Writes .trust file.

    BUG-004: ``repo`` accepts either an absolute repo path or
    a 64-char repo_id hex digest, matching the behavior of get_archetype,
    refresh_repo, propose_archetype_renames, etc. Earlier the function
    only accepted a path and rejected repo_id with "repo path must be
    absolute" even though every other tool documented repo_id as the
    canonical handle.
    """
    from chameleon_mcp.profile.trust import grant_trust

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    repo_path = resolved_path
    if not repo_path.exists():
        return _envelope({"status": "failed", "error": f"repo path does not exist: {repo!r}"})
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    profile_dir = repo_path / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope(
            {"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"}
        )
    if not (profile_dir / "profile.json").is_file():
        return _envelope(
            {
                "status": "failed",
                "error": "no profile.json in .chameleon/ (run /chameleon-init first)",
            }
        )

    from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir

    try:
        load_profile_dir(profile_dir)
    except (ProfileLoadError, json.JSONDecodeError) as exc:
        return _envelope(
            {
                "status": "failed",
                "error": f"profile is not loadable: {exc}",
            }
        )

    repo_id = _compute_repo_id(repo_path)
    expected_short = repo_id[:8]

    if confirmation_token != repo_path.name and confirmation_token != f"yes-trust-{expected_short}":
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "confirmation_token must be exactly the repo basename "
                    f"{repo_path.name!r}, or the literal string "
                    f"'yes-trust-{expected_short}' "
                    f"(yes-trust- prefix + the first 8 hex chars of repo_id "
                    f"{repo_id!r}). Substring / prefix variants are NOT accepted."
                ),
            }
        )

    record = grant_trust(repo_id, profile_dir)
    # Reflect the new trust state in the status line immediately (it reads a
    # SessionStart-written cache that /chameleon-trust did not update, so it kept
    # showing `(stale)` until the next session).
    _update_statusline_trust(repo_path, "trusted")

    workspace_trust_count = 0
    for child_chameleon in _iter_workspace_chameleon_dirs(repo_path):
        if child_chameleon == profile_dir:
            continue
        if not (child_chameleon / "profile.json").is_file():
            continue
        try:
            grant_trust(repo_id, child_chameleon)
            workspace_trust_count += 1
        except Exception:
            pass

    data: dict = {
        "status": "success",
        "trusted_at": record.granted_at,
        "granted_by_user": record.granted_by_user,
    }
    if workspace_trust_count:
        data["workspace_profiles_trusted"] = workspace_trust_count
    return _envelope(data)


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


_SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore previous instructions",
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    ),
    ("disregard above/prior", re.compile(r"disregard\s+(the\s+)?(above|prior)", re.IGNORECASE)),
    (
        "you are now <mode>",
        re.compile(r"you\s+are\s+now\s+(in\s+)?[\w\s]{0,32}mode", re.IGNORECASE),
    ),
    ("system role injection", re.compile(r"(<\s*/?\s*system\s*>|system\s*:\s*)", re.IGNORECASE)),
    ("eval()", re.compile(r"\beval\s*\(", re.IGNORECASE)),
    ("exec()", re.compile(r"\bexec\s*\(", re.IGNORECASE)),
    ("rm -rf", re.compile(r"\brm\s+-rf\b", re.IGNORECASE)),
    (
        "reveal secrets/prompt",
        re.compile(
            r"reveal\s+(the\s+)?(secret|api\s*key|prompt|system\s+prompt)",
            re.IGNORECASE,
        ),
    ),
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
        if current_slug and (s == current_slug or s.startswith(current_slug + "-")):
            return
        candidates.append(s)

    witness_rel = ""
    if canonical:
        witness_rel = (canonical.get("witness") or {}).get("path", "")
    if witness_rel:
        stem = witness_rel.rsplit("/", 1)[-1]
        stem = _re.sub(r"\.[^.]+$", "", stem)
        stem = _re.sub(r"\.[^.]+$", "", stem)
        _push(stem)

    paths_pattern = archetype.get("paths_pattern", "")
    if paths_pattern:
        segments = [s for s in paths_pattern.split("/") if s]
        for seg in reversed(segments):
            if _re.fullmatch(r"v\d+(?:\.\d+)*", seg):
                continue
            _push(seg)
            break

    if witness_rel:
        dirs = witness_rel.rsplit("/", 1)[0].split("/")
        if len(dirs) > 1:
            for seg in reversed(dirs[1:]):
                if _re.fullmatch(r"v\d+(?:\.\d+)*", seg):
                    continue
                _push(seg)
                break

    kinds = archetype.get("top_level_node_kinds") or []
    if kinds:
        friendly = _NODE_KIND_TO_NAME.get(kinds[0])
        if friendly:
            _push(friendly)

    if archetype.get("jsx_present"):
        _push("react-component")

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

    `top_n` must be an integer in 1..64 (inclusive). Values outside that
    range return `{status: failed, error: "top_n must be an int in
    1..64"}`. Default is 8, which is what the chameleon-init skill uses
    for its interview prompts.

    Bug 1: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp.profile.loader import load_profile_dir

    if not isinstance(top_n, int) or top_n <= 0 or top_n > 64:
        return _envelope({"status": "failed", "error": "top_n must be an int in 1..64"})

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    if not resolved_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
    repo_root = resolved_path.resolve()

    profile_dir = repo_root / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope(
            {"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"}
        )

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
        rows.append(
            {
                "current_name": name,
                "cluster_size": int((arch or {}).get("cluster_size", 0)),
                "canonical_file": canonical_path,
                "paths_pattern": (arch or {}).get("paths_pattern", ""),
                "suggested_alternatives": alternatives,
            }
        )

    return _envelope(
        {
            "status": "success",
            "repo_id": _compute_repo_id(repo_root),
            "archetypes": rows,
            "total_archetypes": len(archetypes),
        }
    )


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
    # Sources actually being renamed AWAY (excluding self-renames). A
    # self-rename ({"x": "x"}) leaves "x" occupied, so it must NOT count as
    # freeing the name "x" for another rename to land on — otherwise one
    # archetype silently overwrites the other.
    renamed_away = {
        k for k, v in renames.items() if isinstance(k, str) and isinstance(v, str) and k != v
    }
    for old, new in renames.items():
        if not isinstance(old, str) or not isinstance(new, str):
            return {}, f"rename keys/values must be strings (got {old!r} → {new!r})"
        if old not in existing_names:
            return {}, f"unknown archetype {old!r} (not in committed profile)"
        if not ARCHETYPE_NAME_RE.match(new):
            return {}, (f"target name {new!r} must match {ARCHETYPE_NAME_RE.pattern}")
        if old == new:
            continue
        if new in seen_targets:
            return {}, f"two renames collide on target {new!r}"
        seen_targets.add(new)
        if new in existing_names and new not in renamed_away:
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

    Delegates to the shared renderer in ``chameleon_mcp.profile.summary``.
    """
    from chameleon_mcp.profile.summary import render_summary_md

    return render_summary_md(
        archetypes=archetypes_data,
        canonicals=canonicals_data,
        profile_meta=profile_data,
        idioms_text=idioms_text,
        rules_data=rules_data,
    )


class _RenamesOverlayOverCap(Exception):
    """Raised by _read_renames_overlay_strict when the overlay exceeds the cap.

    apply_archetype_renames catches this to refuse the operation rather
    than silently wiping a teammate's larger committed overlay. The
    tolerant `_read_renames_overlay` (bootstrap path) still returns {}
    on over-cap so the read does not crash bootstrap.
    """


def _read_renames_overlay(profile_dir: Path) -> dict[str, str]:
    """Return the current `.chameleon/renames.json` mapping, or {}.

    Tolerant of missing / malformed / over-cap files — bootstrap-time
    callers want fail-open. apply_archetype_renames must NOT call this
    helper directly; it uses `_read_renames_overlay_strict` so a corrupt
    or over-cap overlay refuses the rename instead of silently wiping it.

    Security: read goes through safe_read_profile_artifact (O_NOFOLLOW +
    5 MB cap). Values are validated against ARCHETYPE_NAME_RE so a teammate
    cannot inject prompt-tokens via a committed rename target. Entry count
    is capped at RENAMES_OVERLAY_CAP (default 256). Empty string keys are
    also dropped to match orchestrator._load_user_renames.
    """
    try:
        return _read_renames_overlay_strict(profile_dir)
    except _RenamesOverlayOverCap:
        return {}


def _read_renames_overlay_strict(profile_dir: Path) -> dict[str, str]:
    """Like `_read_renames_overlay` but raises on over-cap.

    Used by apply_archetype_renames to detect "would-wipe" situations
    before merging incoming renames into an empty dict.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE
    from chameleon_mcp.safe_open import (
        UnsafeFileError,
        safe_read_profile_artifact,
    )

    path = profile_dir / "renames.json"
    try:
        text = safe_read_profile_artifact(path)
    except FileNotFoundError:
        return {}
    except (OSError, UnsafeFileError):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    sv = data.get("schema_version")
    if not isinstance(sv, int) or sv > 1:
        return {}
    raw = data.get("renames", {})
    if not isinstance(raw, dict):
        return {}
    if len(raw) > threshold_int("RENAMES_OVERLAY_CAP"):
        raise _RenamesOverlayOverCap(
            f"renames.json has {len(raw)} entries, exceeds cap "
            f"{threshold_int('RENAMES_OVERLAY_CAP')}"
        )
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and isinstance(v, str) and k):
            continue
        if not ARCHETYPE_NAME_RE.match(v):
            continue
        out[k] = v
    return out


_ARCHETYPE_RENAMES_LEDGER_FILENAME = ".archetype_renames.json"


def _append_rename_ledger_entries(
    profile_dir: Path,
    effective: dict[str, str],
) -> dict | None:
    """Build the next ledger payload by appending ``effective`` to the
    on-disk history. Returns None when there's nothing to append.

    Rec 11b: each call to ``apply_archetype_renames`` appends an entry
    per effective rename so the audit trail records who changed what
    when. FIFO-pruned at RENAMES_OVERLAY_CAP entries (default 256)
    using the same threshold as the overlay so an automated tool can't
    balloon the ledger and blow the trust-check memory ceiling.

    The ledger lives at ``.chameleon/.archetype_renames.json`` and is
    in ``_HASHED_ARTIFACTS`` so a teammate cannot smuggle a rename in
    silently — modifying the ledger trips the material-change re-prompt.

    Reads use ``safe_read_profile_artifact`` (O_NOFOLLOW + size cap),
    validates entries through ``ARCHETYPE_NAME_RE`` (so a hand-edited
    ledger with prompt-injection text in a name doesn't poison the
    next refresh).
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE
    from chameleon_mcp.safe_open import (
        UnsafeFileError,
        safe_read_profile_artifact,
    )

    if not effective:
        return None

    ledger_path = profile_dir / _ARCHETYPE_RENAMES_LEDGER_FILENAME
    history: list[dict] = []
    if ledger_path.is_file():
        try:
            text = safe_read_profile_artifact(ledger_path)
            existing = json.loads(text)
            if isinstance(existing, dict):
                raw_history = existing.get("history")
                if isinstance(raw_history, list):
                    for entry in raw_history:
                        if not isinstance(entry, dict):
                            continue
                        old = entry.get("from")
                        new = entry.get("to")
                        if not (isinstance(old, str) and isinstance(new, str)):
                            continue
                        if not (ARCHETYPE_NAME_RE.match(old) and ARCHETYPE_NAME_RE.match(new)):
                            continue
                        history.append(
                            {
                                "from": old,
                                "to": new,
                                "ts": entry.get("ts") if isinstance(entry.get("ts"), str) else "",
                            }
                        )
        except (FileNotFoundError, OSError, UnsafeFileError, json.JSONDecodeError):
            history = []

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for old, new in sorted(effective.items()):
        if not (ARCHETYPE_NAME_RE.match(old) and ARCHETYPE_NAME_RE.match(new)):
            continue
        history.append({"from": old, "to": new, "ts": now_iso})

    cap = threshold_int("RENAMES_OVERLAY_CAP")
    if len(history) > cap:
        history = history[-cap:]

    return {
        "schema_version": 1,
        "history": history,
        "updated_at": now_iso,
    }


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
    - conventions.json: rename the per-archetype keys under each section
    - profile.summary.md: regenerate from the renamed data
    - principles.md: preserved verbatim (not archetype-keyed)

    Uses atomic_profile_commit so a crash mid-write leaves the previous
    profile untouched. Returns status, renames_applied, new_profile_sha256.

    Idempotent on no-ops: an empty mapping (`{}`) or a mapping where every
    entry is a self-rename (`{"x": "x"}`) returns
    `{status: success, renames_applied: 0, new_profile_sha256: <unchanged>,
    note: "no effective renames (all no-ops or empty mapping)"}` WITHOUT
    rewriting any profile files. The returned sha matches the existing
    profile.json byte-for-byte, so trust grants remain valid across
    successive no-op calls.

    Bug 1: `repo` accepts either an absolute repo path or a
    64-char repo_id hex digest. See `_resolve_repo_arg`.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.transaction import atomic_profile_commit
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.profile.trust import hash_profile

    resolved_path, _resolved_id = _resolve_repo_arg(repo)
    if resolved_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    if not resolved_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
    repo_root = resolved_path.resolve()

    profile_dir = repo_root / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope(
            {"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"}
        )

    try:
        loaded = load_profile_dir(profile_dir)
    except Exception as e:  # pragma: no cover - defensive
        return _envelope({"status": "failed", "error": f"profile load failed: {e}"})

    existing = set(loaded.archetypes.get("archetypes", {}).keys())
    effective, err = _validate_renames(renames, existing)
    if err is not None:
        return _envelope({"status": "failed", "error": err})

    if not effective:
        return _envelope(
            {
                "status": "success",
                "renames_applied": 0,
                "new_profile_sha256": hash_profile(profile_dir),
                "note": "no effective renames (all no-ops or empty mapping)",
            }
        )

    archetypes_data = json.loads(json.dumps(loaded.archetypes))
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

    new_rules_map: dict = {}
    for k, v in rules_map.items():
        new_rules_map[effective.get(k, k)] = v
    rules_data["rules"] = new_rules_map

    # Preserve + rename conventions.json (its sub-sections are keyed per
    # archetype) and preserve principles.md. atomic_profile_commit replaces the
    # whole .chameleon dir and does NOT copy protocol files, so any artifact not
    # written into txn_dir below is LOST — previously rename silently dropped
    # both of these.
    conventions_path = profile_dir / "conventions.json"
    conventions_data = (
        json.loads(json.dumps(loaded.conventions)) if conventions_path.is_file() else None
    )
    if isinstance(conventions_data, dict):
        _conv_block = conventions_data.get("conventions")
        if isinstance(_conv_block, dict):
            for _section in ("imports", "naming", "inheritance", "method_calls", "key_exports"):
                _sub = _conv_block.get(_section)
                if isinstance(_sub, dict):
                    _conv_block[_section] = {effective.get(k, k): v for k, v in _sub.items()}

    principles_path = profile_dir / "principles.md"
    principles_text = (
        principles_path.read_text(encoding="utf-8") if principles_path.is_file() else None
    )

    idioms_path = profile_dir / "idioms.md"
    idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

    summary_md = _rewrite_summary_md(
        profile_data,
        archetypes_data,
        canonicals_data,
        idioms_text,
        rules_data=rules_data,
    )

    try:
        existing_renames = _read_renames_overlay_strict(profile_dir)
    except _RenamesOverlayOverCap as exc:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"refusing to rename: {exc}. The on-disk overlay is too "
                    "large to safely merge — review .chameleon/renames.json "
                    "and remove stale entries (or raise CHAMELEON_RENAMES_OVERLAY_CAP) "
                    "before re-running /chameleon-rename."
                ),
            }
        )
    merged_renames = _merge_rename_overlay(existing_renames, effective)
    renames_payload = {
        "schema_version": 1,
        "renames": merged_renames,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger_payload = _append_rename_ledger_entries(profile_dir, effective)

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
            if conventions_data is not None:
                (txn_dir / "conventions.json").write_text(
                    json.dumps(conventions_data, indent=2, sort_keys=True), encoding="utf-8"
                )
            if principles_text is not None:
                (txn_dir / "principles.md").write_text(principles_text, encoding="utf-8")
            (txn_dir / "idioms.md").write_text(idioms_text, encoding="utf-8")
            (txn_dir / "profile.summary.md").write_text(summary_md, encoding="utf-8")
            (txn_dir / "renames.json").write_text(
                json.dumps(renames_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            if ledger_payload is not None:
                (txn_dir / ".archetype_renames.json").write_text(
                    json.dumps(ledger_payload, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"atomic commit failed: {e}"})

    repo_id = _compute_repo_id(repo_root)
    new_hash = hash_profile(profile_dir)
    try:
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

    return _envelope(
        {
            "status": "success",
            "renames_applied": len(effective),
            "new_profile_sha256": new_hash,
            "renames": effective,
        }
    )


_SLUG_RE = __import__("re").compile(r"^[a-z][a-z0-9-]{2,63}$")
_STRUCTURED_TOTAL_CAP = 50_000


def teach_competing_import(
    repo: str,
    *,
    archetype: str,
    preferred: str,
    over: str,
) -> dict:
    """Capture a wrapper-preference ("use ``preferred``, not ``over``") for an
    archetype, written to ``conventions.imports.<archetype>.competing``.

    This enables the banned-raw-import / mandatory-wrapper convention and its
    principle, which AST analysis cannot infer (a team rule like "import the
    project's ``http`` wrapper, not raw ``axios``"). Mirrors ``teach_profile``'s
    in-place, flock-serialized single-file write — it does not touch any other
    profile artifact, so it can't drop conventions/principles the way a full
    atomic_profile_commit that omits protocol files would.
    """
    from chameleon_mcp.conventions import empty_conventions
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock
    from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE
    from chameleon_mcp.profile.trust import repo_data_dir as _rdd
    from chameleon_mcp.safe_open import safe_read_profile_artifact

    if not isinstance(archetype, str) or not ARCHETYPE_NAME_RE.match(archetype):
        return _envelope(
            {
                "status": "failed",
                "error": f"archetype {archetype!r} must match {ARCHETYPE_NAME_RE.pattern!r}",
            }
        )
    preferred = (preferred or "").strip() if isinstance(preferred, str) else ""
    over = (over or "").strip() if isinstance(over, str) else ""
    if not preferred or not over:
        return _envelope(
            {
                "status": "failed",
                "error": "both 'preferred' and 'over' are required and must be non-empty",
            }
        )
    if len(preferred) > 200 or len(over) > 200:
        return _envelope(
            {"status": "failed", "error": "'preferred'/'over' exceed the 200-char cap"}
        )
    if preferred == over:
        return _envelope({"status": "failed", "error": "'preferred' and 'over' must differ"})

    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    profile_dir = repo_path / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope(
            {"status": "failed", "error": "no profile in this repo (run /chameleon-init)"}
        )
    conv_path = profile_dir / "conventions.json"

    lock_path = _rdd(_compute_repo_id(repo_path)) / ".conventions.lock"
    try:
        with acquire_advisory_lock(lock_path):
            try:
                conv = (
                    json.loads(safe_read_profile_artifact(conv_path))
                    if conv_path.is_file()
                    else empty_conventions(generation=0)
                )
            except Exception:
                conv = empty_conventions(generation=0)
            if not isinstance(conv, dict):
                conv = empty_conventions(generation=0)

            block = conv.setdefault("conventions", {})
            if not isinstance(block, dict):
                conv["conventions"] = block = {}
            imports = block.setdefault("imports", {})
            if not isinstance(imports, dict):
                block["imports"] = imports = {}
            entry = imports.setdefault(archetype, {"preferred": [], "competing": []})
            if not isinstance(entry, dict):
                imports[archetype] = entry = {"preferred": [], "competing": []}
            entry.setdefault("preferred", [])
            competing = entry.setdefault("competing", [])
            if not isinstance(competing, list):
                entry["competing"] = competing = []

            already = any(
                isinstance(c, dict) and c.get("preferred") == preferred and c.get("over") == over
                for c in competing
            )
            if not already:
                competing.append({"preferred": preferred, "over": over})

            # Atomic write: conventions.json is JSON-parsed on load and a
            # truncated file raises ProfileLoadError (bricks the whole profile),
            # so write to a tmp + os.replace rather than truncating in place.
            _tmp = conv_path.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps(conv, indent=2, sort_keys=True), encoding="utf-8")
            _tmp.replace(conv_path)
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": f"another conventions write is in progress: {e}",
            }
        )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"conventions write failed: {e}"})

    return _envelope(
        {
            "status": "ok",
            "archetype": archetype,
            "competing": {"preferred": preferred, "over": over},
            "already_present": already,
            "note": (
                "wrapper-preference recorded in conventions.json; the profile hash "
                "changed, so run /chameleon-trust if it shows as stale."
            ),
        }
    )


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
        return _envelope(
            {
                "status": "failed",
                "error": f"slug {slug!r} must match {_SLUG_RE.pattern!r}",
            }
        )
    if not isinstance(rationale, str) or not rationale.strip():
        return _envelope({"status": "failed", "error": "rationale is required"})
    if status not in ("active", "deprecated"):
        return _envelope(
            {
                "status": "failed",
                "error": "status must be 'active' or 'deprecated'",
            }
        )
    if archetype is not None and not ARCHETYPE_NAME_RE.match(str(archetype)):
        return _envelope(
            {
                "status": "failed",
                "error": (f"archetype {archetype!r} must match {ARCHETYPE_NAME_RE.pattern!r}"),
            }
        )

    total = len(rationale) + len(example or "") + len(counterexample or "")
    if total > _STRUCTURED_TOTAL_CAP:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"rationale + example + counterexample size {total} exceeds "
                    f"50KB cap ({_STRUCTURED_TOTAL_CAP})"
                ),
            }
        )

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

    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None:
        return _envelope(
            {
                "status": "failed",
                "error": "expected absolute repo path or 64-char repo_id hex digest",
            }
        )
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})
    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope(
            {"status": "failed", "error": "no profile in this repo (run /chameleon-init)"}
        )

    try:
        from chameleon_mcp.safe_open import safe_read_profile_artifact

        profile_data = json.loads(
            safe_read_profile_artifact(repo_path / ".chameleon" / "profile.json")
        )
        language = profile_data.get("language", "any")
    except Exception as exc:
        import sys

        print(
            f"[chameleon] WARNING: profile.json read failed in teach_profile_structured;"
            f" defaulting language to 'any'. Detail: {exc}",
            file=sys.stderr,
        )
        language = "any"
    rendered = rendered.replace(f"### {slug}\n", f"### {slug}\nLanguage: {language}\n", 1)

    sections = _find_all_slug_sections(idioms_path, slug)
    in_active = "active" in sections
    in_deprecated = "deprecated" in sections

    if in_deprecated:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"slug {slug!r} already exists in '## deprecated'. Pick a "
                    "new slug or edit idioms.md directly to reactivate."
                ),
            }
        )
    if in_active and status == "active":
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"slug {slug!r} already exists in '## active'. To "
                    'deprecate it, pass status="deprecated"; to update its '
                    "body, edit idioms.md directly or pick a new slug."
                ),
            }
        )
    if in_active and status == "deprecated":
        return _transition_slug_to_deprecated(
            idioms_path,
            slug,
            archetype=archetype,
            rationale=rationale.strip(),
            timestamp=timestamp,
            example=example,
            counterexample=counterexample,
        )

    if status == "active":
        return teach_profile(repo, rendered)
    return _write_new_deprecated_idiom(
        idioms_path,
        slug,
        archetype=archetype,
        rationale=rationale.strip(),
        timestamp=timestamp,
        example=example,
        counterexample=counterexample,
    )


def _find_all_slug_sections(idioms_path: Path, slug: str) -> frozenset[str]:
    """Return the set of sections (`"active"`, `"deprecated"`) where
    `### {slug}` appears in idioms.md. Empty when the slug isn't present
    or the file doesn't exist. Captures the corrupted-state case (slug
    in BOTH sections) so the wrapper can surface it explicitly rather
    than silently adding a third entry.
    """
    if not idioms_path.is_file():
        return frozenset()
    try:
        text = idioms_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    header = f"### {slug}"
    found: set[str] = set()
    section: str = "active"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## active":
            section = "active"
            continue
        if stripped == "## deprecated":
            section = "deprecated"
            continue
        if stripped == header:
            found.add(section)
    return frozenset(found)


def _render_idiom_block(
    slug: str,
    *,
    status: str,
    archetype: str | None,
    rationale: str,
    timestamp: str,
    example: str | None,
    counterexample: str | None,
) -> str:
    """Render one idioms.md block. Used by both the transition path and
    the direct-deprecated writer. Sanitization is delegated to the
    caller before this runs because each entry point sanitizes the raw
    inputs differently (some via teach_profile's per-feedback pipeline,
    some via the local _sanitize_user_input helper)."""
    status_line = (
        f"Status: active (added {timestamp})"
        if status == "active"
        else f"Status: deprecated {timestamp}"
    )
    lines: list[str] = [f"### {slug}", status_line]
    if archetype:
        lines.append(f"Archetype: {archetype}")
    lines.append(rationale)
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
    return "\n".join(lines) + "\n"


def _sanitize_idiom_inputs(
    rationale: str,
    example: str | None,
    counterexample: str | None,
) -> tuple[str, str | None, str | None]:
    """Run rationale / example / counterexample through
    _sanitize_user_input + _escape_markdown_section_headings so the
    transition + direct-deprecated paths match teach_profile's
    sanitization. Returns the sanitized triple; empty strings normalize
    to None for example/counterexample so the renderer can skip them."""
    san_rationale = _escape_markdown_section_headings(_sanitize_user_input(rationale))
    san_example: str | None = None
    if example is not None:
        cleaned = _escape_markdown_section_headings(_sanitize_user_input(example))
        san_example = cleaned if cleaned.strip() else None
    san_counter: str | None = None
    if counterexample is not None:
        cleaned = _escape_markdown_section_headings(_sanitize_user_input(counterexample))
        san_counter = cleaned if cleaned.strip() else None
    return san_rationale, san_example, san_counter


def _transition_slug_to_deprecated(
    idioms_path: Path,
    slug: str,
    *,
    archetype: str | None,
    rationale: str,
    timestamp: str,
    example: str | None,
    counterexample: str | None,
) -> dict:
    """Move an existing `### {slug}` block from `## active` to
    `## deprecated`, replacing its status line with
    `Status: deprecated {timestamp}` and overwriting its body with the
    new rationale / example / counterexample / archetype. Sanitizes the
    rationale / example / counterexample inputs the same way
    teach_profile does (NFC + ANSI + zero-width strip + heading
    escape), and respects the 200KB _IDIOMS_FILE_CAP cumulative cap.
    Acquires the same advisory lock teach_profile uses.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    san_rationale, san_example, san_counter = _sanitize_idiom_inputs(
        rationale, example, counterexample
    )
    new_block = _render_idiom_block(
        slug,
        status="deprecated",
        archetype=archetype,
        rationale=san_rationale,
        timestamp=timestamp,
        example=san_example,
        counterexample=san_counter,
    )

    from chameleon_mcp.profile.trust import repo_data_dir as _rdd

    lock_path = _rdd(_compute_repo_id(idioms_path.parent.parent)) / ".idioms.lock"
    try:
        with acquire_advisory_lock(lock_path):
            text = idioms_path.read_text(encoding="utf-8")
            header = f"### {slug}"
            lines = text.splitlines(keepends=True)
            active_body: list[str] = []
            deprecated_body: list[str] = []
            preamble: list[str] = []
            phase = "preamble"
            i = 0
            removed = False
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                if stripped == "## active":
                    phase = "active"
                    i += 1
                    continue
                if stripped == "## deprecated":
                    phase = "deprecated"
                    i += 1
                    continue
                if phase == "preamble":
                    preamble.append(line)
                    i += 1
                    continue
                if phase == "active" and stripped == header and not removed:
                    removed = True
                    i += 1
                    while i < len(lines):
                        nxt = lines[i].strip()
                        if nxt.startswith("### ") or nxt.startswith("## "):
                            break
                        i += 1
                    continue
                if phase == "active":
                    active_body.append(line)
                else:
                    deprecated_body.append(line)
                i += 1

            if not removed:
                return _envelope(
                    {
                        "status": "failed",
                        "error": (
                            f"slug {slug!r} no longer present in '## active' "
                            "(concurrent write?); retry"
                        ),
                    }
                )

            rebuilt: list[str] = []
            rebuilt.extend(preamble)
            rebuilt.append("## active\n")
            rebuilt.extend(active_body)
            rebuilt.append("## deprecated\n")
            rebuilt.append(new_block)
            rebuilt.extend(deprecated_body)
            new_content = "".join(rebuilt)
            if len(new_content.encode("utf-8")) > _IDIOMS_FILE_CAP:
                return _envelope(
                    {
                        "status": "failed",
                        "error": (
                            f"idioms.md would exceed {_IDIOMS_FILE_CAP // 1000}KB "
                            f"cumulative cap ({len(new_content.encode('utf-8'))} "
                            "bytes after transition). Trim the file or move older "
                            "entries before transitioning more slugs."
                        ),
                    }
                )
            idioms_path.write_text(new_content, encoding="utf-8")
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another /chameleon-teach is in progress (PID {e.holder_pid}); retry shortly"
                ),
            }
        )

    _notify_daemon_cache_invalidation()
    return _envelope(
        {
            "status": "success",
            "idioms_added": 0,
            "idioms_deprecated": 1,
            "slug": slug,
            "note": f"moved '### {slug}' from '## active' to '## deprecated'",
        }
    )


def _write_new_deprecated_idiom(
    idioms_path: Path,
    slug: str,
    *,
    archetype: str | None,
    rationale: str,
    timestamp: str,
    example: str | None,
    counterexample: str | None,
) -> dict:
    """Append a brand-new `### {slug}` block directly under `##
    deprecated`. Used when teach_profile_structured is called with
    status='deprecated' on a slug that doesn't yet exist in either
    section. Sanitizes inputs and respects the cumulative cap, same as
    the transition path.
    """
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    san_rationale, san_example, san_counter = _sanitize_idiom_inputs(
        rationale, example, counterexample
    )
    new_block = _render_idiom_block(
        slug,
        status="deprecated",
        archetype=archetype,
        rationale=san_rationale,
        timestamp=timestamp,
        example=san_example,
        counterexample=san_counter,
    )

    from chameleon_mcp.profile.trust import repo_data_dir as _rdd

    lock_path = _rdd(_compute_repo_id(idioms_path.parent.parent)) / ".idioms.lock"
    try:
        with acquire_advisory_lock(lock_path):
            if idioms_path.is_file():
                current = idioms_path.read_text(encoding="utf-8")
            else:
                current = "# idioms\n\n## active\n\n## deprecated\n"
            current = current.replace(
                "_(no idioms yet — run /chameleon-teach to capture team conventions)_\n\n",
                "",
                1,
            )
            if "## deprecated" in current:
                new_content = current.replace(
                    "## deprecated\n",
                    f"## deprecated\n{new_block}",
                    1,
                )
            else:
                if not current.endswith("\n"):
                    current += "\n"
                new_content = current + "\n## deprecated\n" + new_block
            if len(new_content.encode("utf-8")) > _IDIOMS_FILE_CAP:
                return _envelope(
                    {
                        "status": "failed",
                        "error": (
                            f"idioms.md would exceed {_IDIOMS_FILE_CAP // 1000}KB "
                            f"cumulative cap ({len(new_content.encode('utf-8'))} "
                            "bytes after append). Trim the file or move older "
                            "entries before capturing more."
                        ),
                    }
                )
            idioms_path.write_text(new_content, encoding="utf-8")
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another /chameleon-teach is in progress (PID {e.holder_pid}); retry shortly"
                ),
            }
        )

    _notify_daemon_cache_invalidation()
    return _envelope(
        {
            "status": "success",
            "idioms_added": 0,
            "idioms_deprecated": 1,
            "slug": slug,
            "note": f"appended '### {slug}' directly under '## deprecated'",
        }
    )


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
        pong = _daemon_client.call("ping", {}, timeout=0.5)
        if isinstance(pong, dict):
            raw = pong.get("last_request_at", pong.get("ts"))
            if raw is not None:
                try:
                    last_request_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(raw)))
                except (TypeError, ValueError):
                    last_request_at = None

    # Prefer the in-package __version__ (the bump-synced source of truth) so the
    # reported running version matches the actual code even in an editable/source
    # checkout, where importlib.metadata can return a stale/absent value. Mirrors
    # daemon.py's running-version detection.
    try:
        from chameleon_mcp import __version__ as running_version
    except Exception:  # pragma: no cover - defensive
        try:
            from importlib.metadata import version as _pkg_version

            running_version = _pkg_version("chameleon-mcp")
        except Exception:
            running_version = None

    return _envelope(
        {
            "alive": bool(info.get("alive")),
            "pid": info.get("pid"),
            "socket": info.get("socket"),
            "uptime_s": info.get("uptime_s"),
            "last_request_at": last_request_at,
            "running_version": running_version,
        }
    )


def _chameleon_version_or_unknown() -> str:
    try:
        from importlib.metadata import version

        return version("chameleon-mcp")
    except Exception:
        pass
    try:
        from chameleon_mcp import __version__

        return __version__
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

    py = sys.version_info
    if py >= (3, 11):
        checks.append(
            {
                "name": "python_version",
                "status": "ok",
                "detail": f"{py.major}.{py.minor}.{py.micro}",
            }
        )
    else:
        checks.append(
            {
                "name": "python_version",
                "status": "error",
                "detail": f"{py.major}.{py.minor}.{py.micro} (need >= 3.11)",
            }
        )

    bash_path = shutil.which("bash")
    if bash_path:
        checks.append({"name": "bash_on_path", "status": "ok", "detail": bash_path})
    else:
        checks.append(
            {
                "name": "bash_on_path",
                "status": "error",
                "detail": "bash not on PATH; hooks will not run",
            }
        )

    # The hooks resolve `timeout || gtimeout` and degrade to uncapped python
    # when neither exists, so a missing binary is no longer fatal — but it does
    # remove the external wall-clock cap, so report it (matching the wrapper's
    # resolution order). gtimeout ships with Homebrew coreutils on macOS.
    timeout_path = shutil.which("timeout") or shutil.which("gtimeout")
    if timeout_path:
        checks.append({"name": "timeout_on_path", "status": "ok", "detail": timeout_path})
    else:
        checks.append(
            {
                "name": "timeout_on_path",
                "status": "warn",
                "detail": (
                    "neither timeout(1) nor gtimeout on PATH; hooks still run but "
                    "without an external wall-clock cap (in-process timeouts apply). "
                    "macOS: brew install coreutils"
                ),
            }
        )

    try:
        from chameleon_mcp.profile.trust import plugin_data_dir

        data_dir = plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"name": "plugin_data_writable", "status": "ok", "detail": str(data_dir)})
    except Exception as exc:
        checks.append(
            {
                "name": "plugin_data_writable",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )

    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root_env:
        plugin_root = Path(plugin_root_env)
        hook_dir = plugin_root / "hooks"
        for hook_name in (
            "preflight-and-advise",
            "posttool-recorder",
            "posttool-verify",
            "session-start",
            "callout-detector",
        ):
            hpath = hook_dir / hook_name
            if hpath.is_file() and os.access(hpath, os.X_OK):
                checks.append({"name": f"hook_{hook_name}", "status": "ok", "detail": "executable"})
            elif hpath.is_file():
                checks.append(
                    {
                        "name": f"hook_{hook_name}",
                        "status": "error",
                        "detail": "exists but not executable",
                    }
                )
            else:
                checks.append({"name": f"hook_{hook_name}", "status": "error", "detail": "missing"})
    else:
        checks.append(
            {
                "name": "hooks",
                "status": "warn",
                "detail": "CLAUDE_PLUGIN_ROOT not set; cannot locate hook scripts",
            }
        )

    try:
        from chameleon_mcp.exec_log import _ensure_hmac_key

        _ensure_hmac_key()
        checks.append({"name": "hmac_key", "status": "ok", "detail": "exists and owner-readable"})
    except Exception as exc:
        checks.append(
            {"name": "hmac_key", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    try:
        ds = daemon_status()
        if ds.get("data", {}).get("alive"):
            checks.append(
                {"name": "daemon", "status": "ok", "detail": f"alive (pid={ds['data'].get('pid')})"}
            )
        else:
            checks.append(
                {"name": "daemon", "status": "ok", "detail": "lazy (will spawn on next hook)"}
            )
    except Exception as exc:
        checks.append(
            {"name": "daemon", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    log_env = os.environ.get("CHAMELEON_HOOK_ERROR_LOG")
    if log_env:
        log = Path(log_env)
    else:
        log = Path.home() / ".local" / "share" / "chameleon" / ".hook_errors.log"
    if log.is_file():
        try:
            import re as _re
            from datetime import datetime as _dt
            from datetime import timedelta as _td
            from datetime import timezone as _tz

            try:
                from datetime import UTC as _UTC  # type: ignore[attr-defined]
            except ImportError:  # pragma: no cover - Python <3.11
                _UTC = _tz.utc  # type: ignore[assignment]  # noqa: UP017

            cutoff = _dt.now(_UTC) - _td(hours=72)
            ts_re = _re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")
            recent: list[str] = []
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                m = ts_re.match(line)
                if not m:
                    if recent:
                        recent.append(line)
                    continue
                try:
                    when = _dt.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_UTC)
                except ValueError:
                    if recent:
                        recent.append(line)
                    continue
                if when >= cutoff:
                    recent.append(line)
            tail = recent[-5:]
            if tail:
                checks.append({"name": "recent_hook_errors", "status": "warn", "detail": tail})
            else:
                checks.append(
                    {
                        "name": "recent_hook_errors",
                        "status": "ok",
                        "detail": "no errors in the last 72h",
                    }
                )
        except Exception as exc:
            checks.append(
                {
                    "name": "recent_hook_errors",
                    "status": "warn",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        checks.append({"name": "recent_hook_errors", "status": "ok", "detail": "no errors logged"})

    cwd_config = Path.cwd() / ".chameleon" / "config.json"
    if cwd_config.is_file():
        try:
            from chameleon_mcp.profile.config import (
                ChameleonConfigError,
                load_config,
            )

            cfg = load_config(cwd_config.parent)
            detail = {
                "schema_version": cfg.schema_version,
                "canonical_ref": cfg.canonical_ref,
                "branch_pinning_enabled": cfg.branch_pinning_enabled,
                "auto_refresh.enabled": cfg.auto_refresh.enabled,
                "auto_refresh.drift_threshold": cfg.auto_refresh.drift_threshold,
                "auto_refresh.max_age_hours": cfg.auto_refresh.max_age_hours,
                "trust.auto_preserve_when": cfg.trust.auto_preserve_when,
                "auto_rename": cfg.auto_rename,
            }
            checks.append({"name": "config_json", "status": "ok", "detail": detail})
        except ChameleonConfigError as cfg_exc:
            checks.append(
                {
                    "name": "config_json",
                    "status": "error",
                    "detail": (
                        f"{cwd_config} is present but malformed: "
                        f"{type(cfg_exc).__name__}: {cfg_exc}. config.json "
                        "features are inactive until fixed (built-in defaults apply)."
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "config_json",
                    "status": "warn",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        checks.append(
            {
                "name": "config_json",
                "status": "ok",
                "detail": (
                    "no .chameleon/config.json — using defaults. ON by default: "
                    "auto_refresh (drift_threshold=0.2, max_age_hours=168), "
                    "auto_rename, and trust.auto_preserve_when=always (a refresh "
                    "auto-re-grants trust, no re-prompt). OFF by default: "
                    "canonical_ref (branch pinning). Add a config.json to change "
                    'these, e.g. {"trust": {"auto_preserve_when": null}} to be '
                    "re-prompted for trust on each material refresh."
                ),
            }
        )

    try:
        from chameleon_mcp.bootstrap.transaction import is_committed

        lp = list_profiles(limit=20)
        profiles = lp.get("data", {}).get("profiles", [])
        repo_states = []
        for r in profiles:
            root = r.get("repo_root")
            if root and is_committed(Path(root) / ".chameleon"):
                status = "profile_present"
            elif root:
                status = "no_profile"
            else:
                status = "unknown"
            repo_states.append(
                {
                    "repo_root": root,
                    "profile_status": status,
                    "trust_state": r.get("trust_state"),
                }
            )
        checks.append({"name": "known_repos", "status": "ok", "detail": repo_states})
    except Exception as exc:
        checks.append(
            {"name": "known_repos", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    error_count = sum(1 for c in checks if c["status"] == "error")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    if error_count:
        overall = "error"
    elif warn_count:
        overall = "warn"
    else:
        overall = "ok"

    return _envelope(
        {
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
        }
    )
