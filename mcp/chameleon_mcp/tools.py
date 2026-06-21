"""MCP tool implementations for chameleon.

Each registered MCP tool is fully implemented and returns the standard API
versioning envelope:
{ "api_version": "1", "data": {...}, "truncated"?: bool, "next_cursor"?: str }
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chameleon_mcp.function_catalog import ParsedFn
    from chameleon_mcp.profile.loader import LoadedProfile


# Repo-identity derivation lives in chameleon_mcp.repo_id; the names are
# re-exported here so existing imports and test patches that reference
# chameleon_mcp.tools.<name> keep working unchanged.
from chameleon_mcp.repo_id import (  # noqa: E402,F401
    _CASE_INSENSITIVE_HOSTS,
    _REPO_ID_CACHE,
    _REPO_ID_CACHE_TTL,
    _compute_repo_id,
    _fs_is_case_insensitive,
    _git_remote_url,
    _legacy_path_repo_id,
    _normalize_git_url,
    _persisted_repo_uuid,
)


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

    This notification closes the window in the common case, but it is
    best-effort, not a synchronous reload barrier: a hook call racing the
    mutation can still read one-generation-stale data before the daemon
    processes the invalidation. A session that needs strict read-after-write
    consistency on a mutating tool can run /chameleon-disable to take the
    in-process path for the rest of the session, bypassing the daemon entirely.
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


def _unsafe_root_message(reason: str) -> str:
    """One refusal string for every unsafe-root surface, naming the opt-out.

    The guard itself lives in profile.loader (find_repo_root applies it for
    the hooks); this is the user-facing rendering the write tools and
    detect_repo share so a refused repo is never a bare, unexplained no_repo.
    """
    from chameleon_mcp.profile.loader import ALLOW_TMP_REPO_ENV

    return f"unsafe_root: {reason}; set {ALLOW_TMP_REPO_ENV}=1 to opt in"


def _unsafe_root_refusal(repo_root: Path) -> str | None:
    """Refusal message if ``repo_root`` fails the unsafe-root guard, else None.

    bootstrap_repo and refresh_repo resolve their path argument directly
    (never through find_repo_root), so they must apply the same guard
    explicitly — otherwise they write profiles the hooks then refuse to load.
    """
    from chameleon_mcp.profile.loader import _is_unsafe_repo_root

    reason = _is_unsafe_repo_root(repo_root)
    if reason is None:
        return None
    return _unsafe_root_message(reason)


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
    # A lone surrogate passes the surrogatepass component check below but makes
    # find_repo_root's filesystem calls raise an uncaught UnicodeEncodeError.
    try:
        file_path.encode("utf-8")
    except UnicodeError:
        return False
    if len(file_path) > _MAX_PATH_LEN:
        return False
    for component in file_path.split("/"):
        if len(component.encode("utf-8", "surrogatepass")) > _NAME_MAX_BYTES:
            return False
    return True


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
                "(unresolvable ref / missing .chameleon at ref / scan-rejected / "
                "materialize lock held past its deadline); falling back to working tree",
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


def _persist_repo_uuid_if_no_remote(repo_root: Path) -> None:
    """Stamp a stable ``repo_uuid`` into ``.chameleon/config.json`` for repos
    without a git remote, so a later move/rename keeps the same repo_id.

    No-op when a git remote exists (the remote URL is the stronger identity) or
    when a repo_uuid is already persisted. Best-effort: any failure is swallowed
    and the repo simply keeps its path-derived id. Callers must clear the
    repo-id cache afterward because writing the uuid changes the id this repo
    resolves to.
    """
    try:
        if _git_remote_url(repo_root):
            return
        if _persisted_repo_uuid(repo_root):
            return
        profile_dir = repo_root / ".chameleon"
        if not profile_dir.is_dir():
            return
        config_path = profile_dir / "config.json"
        existing: dict = {}
        if config_path.is_file():
            try:
                parsed = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except (OSError, json.JSONDecodeError, ValueError):
                # A config malformed for some other feature must not block the
                # uuid stamp; overwrite with a minimal valid document.
                existing = {}
        from chameleon_mcp.profile.config import CURRENT_SCHEMA

        existing.setdefault("$schema", CURRENT_SCHEMA)
        existing["repo_uuid"] = secrets.token_hex(16)
        text = json.dumps(existing, indent=2, sort_keys=True) + "\n"
        tmp = config_path.with_name(config_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(config_path)
    except Exception:
        pass


def _persisted_production_ref(repo_root: Path) -> str | None:
    """Read ``.chameleon/config.json``'s ``production_ref`` if present.

    Raw tolerant read (not the strict config loader) so a config malformed
    for some unrelated feature still yields a usable lock. Fail-open: any
    read/parse error returns None and derivation falls back to the
    working tree.
    """
    try:
        raw = (repo_root / ".chameleon" / "config.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ref = data.get("production_ref")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _production_ref_explicitly_disabled(repo_root: Path) -> bool:
    """True when config.json carries an explicit ``"production_ref": null``.

    An explicit null is the user's opt-out of production pinning; the
    auto-lock migration must respect it instead of re-detecting. Distinct
    from an ABSENT key (no decision yet — migration may lock). Fail-open
    to False on any read error.
    """
    try:
        raw = (repo_root / ".chameleon" / "config.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "production_ref" in data and data["production_ref"] is None


def _persist_production_ref(repo_root: Path, branch: str) -> None:
    """Stamp ``production_ref`` into ``.chameleon/config.json``.

    Same read-modify-write shape as the repo_uuid stamp: tolerant read that
    preserves unknown keys, tmp-file + atomic replace, every failure
    swallowed. Runs before the profile-hash snapshot so the trust mirror
    captures the post-write config bytes.
    """
    try:
        profile_dir = repo_root / ".chameleon"
        if not profile_dir.is_dir():
            return
        config_path = profile_dir / "config.json"
        existing: dict = {}
        if config_path.is_file():
            try:
                parsed = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except (OSError, json.JSONDecodeError, ValueError):
                existing = {}
        if existing.get("production_ref") == branch:
            return
        from chameleon_mcp.profile.config import CURRENT_SCHEMA

        existing.setdefault("$schema", CURRENT_SCHEMA)
        existing["production_ref"] = branch
        text = json.dumps(existing, indent=2, sort_keys=True) + "\n"
        tmp = config_path.with_name(config_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(config_path)
    except Exception:
        pass


@dataclass
class _ProductionDerivation:
    """Resolved production-lock state for one bootstrap/refresh run."""

    branch: str | None = None
    source: str = "none"  # explicit | config | origin_head | named_production | default_name | none
    conflict: bool = False
    candidates: tuple[str, ...] = ()
    ref: str | None = None
    sha: str | None = None
    tree: Path | None = None  # materialized analysis root (caller must release)
    # The dir registered as a git worktree. For a subdirectory bootstrap the
    # analysis root (`tree`) points INSIDE this dir; release must remove the
    # registration root, not the analysis path.
    worktree_root: Path | None = None
    persist: bool = False  # write the lock into config.json on success
    note: str | None = None

    @property
    def locked(self) -> bool:
        return self.sha is not None

    def derivation_source(self) -> dict | None:
        if not self.locked:
            return None
        return {"mode": "production_ref", "branch": self.branch, "ref": self.ref, "sha": self.sha}

    def envelope_block(self) -> dict:
        block: dict = {
            "locked": self.locked and self.tree is not None,
            "branch": self.branch,
            "source": self.source,
        }
        if self.ref:
            block["ref"] = self.ref
        if self.sha:
            block["sha"] = self.sha
        if self.conflict:
            block["conflict"] = True
        if self.candidates:
            block["candidates"] = list(self.candidates)
        if self.note:
            block["note"] = self.note
        return block


def _prepare_production_derivation(
    repo_root: Path, *, requested_ref: str | None = None
) -> _ProductionDerivation:
    """Decide which tree this derivation analyzes.

    Precedence: explicit ``requested_ref`` (the init skill's confirmed
    answer) > the persisted config lock > auto-detection. Auto-detection
    engages the lock only when the answer is clean AND origin-backed; a
    local-only repo keeps working-tree derivation and the envelope just
    suggests the candidate. Every failure (no git, unresolvable ref,
    worktree add failure) degrades to working-tree derivation with a note
    — production pinning is best-effort, never a new hard dependency.
    """
    from chameleon_mcp.production_ref import (
        detect_production_branch,
        git_toplevel,
        materialize_production_tree,
        prune_stale_production_trees,
        resolve_production_ref,
    )

    state = _ProductionDerivation()
    try:
        # A bootstrap root may be a SUBDIRECTORY of its git repo (the
        # JS-sidecar flow: bootstrap_repo(<repo>/app/javascript)). git
        # resolves refs against the containing repo, so the materialized
        # tree is the toplevel's — analysis must re-base onto the same
        # subdirectory or the sidecar profile gets derived from the whole
        # repo (wrong language, dangling paths).
        toplevel = git_toplevel(repo_root)
        subdir_rel: Path | None = None
        if toplevel is not None:
            try:
                rel = repo_root.resolve().relative_to(toplevel)
            except (OSError, ValueError):
                rel = Path(".")
            if str(rel) not in ("", "."):
                subdir_rel = rel

        if requested_ref:
            state.branch = requested_ref
            state.source = "explicit"
            state.persist = True
        else:
            configured = _persisted_production_ref(repo_root)
            toplevel_configured = (
                _persisted_production_ref(toplevel)
                if subdir_rel is not None and toplevel is not None
                else None
            )
            if configured:
                state.branch = configured
                state.source = "config"
            elif _production_ref_explicitly_disabled(repo_root):
                # Explicit "production_ref": null — the user opted out;
                # never re-detect over their decision.
                state.source = "disabled"
                state.note = "production_ref is explicitly null (opt-out); working-tree derivation"
                return state
            elif (
                subdir_rel is not None
                and toplevel is not None
                and _production_ref_explicitly_disabled(toplevel)
            ):
                # The repo-root profile opted out; a sidecar bootstrap must
                # not auto-lock over that repo-level decision.
                state.source = "disabled"
                state.note = (
                    "production_ref is explicitly null at the repo root (opt-out); "
                    "working-tree derivation"
                )
                return state
            elif toplevel_configured:
                # Sidecar without its own lock inherits the repo root's, and
                # persists it locally so refresh stays tip-keyed.
                state.branch = toplevel_configured
                state.source = "config"
                state.persist = True
            else:
                det = detect_production_branch(repo_root)
                state.conflict = det.conflict
                state.candidates = det.candidates
                state.source = det.source
                state.branch = det.branch
                lockable = bool(det.branch) and not det.conflict and det.from_origin
                if not lockable:
                    if det.conflict:
                        state.note = (
                            "ambiguous production branch (candidates: "
                            f"{[det.branch, *det.candidates]!r}); not auto-locked"
                        )
                    elif det.branch:
                        state.note = (
                            f"detected candidate branch {det.branch!r} has no origin "
                            "backing; not auto-locked (set production_ref to opt in)"
                        )
                    return state
                state.persist = True

        resolved = resolve_production_ref(repo_root, state.branch)
        if resolved is None:
            state.note = (
                f"production_ref {state.branch!r} did not resolve to a commit; "
                "analyzed the working tree instead"
            )
            state.persist = False
            return state
        state.ref = resolved.ref
        state.sha = resolved.sha

        repo_id = _compute_repo_id(repo_root)
        from chameleon_mcp.profile.trust import repo_data_dir

        # The materialized tree becomes the extractors' scan root, and the
        # extractors emit fully symlink-resolved file paths. Resolve the
        # container BEFORE the worktree is created so the two prefixes agree:
        # a symlinked data-dir component (macOS /tmp -> /private/tmp, a linked
        # ~/.local/share) would otherwise make every relative_to() against the
        # tree fail, and clustering would commit absolute-path buckets that no
        # per-edit lookup ever matches.
        container = (repo_data_dir(repo_id) / "prodtree").resolve()
        prune_stale_production_trees(repo_root, container)
        dest = container / f"{resolved.sha[:12]}-{os.getpid()}"
        tree = materialize_production_tree(repo_root, dest, resolved.sha)
        if tree is None:
            state.note = (
                f"could not materialize {state.ref!r} (worktree add failed); "
                "analyzed the working tree instead"
            )
            # A detected lock must not persist off a degraded run — this
            # profile was derived from the working tree, and silently locking
            # would make the next refresh treat it as production-derived. An
            # EXPLICIT request still persists (the user's stated intent), and
            # the missing derivation_source self-heals via a full re-derive
            # on the next refresh.
            if state.source != "explicit":
                state.persist = False
            return state
        state.worktree_root = tree
        if subdir_rel is not None:
            sub = tree / subdir_rel
            if sub.is_dir():
                tree = sub
            else:
                # The sidecar dir does not exist at the production ref —
                # deriving from the toplevel would write a whole-repo profile
                # into the sidecar. Degrade to working-tree derivation.
                from chameleon_mcp.production_ref import remove_production_tree

                remove_production_tree(repo_root, state.worktree_root)
                state.worktree_root = None
                state.note = (
                    f"{subdir_rel.as_posix()!r} does not exist in {state.ref!r}; "
                    "analyzed the working tree instead"
                )
                if state.source != "explicit":
                    state.persist = False
                return state
        state.tree = tree
        return state
    except Exception:  # noqa: BLE001 — pinning must never break bootstrap
        state.note = "production-ref preparation failed; analyzed the working tree instead"
        if state.worktree_root is not None:
            try:
                from chameleon_mcp.production_ref import remove_production_tree

                remove_production_tree(repo_root, state.worktree_root)
            except Exception:  # noqa: BLE001
                pass
            state.worktree_root = None
        state.tree = None
        # Same rule as the materialize/subdir degrades: a DETECTED lock must
        # not persist off a run that fell back to the working tree (the sha
        # may already be resolved, which alone satisfies the persist guard).
        if state.source != "explicit":
            state.persist = False
        return state


def _release_production_derivation(repo_root: Path, state: _ProductionDerivation) -> None:
    """Remove the materialized tree, tolerating every failure."""
    target = state.worktree_root if state.worktree_root is not None else state.tree
    if target is not None:
        try:
            from chameleon_mcp.production_ref import remove_production_tree

            remove_production_tree(repo_root, target)
        except Exception:  # noqa: BLE001
            pass
        state.tree = None
        state.worktree_root = None


def _hash_profile_or_cached(profile_dir: Path, cached: dict) -> str | None:
    """Fresh profile hash, falling back to the index.db mirror on failure."""
    try:
        from chameleon_mcp.profile.trust import hash_profile

        return hash_profile(profile_dir)
    except Exception:  # noqa: BLE001
        return cached.get("profile_sha256")


def _recorded_derivation_sha(profile_dir: Path) -> str | None:
    """SHA the committed profile was derived from, or None (pre-feature or
    working-tree profiles)."""
    try:
        data = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
        src = data.get("derivation_source")
        if isinstance(src, dict):
            sha = src.get("sha")
            if isinstance(sha, str) and sha:
                return sha
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to.

    The envelope also carries a ``production_branch`` block: for a locked
    repo ``{locked: true, branch, resolvable}``; otherwise the detection
    result ``{locked: false, branch, source, conflict, candidates,
    from_origin}`` the init skill reads to decide whether to auto-lock,
    ask, or skip.

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
    from chameleon_mcp.profile.loader import find_repo_root_with_refusal
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
    repo_root, root_refusal = find_repo_root_with_refusal(p)
    if repo_root is None:
        no_repo: dict = {
            "repo_id": None,
            "repo_root": None,
            "profile_status": "no_repo",
            "trust_state": "n/a",
        }
        # A guard-refused root must say WHY: a bare no_repo on a repo that
        # visibly exists reads as a dead install with zero explanation.
        if root_refusal is not None:
            no_repo["reason"] = _unsafe_root_message(root_refusal)
        return _envelope(no_repo)

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
    profile_too_new = False
    if profile_present:
        from chameleon_mcp.bootstrap.transaction import is_committed

        if not is_committed(profile_dir):
            # profile.json exists but the COMMITTED sentinel is missing: the read
            # path (load_profile_dir) refuses it and reports profile_corrupted, so
            # detect_repo must agree instead of claiming profile_present/trusted.
            profile_corrupted = True
    if profile_present and not profile_corrupted:
        try:
            import json as _json

            with profile_file.open("r", encoding="utf-8") as fh:
                _peek = _json.load(fh)
            from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

            _sv = _peek.get("schema_version") if isinstance(_peek, dict) else None
            if _sv is not None and (isinstance(_sv, bool) or not isinstance(_sv, int)):
                # schema_version is present but not a plain integer: a malformed /
                # hand-edited manifest. Report corrupt rather than serving it as a
                # healthy profile (bootstrap always writes an int).
                profile_corrupted = True
            elif isinstance(_sv, int) and _sv > MAX_SUPPORTED_SCHEMA_VERSION:
                profile_unsupported_schema = True
            # A profile from a NEWER engine is intact, just unreadable here;
            # "corrupted" would send the user chasing damage that isn't there,
            # and "trusted/present" would imply this engine can honor it.
            if _profile_requires_newer_engine(profile_dir) is not None:
                profile_too_new = True
        except (OSError, ValueError):
            profile_corrupted = True

    if not profile_present or profile_corrupted or profile_unsupported_schema or profile_too_new:
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
    elif profile_too_new:
        profile_status = "profile_too_new"
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
    # Production-branch state for the init/refresh skill flows: the persisted
    # lock when one exists, else what auto-detection would pick — so the skill
    # can announce a clean zero-touch lock, or ask the user only when the
    # signal is ambiguous (conflict) or absent. Best-effort: never fails the
    # detect_repo call.
    try:
        from chameleon_mcp.production_ref import detect_production_branch

        locked_branch = _persisted_production_ref(repo_root)
        if locked_branch:
            from chameleon_mcp.production_ref import resolve_production_ref

            _det_resolved = resolve_production_ref(repo_root, locked_branch)
            lock_block: dict = {
                "locked": True,
                "branch": locked_branch,
                "resolvable": _det_resolved is not None,
            }
            if _det_resolved is None:
                lock_block["note"] = (
                    f"locked branch {locked_branch!r} does not resolve to a commit; "
                    "derivation will fall back to the working tree (see /chameleon-doctor)"
                )
            data["production_branch"] = lock_block
        else:
            det = detect_production_branch(repo_root)
            data["production_branch"] = {
                "locked": False,
                "branch": det.branch,
                "source": det.source,
                "conflict": det.conflict,
                "candidates": list(det.candidates),
                "from_origin": det.from_origin,
            }
    except Exception:  # noqa: BLE001
        pass
    return _envelope(data)


def _norm_rel_path(rel: str) -> str:
    """Normalize a repo-relative path to forward-slash segments.

    Repo-relative paths reach the scoring/overlap helpers from two sources: the
    edited file's path (which on Windows stringifies with backslashes) and
    witness paths loaded from JSON profile artifacts (which on a Windows-authored
    or cross-platform-shared profile may carry backslashes). The directory-prefix
    overlap logic splits on "/", so a backslash path scores zero overlap and the
    archetype resolution silently degrades. Folding both separators to "/" keeps
    the comparison correct regardless of authoring platform.
    """
    return rel.replace("\\", "/")


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


def _nearest_canonical_entry(rel_str: str, entries: list, snapshot=None) -> dict:
    """Pick the canonical entry whose witness best fits the edited file.

    A dense archetype can carry several merged sub-buckets (e.g. services across
    amazon_s3/, hubspot/, llm/), each with its own canonical witness. Ranking is
    ``(shape_match, path_overlap)``: when the edited file's ``snapshot`` is known,
    a witness whose recorded ``ast_query`` matches the file's shape wins, so a
    ClassNode controller is not shown a ModuleNode witness from the same archetype;
    leading-directory overlap is the tiebreak among equal-shape witnesses. With no
    snapshot (or no shape data) shape is constant, so this is the prior pure
    path-overlap behavior, ties keeping entries[0].
    """
    if not entries:
        return {}
    from chameleon_mcp.lint_engine import canonical_confidence

    q_parts = _norm_rel_path(rel_str).split("/")[:-1]
    best = entries[0] or {}
    best_key = (-1.0, -1)
    for e in entries:
        e = e or {}
        witness_rel = (e.get("witness") or {}).get("path") or ""
        w_parts = _norm_rel_path(witness_rel).split("/")[:-1]
        overlap = 0
        for a, b in zip(q_parts, w_parts):  # noqa: B905
            if a == b:
                overlap += 1
            else:
                break
        if snapshot is not None:
            ast_query = (e.get("normative_shape") or {}).get("ast_query")
            shape = canonical_confidence(snapshot, ast_query)
        else:
            shape = 0.0
        key = (shape, overlap)
        if key > best_key:
            best_key = key
            best = e
    return best


def _file_shape_snapshot(p: Path, loaded):
    """The edited file's AST dimension snapshot, for shape-aware witness choice.

    Mirrors the content read + language detection in ``_get_archetype_with_loaded``
    so the shape compared against witness ``ast_query`` is the same one archetype
    scoring used. Returns None for an absent/unreadable file (the new-file case),
    so witness selection falls back to path overlap.
    """
    from chameleon_mcp.lint_engine import detect_language, extract_dimensions

    if not p.is_file():
        return None
    try:
        content = p.read_bytes()[:100_000].decode("utf-8", errors="replace")
    except OSError:
        return None
    language = detect_language(str(p)) or loaded.profile.get("language")
    if language not in ("typescript", "ruby"):
        language = None
    try:
        return extract_dimensions(content, language=language)
    except Exception:
        return None


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
    q_parts = _norm_rel_path(rel_str).split("/")[:-1]
    w_parts = _norm_rel_path(witness_path).split("/")[:-1]
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


def _empty_archetype_envelope(content_signal_match: str, file_exists: bool) -> dict:
    """The no-match payload returned by ``get_archetype``'s early exits.

    Every early-return path (bad input, no repo, repo-id mismatch, profile load
    failure) yields the same shape; only the content-signal and file-exists
    fields vary by how far resolution got.
    """
    return {
        "archetype": None,
        "alternatives": [],
        "content_signal_match": content_signal_match,
        "confidence_band": "low",
        "match_quality": "none",
        "match_basis": None,
        "file_exists": file_exists,
    }


def get_archetype(repo: str, file_path: str) -> dict:
    """Look up the archetype a given file matches.

    Tiebreaks among multiple path-bucket matches by AST shape.
    When the file exists on disk we extract its dimensions via the lint
    engine's pure-function `extract_dimensions` and score each path-bucket
    candidate by how many `ast_query` dimensions align. Higher score wins;
    ties fall back to the cluster-size ordering.

    The confidence band reflects the strength of whatever evidence backed the
    match, and ``match_basis`` names that evidence so bands from different
    bases are not compared as if they were one scale:
      basis "path_and_ast" (file readable, canonical ast_query available):
        "high"   — score >= 4 of 5 ast_query dimensions agreed
        "medium" — at least one dimension agreed
        "low"    — no dimension agreed
      basis "path_only" (file missing/unreadable, or no ast_query to compare):
        "high"   — exactly one archetype's path pattern matched
        "low"    — multiple candidates or substring-only fallback
    A nonexistent path scoring "exact/high" while a real file scores
    "ast/medium" is therefore expected, not inverted: the phantom band speaks
    only to path-pattern uniqueness (the supported PreToolUse new-file case —
    the hook fires before the Write creates the file), the real-file band to
    AST agreement. ``file_exists`` distinguishes the two at a glance.

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
        return _envelope(_empty_archetype_envelope("none", False))

    p = Path(file_path).expanduser()

    content_signal_value: str = _content_signal_for_path(p)

    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(_empty_archetype_envelope(content_signal_value, p.is_file()))

    expected_repo_id = _compute_repo_id(repo_root)
    if _REPO_ID_RE.match(repo) if isinstance(repo, str) else False:
        if expected_repo_id != repo:
            return _envelope(_empty_archetype_envelope(content_signal_value, p.is_file()))
    else:
        _resolved_path, resolved_repo_id = _resolve_repo_arg(repo)
        if resolved_repo_id is None or resolved_repo_id != expected_repo_id:
            return _envelope(_empty_archetype_envelope(content_signal_value, p.is_file()))

    profile_dir = _effective_profile_dir(repo_root)
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        return _envelope(_empty_archetype_envelope(content_signal_value, p.is_file()))

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
    rel_str = _norm_rel_path(rel_str)
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
                "match_basis": "path_only" if primary is not None else None,
                "file_exists": p.is_file(),
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
        # Path-pattern-only branding for a file with no readable content — the
        # supported PreToolUse new-file case (the hook fires before the Write
        # creates the file). The band can read "high" on a unique path match,
        # which is HIGHER than an existing file scoring medium on AST: the two
        # bands rest on different evidence, so `match_basis`/`file_exists`
        # carry the distinction rather than the band silently inverting.
        return _envelope(
            {
                "archetype": exact_matches[0],
                "alternatives": exact_matches[1:],
                "content_signal_match": content_signal_value,
                "confidence_band": "high" if len(exact_matches) == 1 else "low",
                "match_quality": "exact",
                "match_basis": "path_only",
                "file_exists": False,
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
        match_basis = "path_and_ast"
    else:
        primary = exact_matches[0]
        alternatives = exact_matches[1:]
        confidence = "high" if len(exact_matches) == 1 else "low"
        match_quality = "exact"
        match_basis = "path_only"

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
            "match_basis": match_basis,
            "file_exists": True,
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


def _classify_profile_load_failure(profile_file: Path) -> str:
    """Decide the profile_status when load_profile_dir raises.

    A profile written by a newer engine (schema_version above the supported
    max) is a version mismatch, not data corruption. Peek at schema_version
    the same way detect_repo does so both tools report the same status; fall
    back to "profile_corrupted" for an unparseable file or any lower version.
    """
    try:
        import json as _json

        with profile_file.open("r", encoding="utf-8") as fh:
            peek = _json.load(fh)
    except (OSError, ValueError):
        return "profile_corrupted"
    from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

    schema = peek.get("schema_version") if isinstance(peek, dict) else None
    if isinstance(schema, int) and schema > MAX_SUPPORTED_SCHEMA_VERSION:
        return "profile_unsupported_schema_version"
    return "profile_corrupted"


def get_pattern_context(file_path: str) -> dict:
    """Collapsed call: archetype + canonical + rules + meta in one round trip.

    Returns real archetype data when the profile is present and trusted. The
    archetype envelope carries provenance the caller must weigh:

    - ``match_quality`` — ``none`` / ``fallback`` / ``exact`` / ``ast``, as
      resolved at archetype-match time.
    - ``match_basis`` — ``path_only`` when the resolution used the path alone
      (the file does not exist on disk yet, e.g. a pre-write PreToolUse call) or
      ``path_and_ast`` when the file's AST shape was also scored. ``confidence_band``
      rests on a different scale per basis, so a nonexistent path can score
      ``exact`` / ``high`` (path uniqueness) while a real file scores ``ast`` /
      ``medium`` (AST agreement) — expected, not inverted.
    - ``file_exists`` — whether the path is a real file. A phantom path still
      returns a full canonical/rules/idioms envelope (so a pre-write call gets
      priming), so a consumer that needs the file to exist must check this flag
      itself rather than infer it from a populated envelope.
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
        return _envelope(
            _empty_pattern_envelope(repo_id, _classify_profile_load_failure(profile_file), "n/a")
        )

    content_signal_value = _content_signal_for_path(p)
    arch_response = _get_archetype_with_loaded(p, repo_root, loaded, content_signal_value)
    arch_data = arch_response["data"]

    if arch_data.get("archetype"):
        arch_entry = loaded.archetypes.get("archetypes", {}).get(arch_data["archetype"], {}) or {}
        sub_buckets = arch_entry.get("sub_buckets") or {}
        arch_data["sub_buckets_count"] = len(sub_buckets) if isinstance(sub_buckets, dict) else 0
        summary = arch_entry.get("summary") or ""
        if summary:
            # Free prose from archetypes.json (attacker-controllable in a committed
            # profile). Sanitize before it enters the model-callable response, for
            # parity with idioms below: an unsanitized summary could carry a
            # </chameleon-context> tag-escape or a forged status header.
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context

            arch_data["summary"] = sanitize_for_chameleon_context(summary)
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
            # Only extract the file's shape when there is more than one witness to
            # choose between (cost-bounded to the multi-sub-bucket case); a single
            # witness needs no shape comparison.
            snapshot = _file_shape_snapshot(p, loaded) if len(canonicals) > 1 else None
            first = _nearest_canonical_entry(rel_str, canonicals, snapshot=snapshot)
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
                except FileNotFoundError:
                    # Witness deleted or renamed since derivation. Do not serve a
                    # silent empty excerpt: flag it so the hook and direct
                    # callers can tell the user to refresh.
                    canonical_data = {
                        "content": "",
                        "witness_path": witness_rel,
                        "truncated": False,
                        "missing": True,
                        "sha_hint": first.get("witness", {}).get("sha_hint"),
                    }
                except UnsafeFileError as e:
                    if isinstance(e.__cause__, FileNotFoundError):
                        # safe_open_fd wraps FileNotFoundError; treat it the same way.
                        canonical_data = {
                            "content": "",
                            "witness_path": witness_rel,
                            "truncated": False,
                            "missing": True,
                            "sha_hint": first.get("witness", {}).get("sha_hint"),
                        }
                    # Security rejection (traversal/symlink): leave canonical_data empty.
                except OSError:
                    # Read error (e.g. mid-read change detection): leave empty.
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
        # Distinguish "profile failed to load" from the legitimate "archetype
        # has no witness" empty result, which returns this same shape minus
        # the degraded flag; without it corruption reads as a benign no-op.
        return _envelope(
            {
                "status": "degraded",
                "reason": "profile_unavailable",
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
    except FileNotFoundError:
        # Witness deleted or never created on this working tree. Flag it so
        # callers can tell the user to refresh rather than silently degrading
        # to an empty excerpt with no signal.
        return _envelope(
            {
                "content": "",
                "witness_path": witness_rel,
                "truncated": False,
                "missing": True,
                "sha_hint": witness.get("sha_hint"),
            }
        )
    except UnsafeFileError as e:
        if isinstance(e.__cause__, FileNotFoundError):
            # safe_open wraps FileNotFoundError; treat it the same way.
            return _envelope(
                {
                    "content": "",
                    "witness_path": witness_rel,
                    "truncated": False,
                    "missing": True,
                    "sha_hint": witness.get("sha_hint"),
                }
            )
        # Security rejection (traversal, symlink, etc.): leave content empty.
        return _envelope(
            {
                "content": "",
                "witness_path": witness_rel,
                "truncated": False,
                "sha_hint": witness.get("sha_hint"),
            }
        )
    except OSError:
        # Read error or other I/O failure: leave content empty.
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
        # A repo with no configured lint rules and a repo whose profile failed
        # to load must not look identical: the caller needs the degraded flag
        # to avoid reading corruption as "nothing to enforce".
        env = {"rules": [], "status": "degraded", "reason": "profile_unavailable"}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    rules_dict = loaded.rules.get("rules", {}) or {}

    # A source whose config could not be (fully) parsed carries a per-source
    # ``parse_warning``. Aggregate them at the top level too: on a Rails repo
    # whose primary linter is rubocop, "zero rubocop rules" with the warning
    # buried inside an empty source block read as silent degradation.
    # Sanitize each warning — a YAML parse error embeds source lines from the
    # committed (attacker-controllable) config file, so the string must not be
    # able to smuggle tag-boundary tokens onto the model surface. The
    # per-source block gets the sanitized form too.
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize_warn

    parse_warnings: dict[str, str] = {}
    for key, val in rules_dict.items():
        if isinstance(val, dict) and val.get("parse_warning"):
            clean_warning = _sanitize_warn(str(val["parse_warning"]))
            parse_warnings[key] = clean_warning
            val["parse_warning"] = clean_warning

    def _with_warnings(env: dict) -> dict:
        if parse_warnings:
            env["parse_warnings"] = parse_warnings
        return env

    if source is None:
        env = _with_warnings({"rules": list(rules_dict.items())})
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    if source in rules_dict:
        env = _with_warnings({"rules": [(source, rules_dict[source])]})
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
    # Tag each secret hit with whether its kind may hard-block, matching the hook
    # path. scan_secrets emits every kind under one rule; the enforcement gate
    # reads the per-hit secret_hard flag, so a consumer of this tool's violations
    # (the posttool daemon path) sees the same hardness classification the
    # in-process lint produces. Only the deterministic high-precision kinds set
    # secret_hard; entropy/broad-fallback hits stay advisory.
    try:
        from chameleon_mcp.violation_class import tag_secret_hardness as _tag_secret_hardness

        _tag_secret_hardness(secret_violations)
    except Exception:
        pass

    # Dangerous code sinks (dynamic eval, weak hash, insecure random, SQL string
    # interpolation) are content facts about the caller's own submission, like the
    # secret scan above, so they run before the trust/canonical gates and ride the
    # early-return paths too. eval-call is block-eligible; the rest are advisory.
    # Language is inferred from the path; with no recognizable extension only the
    # language-agnostic eval( shape fires, which is the block-eligible one.
    _sink_lang = _detect_language(file_path) if file_path else None
    if _sink_lang not in ("typescript", "ruby"):
        _sink_lang = None
    sink_violations: list[dict] = []
    try:
        from chameleon_mcp.lint_engine import scan_dangerous_sinks as _scan_dangerous_sinks

        sink_violations = [
            v.to_dict() for v in _scan_dangerous_sinks(working_content, language=_sink_lang)
        ]
    except Exception:
        sink_violations = []

    # Resolve `repo` through the shape-detecting resolver every other read tool
    # uses. Calling `_resolve_repo_root_by_id` directly let a hostile path
    # (nonexistent absolute path, embedded NUL, `../` traversal) reach
    # `repo_data_dir`'s mkdir and crash instead of failing open with the stub
    # envelope below.
    resolved_path, _resolved_repo_id = _resolve_repo_arg(repo)
    repo_root = resolved_path
    try:
        if repo_root is not None and not repo_root.is_dir():
            repo_root = None
    except (OSError, ValueError):
        repo_root = None
    if repo_root is None:
        candidate = Path(repo) if isinstance(repo, str) and repo else None
        try:
            if (
                candidate is not None
                and candidate.is_absolute()
                and candidate.is_dir()
                and (candidate / ".chameleon" / "profile.json").is_file()
            ):
                repo_root = candidate
        except (OSError, ValueError):
            repo_root = None

    if repo_root is None:
        return _envelope(
            {
                "stub": True,
                "stub_reason": (
                    "repo could not be resolved to a profile dir; "
                    "/chameleon-init or /chameleon-trust the repo first"
                ),
                "violations": secret_violations + sink_violations,
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
                "violations": secret_violations + sink_violations,
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
                "violations": secret_violations + sink_violations,
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
                    "violations": secret_violations + sink_violations,
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

    # An empty candidate_queries set means no ast_query could be derived for this
    # archetype (common for sparse/test archetypes on real repos). The dimension
    # lint is the only scan below that needs an ast_query, so skip just that one
    # and still run everything archetype-independent or name-gated: the conventions
    # block (test-quality, required_guards, file-naming), the phantom-import check,
    # the cross-file check, and the style scan. The noop_reason then narrates that
    # only the dimension lint was withheld, not the whole tool.
    ast_noop_reason: str | None = None
    if not candidate_queries:
        ast_noop_reason = (
            f"no ast_query for archetype {archetype!r} (dimension lint withheld) "
            "-- re-bootstrap via /chameleon-refresh"
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
        # required_guards drives the advisory authz hint. The lint reads it from
        # the per-archetype slice, so it must be copied in alongside the others or
        # the rule can never fire on this path.
        if conv_data.get("required_guards", {}).get(archetype):
            arch_conv["required_guards"] = conv_data["required_guards"][archetype]
        # The test-quality pass gates only on the archetype name (it reads no
        # convention keys), but lint_conventions early-returns on an empty
        # conventions dict. A test/spec archetype often has no import/naming/
        # inheritance conventions, so arch_conv would be empty and the pass would
        # never run. Force a non-empty dict for those archetypes so the pass fires.
        _is_test_arch = isinstance(archetype, str) and archetype.startswith(("test", "spec"))
        if _is_test_arch and not arch_conv:
            arch_conv = {"_test_quality_only": True}
        # Witness content self-calibrates the test-quality stub/freeze/helper gates
        # to the team's own test style. Read it only for a test/spec archetype (the
        # only case where the gated rules can fire), via the same path-safe read
        # the AST recalibration uses, so the common non-test edit stays witness-free.
        _witness_content: str | None = None
        if _is_test_arch and witness_rel_path:
            try:
                from chameleon_mcp.safe_open import safe_open as _safe_open

                _wpath = _safe_open(repo_root, witness_rel_path, max_size_bytes=WITNESS_MAX_BYTES)
                _witness_content = _wpath.read_bytes()[:100_000].decode("utf-8", errors="replace")
            except Exception:
                _witness_content = None
        if arch_conv:
            # Thread the archetype name (enables the test-quality pass on test/spec
            # files) and the witness content (self-calibrates its gated checks); the
            # params are keyword-only with None defaults, so guard the call in case
            # an older engine build lacks them (then fall back to the legacy shape).
            try:
                conv_violations = _lint_conventions(
                    working_content,
                    arch_conv,
                    language=language,
                    file_path=file_path,
                    archetype_name=archetype,
                    witness_content=_witness_content,
                )
            except TypeError:
                conv_violations = _lint_conventions(working_content, arch_conv, language=language)
            convention_violations = [v.to_dict() for v in conv_violations]
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

    # Cross-file import context (index-backed; advisory). Surfaces who imports
    # the edited module's bindings and any export removed out from under an
    # indexed call site. Reads the prebuilt reverse index only -- no caller is
    # re-parsed on the hot path; silent without an absolute in-repo file_path.
    crossfile_violations: list[dict] = []
    try:
        from chameleon_mcp.phantom_imports import lint_cross_file_imports

        crossfile_violations = [
            v.to_dict()
            for v in lint_cross_file_imports(
                working_content,
                file_path=file_path,
                repo_root=repo_root,
                language=language,
            )
        ]
    except Exception:
        crossfile_violations = []

    # Style baseline (indent / quote / line-length vs the repo's declared
    # formatter config in rules.json). Archetype-independent like the secret and
    # sink scans, so a sparse repo with no resolvable archetype still gets style
    # feedback; advisory only, never block-eligible. Reads only declared config
    # values and emits its own messages, so it is safe past the trust gate.
    style_violations: list[dict] = []
    try:
        from chameleon_mcp.lint_engine import scan_style_rules as _scan_style_rules

        style_violations = [
            v.to_dict()
            for v in _scan_style_rules(
                working_content,
                language=language,
                rules=loaded.rules,
                file_path=file_path,
                repo_root=repo_root,
            )
        ]
    except Exception:
        style_violations = []

    # Sanitize profile-derived violation messages (they embed conventions.json /
    # witness-derived strings) before returning on a model-callable surface,
    # matching posttool_verify. Secret and dangerous-sink violations come from the
    # caller's own content and are left as-is. Style violations carry no profile
    # strings (only declared config numbers), so they need no sanitizing.
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    for _v in (
        best_ast_violations + convention_violations + phantom_violations + crossfile_violations
    ):
        for _k, _val in list(_v.items()):
            if isinstance(_val, str):
                _v[_k] = _sanitize(_val)

    violations = (
        secret_violations
        + best_ast_violations
        + convention_violations
        + phantom_violations
        + crossfile_violations
        + sink_violations
        + style_violations
    )

    # Stamp every violation an inline chameleon-ignore directive covers with
    # ``ignored: True`` rather than dropping it: the posttool recorder reads this
    # list to tally inline overrides for the shadow report, so the overridden
    # hits must remain visible to that audit. A consumer that wants the effective
    # set filters on ``not v.get("ignored")``. The eval-call / secret scans run
    # before the convention lint and are not gated by the directive upstream, so
    # without this flag they would read as un-suppressed on this tool surface.
    try:
        from chameleon_mcp.violation_class import (
            build_ignore_index as _build_ignore_index,
        )
        from chameleon_mcp.violation_class import (
            is_violation_ignored as _is_violation_ignored,
        )

        _ig_idx = _build_ignore_index(working_content, file_path=file_path)
        if _ig_idx is not None:
            for _v in violations:
                if _is_violation_ignored(_v, _ig_idx):
                    _v["ignored"] = True
    except Exception:
        pass

    out = {
        "stub": False,
        "stub_reason": None,
        "violations": violations,
        "canonical_confidence": best_confidence,
        "unparseable_regions": snapshot.unparseable_regions,
        "content_size": content_size,
        "archetype": archetype,
        "language": language,
    }
    if ast_noop_reason is not None:
        out["noop_reason"] = ast_noop_reason
    return _envelope(out, truncated=truncated)


def _crossfile_unavailable_reason(repo_root: Path) -> str:
    """Why no reverse index exists here: a TS-only feature vs a missing artifact.

    The symbol indexes are built from TS/JS export extras only, so on a Ruby
    profile their absence is by design; a bare not-found read as damage and
    sent users chasing a repair that does not exist.
    """
    from chameleon_mcp.enforcement_calibration import _stored_profile_languages

    langs = _stored_profile_languages(_effective_profile_dir(repo_root))
    if langs and "typescript" not in langs:
        return "typescript-only"
    return "index-unavailable"


def query_symbol_importers(repo: str, file_path: str) -> dict:
    """Who imports a TypeScript module's bindings, and which imports it now breaks.

    The cross-file read backing PR-review and the Stop existence check. Reads the
    prebuilt reverse index (``symbol -> importers``) plus the module's CURRENT
    on-disk export set and returns, per the module at ``file_path``:

    - ``importers``: each exported name with indexed importers, as
      ``{name, count, sites: [{path, line}]}`` -- the rename blast radius.
    - ``broken``: each name an importer still references that the module NO
      LONGER exports -- the deterministic existence break (removed / renamed
      export with a live call site). High-confidence; the only finding class a
      consumer should surface as more than advisory.
    - ``export_set_open``: True when the module does ``export * from`` and its
      set can't be enumerated, so ``broken`` is suppressed (a missing name may be
      re-exported). ``importers`` is still reported.

    Fails open with an empty result (``found: False``) on any ambiguity:
    unresolvable / untrusted repo, missing index, unreadable module. Never
    fabricates an importer -- every site is a row the bootstrap recorded.
    """
    from chameleon_mcp.phantom_imports import _current_export_names
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.safe_open import safe_read_text
    from chameleon_mcp.symbol_index import load_reverse_index, module_key_for_path

    empty = {
        "found": False,
        "module": None,
        "importers": [],
        "broken": [],
        "export_set_open": False,
    }

    if not _validate_file_path_arg(file_path):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(dict(empty))

    # The repo arg must agree with the file's own repo, mirroring get_archetype's
    # cross-arg consistency check (a mismatched repo id must not read another
    # repo's index).
    expected_repo_id = _compute_repo_id(repo_root)
    if isinstance(repo, str) and _REPO_ID_RE.match(repo):
        if repo != expected_repo_id:
            return _envelope(dict(empty))
    else:
        _resolved_path, resolved_repo_id = _resolve_repo_arg(repo)
        if resolved_repo_id is None or resolved_repo_id != expected_repo_id:
            return _envelope(dict(empty))

    # Trust-gate: the index is an attacker-controllable committed artifact, so
    # its importer paths must not reach the model surface from an untrusted
    # profile (mirrors lint_file / the other read tools).
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    index = load_reverse_index(repo_root)
    if index is None:
        out = dict(empty)
        out["reason"] = _crossfile_unavailable_reason(repo_root)
        return _envelope(out)
    target_key = module_key_for_path(p, repo_root)
    if target_key is None:
        return _envelope(dict(empty))
    indexed = index.names_for(target_key)
    if not indexed:
        # The module is real but nothing imports it by name; report found with
        # empty lists so a caller can tell "no importers" from "couldn't look".
        out = dict(empty)
        out["found"] = True
        out["module"] = target_key
        return _envelope(out)

    try:
        content = safe_read_text(repo_root, target_key, max_size_bytes=1_000_000)
    except Exception:
        # The module can't be read (deleted, oversized, unsafe path); without its
        # current export set the existence check can't run -- fail open.
        return _envelope(dict(empty))

    current, open_set = _current_export_names(content)

    importers_out: list[dict] = []
    broken_out: list[dict] = []
    for name, importers in sorted(indexed.items()):
        sites = [{"path": imp.path, "line": imp.line} for imp in importers]
        if name in current:
            importers_out.append({"name": name, "count": len(importers), "sites": sites})
        elif not open_set:
            broken_out.append({"name": name, "count": len(importers), "sites": sites})

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    def _clean(rows: list[dict]) -> list[dict]:
        for row in rows:
            row["name"] = _sanitize(row["name"])
            for s in row["sites"]:
                if isinstance(s.get("path"), str):
                    s["path"] = _sanitize(s["path"])
        return rows

    return _envelope(
        {
            "found": True,
            "module": _sanitize(target_key),
            "importers": _clean(importers_out),
            "broken": _clean(broken_out),
            "export_set_open": open_set,
        }
    )


def get_callers(repo: str, file_path: str, function_name: str) -> dict:
    """Who calls a function, from the committed calls snapshot (deterministic grades only).

    Reads the prebuilt ``calls_index.json`` artifact and returns the recorded
    caller rows for ``function_name`` defined in the file at ``file_path``.

    Grades are deterministic: ``same_file`` (bare call to a file-local name or
    ``this.``/``self.`` to a class member defined in the same file),
    ``import`` (TypeScript named-import or namespace-import call, closed export
    set only), ``constant_receiver`` (Ruby ``Const.method`` with exactly one
    defining class). Name-only / dynamic / inheritance-based call paths are
    deliberately absent -- the index asserts only what is unambiguous.

    Interpretation note: absence of callers is NOT evidence of dead code.
    Dynamic dispatch, unsupported call patterns (superclass chains, runtime
    reflection, metaprogramming), and callers added after the last bootstrap
    are all invisible. The result is a grounding fact for deterministic review,
    not a reachability oracle. The snapshot reflects the profile derivation
    point; run ``/chameleon-refresh`` to update it.

    Fails open with ``found: False`` on any ambiguity: unresolvable / untrusted
    repo, missing artifact, path outside the repo. Never fabricates a caller.
    """
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.symbol_index import module_key_for_path

    empty = {
        "found": False,
        "module": None,
        "function": None,
        "callers": [],
        "total": 0,
        "truncated": False,
    }

    if not _validate_file_path_arg(file_path):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(dict(empty))

    # The repo arg must agree with the file's own repo, mirroring
    # query_symbol_importers' cross-arg consistency check.
    expected_repo_id = _compute_repo_id(repo_root)
    if isinstance(repo, str) and _REPO_ID_RE.match(repo):
        if repo != expected_repo_id:
            return _envelope(dict(empty))
    else:
        _resolved_path, resolved_repo_id = _resolve_repo_arg(repo)
        if resolved_repo_id is None or resolved_repo_id != expected_repo_id:
            return _envelope(dict(empty))

    # Trust-gate: the calls index is a committed artifact whose caller paths
    # must not reach the model surface from an untrusted profile.
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    index = load_calls_index(repo_root)
    if index is None:
        out = dict(empty)
        out["reason"] = "no-calls-index"
        return _envelope(out)

    rel = module_key_for_path(p, repo_root)
    if rel is None:
        out = dict(empty)
        out["reason"] = "file-outside-repo"
        return _envelope(out)

    entry = index.callers_of(rel, function_name)
    if entry is None:
        # The (file, name) pair was not recorded -- a known-absent callee is a
        # real answer (no deterministic callers at derivation time), not an error.
        return _envelope(
            {
                "found": True,
                "module": rel,
                "function": function_name,
                "callers": [],
                "total": 0,
                "truncated": False,
            }
        )

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    clean_callers = []
    for row in entry["callers"]:
        clean_callers.append(
            {
                "path": _sanitize(row["path"])
                if isinstance(row.get("path"), str)
                else row.get("path"),
                "caller": _sanitize(row["caller"])
                if isinstance(row.get("caller"), str)
                else row.get("caller"),
                "line": row.get("line"),
                "grade": row.get("grade"),
            }
        )

    return _envelope(
        {
            "found": True,
            "module": _sanitize(rel),
            "function": _sanitize(function_name),
            "callers": clean_callers,
            "total": entry["total"],
            "truncated": entry["truncated"],
        }
    )


def _name_still_referenced(repo_root: Path, importer_rel: str, name: str, line: int | None) -> bool:
    """Cheap regex presence check: does ``importer_rel`` still name ``name``?

    The reverse index is a bootstrap snapshot; an importer may have dropped the
    reference since (the rename was completed there too). Confirm the importer
    file still references ``name`` as a whole word so a finding is not raised for
    a call site that no longer exists. Prefers the recorded import ``line`` when
    present (the index placed the import there), and falls back to a whole-file
    scan when the line drifted or was never placed. No parser -- a word-boundary
    regex over the file bytes, matching the rest of the cross-file path's
    in-process, never-execute-repo-code stance. Returns False on any read error
    so an unreadable importer cannot prop up a finding.
    """
    from chameleon_mcp.safe_open import safe_read_text

    try:
        content = safe_read_text(repo_root, importer_rel, max_size_bytes=1_000_000)
    except Exception:
        return False
    needle = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])")
    lines = content.splitlines()
    if line is not None and 1 <= line <= len(lines):
        if needle.search(lines[line - 1]):
            return True
    return bool(needle.search(content))


def get_crossfile_context(repo: str) -> dict:
    """Cross-file existence breaks across a TypeScript repo, for PR review.

    The single cross-file finding class chameleon can assert deterministically: a
    binding a module USED to export (so the reverse index records importers for
    it) is gone from the module's CURRENT export set, while indexed importers
    still name it. Each such removed/renamed export is a call site the diff broke.

    Scans the prebuilt reverse index (``symbol -> importers``) target by target,
    recomputes each module's current export set from its on-disk source (a regex
    read, no parser), and returns one finding per still-referenced removed export.
    No importer is re-parsed and no edge is fabricated; every site is a row the
    bootstrap recorded.

    Each finding carries ``high_confidence``, true ONLY when the resolver chain is
    unambiguous end to end:

    - exact file match: the module's index key resolves to a real file whose
      export set could be read;
    - the export set is closed (no ``export * from``), so a missing name is truly
      gone, not possibly re-exported through a star;
    - at least one indexed importer still references the name (the recorded import
      line, else anywhere in that file) by a cheap presence check.

    A consumer (PR review) must relay ONLY ``high_confidence=true`` findings; a
    leaky resolution must not launder a wrong cross-file claim past the integrity
    rule. Findings with ``high_confidence=false`` are returned for transparency
    (the module was open-set, or no importer still names the binding) and must be
    dropped, not surfaced.

    Returns ``{"found": bool, "findings": [...]}`` where each finding is
    ``{symbol, module, count, high_confidence, sites: [{path, line}]}``. Fails
    open with ``found: False`` on any ambiguity (unresolvable / untrusted repo,
    missing index). TS-only; Ruby has no static import-of-named-symbol.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.phantom_imports import _current_export_names
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.safe_open import safe_read_text
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize
    from chameleon_mcp.symbol_index import load_reverse_index

    empty = {"found": False, "findings": []}

    repo_root, repo_id = _resolve_repo_arg(repo)
    if repo_root is None and repo_id is not None:
        repo_root = _resolve_repo_root_by_id(repo_id)
    if repo_root is None or not repo_root.is_dir():
        return _envelope(dict(empty))

    # Trust-gate: the reverse index is an attacker-controllable committed
    # artifact, so its importer paths must not reach the model surface from an
    # untrusted profile (mirrors the other cross-file reads).
    expected_repo_id = _compute_repo_id(repo_root)
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    index = load_reverse_index(repo_root)
    if index is None:
        out = dict(empty)
        out["reason"] = _crossfile_unavailable_reason(repo_root)
        return _envelope(out)

    max_modules = threshold_int("CROSSFILE_MAX_MODULES_SCANNED")
    max_findings = threshold_int("CROSSFILE_MAX_FINDINGS")
    max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")

    # Sorted target keys so a truncated scan is deterministic across runs.
    # High- and low-confidence findings are capped SEPARATELY: low-confidence
    # open-set/barrel rows are transparency output the consumer always drops,
    # so letting them share one cap would crowd the genuine high-confidence
    # breaks found later in the scan order out of the response entirely.
    target_keys = sorted(index.target_keys())
    high: list[dict] = []
    low: list[dict] = []
    low_cap = threshold_int("CROSSFILE_MAX_LOW_CONFIDENCE")
    low_dropped = 0
    truncated = False
    for target_key in target_keys[:max_modules]:
        if len(high) >= max_findings:
            truncated = True
            break
        try:
            content = safe_read_text(repo_root, target_key, max_size_bytes=1_000_000)
        except Exception:
            # The module can't be read (deleted, oversized, unsafe path). Without
            # its current export set the existence check can't run for it -- skip
            # rather than guess a break.
            continue
        current, open_set = _current_export_names(content)
        broken = index.broken_importers(target_key, current)
        if not broken:
            continue
        for name in sorted(broken):
            if len(high) >= max_findings:
                truncated = True
                break
            importers = broken[name]
            # High confidence requires the closed export set (a star re-export
            # could still expose the name) AND at least one importer that still
            # references the binding by the cheap presence check.
            live_sites = [
                imp
                for imp in importers
                if _name_still_referenced(repo_root, imp.path, name, imp.line)
            ]
            high_confidence = (not open_set) and bool(live_sites)
            # When the export set is closed, the still-referencing sites are the
            # broken call sites; when it is open, no site is asserted broken, so
            # report the recorded importers for transparency only.
            sites_src = live_sites if high_confidence else importers
            sites_sorted = sorted(
                sites_src, key=lambda imp: (imp.path, imp.line if imp.line is not None else -1)
            )
            sites = [
                {"path": _sanitize(imp.path), "line": imp.line} for imp in sites_sorted[:max_sites]
            ]
            finding = {
                "symbol": _sanitize(name),
                "module": _sanitize(target_key),
                "count": len(sites_src),
                "high_confidence": high_confidence,
                "sites": sites,
            }
            if high_confidence:
                high.append(finding)
            elif len(low) < low_cap:
                low.append(finding)
            else:
                low_dropped += 1

    return _envelope(
        {"found": True, "findings": high + low, "low_confidence_dropped": low_dropped},
        truncated=truncated,
    )


def refute_finding(repo: str, findings: list, base_ref: str = "main") -> dict:
    """Round-3 independent refutation of model-judgment review findings.

    Spawns one hardened claude -p refuter per finding (engine-owned, no tools,
    CHAMELEON_DISABLE=1), capped at REFUTER_MAX_SPAWNS_PER_INVOCATION with a
    concurrency ceiling. Returns one verdict per finding: confirmed | refuted |
    unverified. Default-ON; CHAMELEON_REVIEW_REFUTER=0 disables. Fails open to
    refuter='unavailable' (every finding -> unverified) on any error; never
    crashes, never invents a verdict, never authorizes an edit or post.
    """
    import os

    from chameleon_mcp import refuter as _refuter
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    def _all_unverified(reason):
        return [
            {"id": str(f.get("id", "")), "verdict": "unverified", "reason": reason}
            for f in (findings or [])
            if isinstance(f, dict)
        ]

    if os.environ.get("CHAMELEON_REVIEW_REFUTER") == "0":
        return _envelope({"refuter": "disabled", "verdicts": []})

    if not isinstance(findings, list) or not findings:
        return _envelope({"refuter": "enabled", "verdicts": []})

    repo_root, repo_id = _resolve_repo_arg(repo)
    if repo_root is None and repo_id is not None:
        repo_root = _resolve_repo_root_by_id(repo_id)
    if repo_root is None or not repo_root.is_dir():
        return _envelope({"refuter": "unavailable", "verdicts": _all_unverified("repo unresolved")})

    expected_repo_id = _compute_repo_id(repo_root)
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        return _envelope({"refuter": "untrusted", "verdicts": _all_unverified("profile untrusted")})

    if not _refuter.refuter_available():
        return _envelope(
            {"refuter": "unavailable", "verdicts": _all_unverified("claude CLI unavailable")}
        )

    model = os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
    timeout = threshold_int("REFUTER_TIMEOUT_SECONDS")
    max_spawns = threshold_int("REFUTER_MAX_SPAWNS_PER_INVOCATION")
    # Prefetch an inlined excerpt for each finding so the no-tools refuter
    # never reads files at spawn time. Fails open to "" per finding.
    excerpts = [_refuter_excerpt_for(repo_root, f, base_ref) for f in findings]
    try:
        verdicts = _refuter.run_batch(
            repo_root,
            findings,
            excerpts,
            model=model,
            timeout=timeout,
            max_spawns=max_spawns,
        )
    except Exception:
        return _envelope(
            {"refuter": "unavailable", "verdicts": _all_unverified("refuter batch failed")}
        )
    return _envelope({"refuter": "enabled", "verdicts": verdicts})


def _git_branch_diff(repo_root: Path, base_ref: str, rel_path: str | None = None) -> str:
    """`git diff <base_ref>...HEAD [-- <rel_path>]` text, capped, fail-open to ''."""
    args = ["git", "-C", str(repo_root), "diff", f"{base_ref}...HEAD"]
    if rel_path:
        args += ["--", rel_path]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "")[:8000]
    except Exception:
        return ""


def _refuter_excerpt_for(repo_root: Path, finding: dict, base_ref: str) -> str:
    """Prefetch the inlined excerpt for one finding (base_ref...HEAD scoped).

    Anchored (file + line present): returns a ~50-line window around the
    reported line from disk. File present but no line: returns the
    base_ref...HEAD diff for that file, falling back to the first ~200 lines
    from disk. Truly anchorless (no file): returns the whole base_ref...HEAD
    branch diff. Fails open to "" on any error.
    """
    try:
        from chameleon_mcp.safe_open import UnsafeFileError, safe_read_text

        line = finding.get("line")
        path = finding.get("file")
        if path:
            try:
                content = safe_read_text(repo_root, path, max_size_bytes=1_000_000)
            except (UnsafeFileError, FileNotFoundError):
                # Path failed safety check or doesn't exist; fall through to diff.
                content = None
            if content is not None:
                lines = content.splitlines()
                if line:
                    lo = max(0, int(line) - 25)
                    hi = min(len(lines), int(line) + 25)
                    return "\n".join(lines[lo:hi])
                # File-anchored but no line: return the branch diff for this file.
                diff = _git_branch_diff(repo_root, base_ref, path)
                if diff:
                    return diff
                return "\n".join(lines[:200])
        # Truly anchorless: return the whole branch diff.
        return _git_branch_diff(repo_root, base_ref)
    except Exception:
        return ""


def _candidate_body_excerpt(repo_root: Path, rel_path: str, name: str, max_lines: int) -> str:
    """Read a short body excerpt for a cataloged function from disk, or "".

    The catalog stores no body (the dump scripts emit no line spans alongside a
    callable's name), so the candidate's source is read at query time -- the same
    "read the witness body when asked" pattern the canonical excerpt uses. Finds
    the first line whose text declares ``name`` (a ``def``/``function``/``const``
    /``=>`` /method form) and returns that line plus up to ``max_lines`` after
    it. This is a citation aid for the LLM judge, not a parse; an imperfect
    excerpt simply gives the judge a few less-precise lines. Fails open to "" on
    any unsafe path or read error.
    """
    from chameleon_mcp.safe_open import safe_read_text

    try:
        content = safe_read_text(repo_root, rel_path, max_size_bytes=1_000_000)
    except Exception:
        return ""
    lines = content.splitlines()
    needle = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    decl_hint = re.compile(r"\b(def|function|class|const|let|var)\b|=>|=\s*(async\s+)?\(")
    start = None
    for i, line in enumerate(lines):
        if needle.search(line) and decl_hint.search(line):
            start = i
            break
    if start is None:
        return ""
    excerpt = "\n".join(lines[start : start + max_lines])
    return excerpt


def parse_edited_functions(repo_root, file_path: str) -> list[ParsedFn]:
    """Parse one edited file into ParsedFn rows (name, kind, arity, required, span, hashes, excerpt).

    Shares the exact parse get_duplication_candidates uses; the only additions are
    the start/end line span and a bounded body excerpt. Returns [] on any parse
    error. Entries whose dump predates span recording are included with None
    start/end lines and None hashes so get_duplication_candidates can still build
    NewFunction rows for name-token matching.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.bootstrap.orchestrator import resolve_extractor
    from chameleon_mcp.function_catalog import (
        ParsedFn,
        _lang_from_path,
        _param_names,
        _signature_shape,
        normalized_body_hash,
    )

    p = Path(file_path)
    try:
        query_lines = p.read_bytes()[:1_000_000].decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []

    try:
        extractor = resolve_extractor(Path(repo_root))
    except Exception:
        return []
    if extractor is None:
        return []

    try:
        parse_result = extractor.parse_repo(Path(repo_root), paths=[p])
    except Exception:
        return []

    query_lang = _lang_from_path(str(p))
    excerpt_cap = threshold_int("DUPLICATION_BODY_EXCERPT_LINES")
    out: list[ParsedFn] = []
    for pf in parse_result.files or ():
        extras = getattr(pf, "extras", None) or {}
        raw = extras.get("callable_signatures")
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            params = entry.get("params")
            arity, required = _signature_shape(params)
            start = entry.get("start_line")
            end = entry.get("end_line")
            has_span = isinstance(start, int) and isinstance(end, int)
            body_hash = normalized_body_hash(query_lines, start, end) if has_span else None
            body_hash_pnorm = (
                normalized_body_hash(
                    query_lines,
                    start,
                    end,
                    param_names=_param_names(params),
                    language=query_lang,
                )
                if has_span
                else None
            )
            excerpt = ""
            if has_span:
                # start_line is 1-based; the slice must include the signature
                # line or the judge sees a headless body.
                body = query_lines[max(start - 1, 0) : min(end, len(query_lines))]
                excerpt = "\n".join(body[:excerpt_cap])
            kind = entry.get("kind")
            out.append(
                ParsedFn(
                    name=name,
                    kind=kind if isinstance(kind, str) else "function",
                    arity=arity,
                    required=required,
                    start_line=start if has_span else None,
                    body_hash=body_hash,
                    body_hash_pnorm=body_hash_pnorm,
                    excerpt=excerpt,
                    end_line=end if has_span else None,
                )
            )
    return out


def get_duplication_candidates(repo: str, file_path: str) -> dict:
    """Candidate existing functions a file's new functions may re-implement.

    The cross-file duplication read backing PR-review and the turn-end judge. For
    each function the file at ``file_path`` defines, the bootstrap function
    catalog is prefiltered (signature shape + name-token overlap) to the handful
    of existing functions elsewhere in the repo that look like the same intent
    under a different name -- the ``toDisplayDate`` vs ``formatDate`` case the
    flat exact-name signal cannot see. Each surfaced candidate carries a short
    body excerpt read from disk so the caller can cite it.

    The tool only PREFILTERS. The LLM caller judges semantic equivalence against
    the candidate bodies; a returned candidate is a place to look, never a
    confirmed duplicate. Duplication is a judgment call, so any finding the
    caller raises from this is advisory FIX/NIT at most -- never block-eligible.

    Returns ``{"found": bool, "file": str|None, "matches": [...]}`` where each
    match is ``{"function": {name, kind, arity, required}, "candidates":
    [{name, file, kind, arity, required, shared_tokens, body_excerpt}, ...]}``.
    Fails open with ``found: False`` on any ambiguity (unresolvable / untrusted
    repo, missing catalog, unparsable file). Never fabricates a candidate -- each
    is a function the bootstrap recorded.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.function_catalog import (
        NewFunction,
        load_function_catalog,
        select_candidates,
    )
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    empty = {"found": False, "file": None, "matches": []}

    if not _validate_file_path_arg(file_path):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(dict(empty))

    # The repo arg must agree with the file's own repo, mirroring the other
    # cross-file reads (a mismatched repo id must not read another repo's
    # catalog).
    expected_repo_id = _compute_repo_id(repo_root)
    if isinstance(repo, str) and _REPO_ID_RE.match(repo):
        if repo != expected_repo_id:
            return _envelope(dict(empty))
    else:
        _resolved_path, resolved_repo_id = _resolve_repo_arg(repo)
        if resolved_repo_id is None or resolved_repo_id != expected_repo_id:
            return _envelope(dict(empty))

    # Trust-gate: the catalog is an attacker-controllable committed artifact, so
    # its function names and paths must not reach the model surface from an
    # untrusted profile (mirrors the other read tools).
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    catalog = load_function_catalog(repo_root)
    if catalog is None or not len(catalog):
        return _envelope(dict(empty))

    try:
        file_rel = p.resolve().relative_to(repo_root).as_posix()
    except (ValueError, OSError):
        file_rel = None

    # Parse the edited file's own functions through the same extractor the
    # bootstrap used, so the new functions carry the same callable_signatures
    # shape the catalog was built from. parse_edited_functions applies the
    # workspace-monorepo fallback via resolve_extractor and returns [] on any
    # parse or extractor failure (which the empty-result path below handles).
    parsed_fns = parse_edited_functions(repo_root, str(p))

    # Map ParsedFn -> NewFunction, deduplicating overload sets on (name, arity,
    # required) exactly as the original loop did.
    new_functions: list[NewFunction] = []
    seen: set[tuple[str, int, int]] = set()
    for fn in parsed_fns:
        key = (fn.name, fn.arity, fn.required)
        if key in seen:
            continue
        seen.add(key)
        new_functions.append(
            NewFunction(
                name=fn.name,
                kind=fn.kind,
                arity=fn.arity,
                required=fn.required,
                body_hash=fn.body_hash,
                body_hash_pnorm=fn.body_hash_pnorm,
            )
        )

    if not new_functions:
        out = dict(empty)
        out["found"] = True
        out["file"] = _sanitize(file_rel) if file_rel else None
        return _envelope(out)

    matches = select_candidates(catalog, new_functions, exclude_file=file_rel)

    # Bound the response: a large file (hundreds of functions, each with up to
    # DUPLICATION_MAX_CANDIDATES_PER_FN excerpts) would otherwise exceed the MCP
    # token cap and return nothing usable. Keep the first N matches (already
    # ranked by select_candidates) and flag the truncation so the caller knows
    # the list is partial rather than complete.
    max_matches = threshold_int("DUPLICATION_MAX_MATCHES")
    total_matches = len(matches)
    truncated = total_matches > max_matches
    if truncated:
        matches = matches[:max_matches]

    excerpt_lines = threshold_int("DUPLICATION_BODY_EXCERPT_LINES")
    for match in matches:
        fn = match["function"]
        fn["name"] = _sanitize(fn["name"])
        for cand in match["candidates"]:
            cand["body_excerpt"] = _sanitize(
                _candidate_body_excerpt(repo_root, cand["file"], cand["name"], excerpt_lines)
            )
            cand["name"] = _sanitize(cand["name"])
            cand["file"] = _sanitize(cand["file"])
            cand["shared_tokens"] = [_sanitize(t) for t in cand.get("shared_tokens", [])]

    out = {
        "found": True,
        "file": _sanitize(file_rel) if file_rel else None,
        "matches": matches,
    }
    if truncated:
        out["truncated"] = True
        out["truncated_matches"] = total_matches - max_matches
    return _envelope(out)


def get_drift_status(repo: str) -> dict:
    """Report freshness for a repo.

    Computes:
    - days_since_refresh from the trust record's granted_at
    - observed_drift_score from drift.db's recent edit_observations
      (None if no observations yet)
    - structural_conformance_score: the SAME value as observed_drift_score by
      design — an honest alias kept alongside the legacy name (see the inline
      comment at the return), not an independent metric
    - recommended_action: combines both signals; always a sentence, never a
      bare token

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
        # An opaque id whose single component exceeds NAME_MAX makes the
        # plugin_data_dir()/repo_id is_dir() probe below raise ENAMETOOLONG.
        if len(repo_id.encode("utf-8", "surrogatepass")) > _NAME_MAX_BYTES:
            return _envelope(
                {"status": "failed", "error": "expected repo path or repo_id hex digest"}
            )

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

    # A pre-current schema_version means the clustering algorithm changed
    # underneath the profile, which the loader accepts silently (only a NEWER
    # schema is rejected). In practice an old schema rides along with an old
    # engine stamp, so the engine branch above catches it first — this check
    # closes the remaining gap (hand-edited or partially-migrated profiles).
    schema_outdated = False
    if resolved_path is not None:
        try:
            from chameleon_mcp.profile.schema import CURRENT_SCHEMA_VERSION

            _declared = json.loads(
                (resolved_path / ".chameleon" / "profile.json").read_text(encoding="utf-8")
            ).get("schema_version")
            schema_outdated = isinstance(_declared, int) and _declared < CURRENT_SCHEMA_VERSION
        except (OSError, ValueError):
            schema_outdated = False

    # Production-pinned freshness: when a production_ref lock exists, compare
    # the profile's recorded derivation SHA with the locked ref's current tip
    # (the LOCAL ref — current as of the user's last fetch; no network).
    production_block: dict | None = None
    production_tip_moved = False
    if resolved_path is not None:
        try:
            from chameleon_mcp.production_ref import resolve_production_ref

            _prod_branch = _persisted_production_ref(resolved_path)
            if _prod_branch:
                _prod_resolved = resolve_production_ref(resolved_path, _prod_branch)
                _recorded = _recorded_derivation_sha(resolved_path / ".chameleon")
                production_block = {
                    "branch": _prod_branch,
                    "ref": _prod_resolved.ref if _prod_resolved else None,
                    "tip_sha": _prod_resolved.sha if _prod_resolved else None,
                    "derived_sha": _recorded,
                    "resolvable": _prod_resolved is not None,
                }
                if _prod_resolved is not None and _recorded != _prod_resolved.sha:
                    production_tip_moved = True
                    production_block["tip_moved"] = True
                    # Always present when tip_moved — null when the old commit
                    # is unreachable (gc'd) or the count fails.
                    production_block["commits_ahead"] = None
                    try:
                        _count = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(resolved_path),
                                "rev-list",
                                "--count",
                                f"{_recorded}..{_prod_resolved.sha}",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=2,
                            check=False,
                        )
                        if _count.returncode == 0 and _count.stdout.strip().isdigit():
                            production_block["commits_ahead"] = int(_count.stdout.strip())
                    except (subprocess.TimeoutExpired, OSError):
                        pass
        except Exception:  # noqa: BLE001
            production_block = None

    if engine_version_mismatch:
        recommended = "engine upgraded since this profile was built; run /chameleon-refresh"
    elif schema_outdated:
        recommended = (
            "profile predates the current clustering schema; run /chameleon-refresh to re-derive"
        )
    elif days_since_refresh is None:
        # Only a resolvable path lets us distinguish "no profile" (run init) from
        # "untrusted profile" (run trust). A bare repo_id has no path to inspect
        # and was seen before, so keep the trust wording.
        profile_absent = (
            resolved_path is not None
            and not (resolved_path / ".chameleon" / "profile.json").is_file()
        )
        if profile_absent:
            recommended = "no profile found; run /chameleon-init first"
        else:
            recommended = "no trust grant found; run /chameleon-trust first"
    elif production_tip_moved:
        _ahead = (production_block or {}).get("commits_ahead")
        _ahead_txt = f" ({_ahead} commit(s) ahead)" if isinstance(_ahead, int) else ""
        recommended = (
            f"production branch {(production_block or {}).get('ref')} moved past the "
            f"profile's derivation commit{_ahead_txt}; run /chameleon-refresh"
        )
    elif drift_score is not None and drift_score > 0.5:
        recommended = f"observed drift is high ({drift_score:.2f}); run /chameleon-refresh"
    elif days_since_refresh > 90:
        recommended = "profile may be stale; run /chameleon-refresh"
    elif days_since_refresh > 30:
        recommended = "consider /chameleon-refresh if codebase has materially changed"
    else:
        # A full sentence like its siblings: the bare value "fresh" read as an
        # instruction ("refresh"?) rather than a status.
        recommended = "none; profile is fresh"

    # The drift score is structural-match confidence, not a correctness measure.
    # Expose it under an honest alias and attach the blind-spots disclaimer so a
    # caller cannot read "drift 0.08" as a quality bar. observed_drift_score is
    # kept for backward compatibility with existing callers.
    from chameleon_mcp.shadow_report import CONFORMANCE_DISCLAIMER, SIGNAL_BLIND_SPOTS

    drift_data = {
        "repo_id": repo_id,
        "days_since_refresh": days_since_refresh,
        "observed_drift_score": drift_score,
        "structural_conformance_score": drift_score,
        "is_quality_bar": False,
        "conformance_disclaimer": CONFORMANCE_DISCLAIMER,
        "blind_spots": list(SIGNAL_BLIND_SPOTS),
        "engine_version_mismatch": engine_version_mismatch,
        "schema_outdated": schema_outdated,
        "recommended_action": recommended,
    }
    if production_block is not None:
        drift_data["production_ref"] = production_block
    return _envelope(drift_data)


def _profile_requires_newer_engine(profile_dir: Path) -> str | None:
    """The profile's declared engine_min_version when this engine is older.

    Mirrors the loader's gate as a cheap peek for display paths (detect_repo,
    get_status) so they classify a too-new profile honestly instead of
    rendering its panels or reporting it corrupted. Returns None when the
    profile is readable by this engine or the peek fails (the load-bearing
    paths still enforce the gate).
    """
    from chameleon_mcp import __version__ as _engine_version
    from chameleon_mcp.profile.loader import _version_tuple

    try:
        peek = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = peek.get("engine_min_version") if isinstance(peek, dict) else None
    if (
        isinstance(declared, str)
        and declared
        and _version_tuple(_engine_version) < _version_tuple(declared)
    ):
        return declared
    return None


def _partition_block_rules(
    rules: dict, *, lang_inert, signal_inert
) -> tuple[list[str], list[dict]]:
    """Split persisted block-rule verdicts into the active set and the demoted
    list for /chameleon-status, attaching the reason a rule is not blocking.

    Two demotion axes are reported distinctly: capability ``inert_reason``
    (no language signal / missing convention data, including a stale active=True
    the read-time gates override) and the refresh-time ``demoted_reason``
    (``high-override-rate``) with its measured ``override_rate``, so a lead can
    tell "this rule cannot speak here" from "the team keeps overriding it".
    """
    active: list[str] = []
    demoted: list[dict] = []
    for rule, meta in rules.items():
        if not isinstance(meta, dict):
            continue
        li = lang_inert(rule)
        si = signal_inert(rule)
        if meta.get("active") is True and not li and not si:
            active.append(rule)
        else:
            entry = {"rule": rule, "fp_rate": meta.get("fp_rate")}
            reason = meta.get("inert_reason")
            if not reason and meta.get("active") is True:
                reason = "missing-convention-data" if si else "no-signal-for-language"
            if reason:
                entry["inert_reason"] = reason
            if meta.get("demoted_reason"):
                entry["demoted_reason"] = meta["demoted_reason"]
            if meta.get("override_rate") is not None:
                entry["override_rate"] = meta["override_rate"]
            demoted.append(entry)
    active.sort()
    demoted.sort(key=lambda d: d["rule"])
    return active, demoted


def _block_precision_summary(block_rules: dict, active: list[str]) -> dict:
    """Headline calibration-precision number for /chameleon-status.

    Every active block rule is certified by calibration to flag at most
    ``fp_epsilon`` of the repo's OWN committed files: that measured
    false-positive ceiling is chameleon's low-noise design, proven with a
    number rather than asserted. This aggregates the active rules' per-rule
    fp_rate into a single surfaced summary (count of active block rules, the
    files sampled, and the max / mean false-positive rate across them). An empty
    active set reports zeros rather than crashing.
    """
    fps: list[float] = []
    sampled = 0
    for rule in active:
        meta = block_rules.get(rule)
        if not isinstance(meta, dict):
            continue
        fr = meta.get("fp_rate")
        if isinstance(fr, (int, float)):
            fps.append(float(fr))
        s = meta.get("sampled")
        if isinstance(s, int):
            sampled = max(sampled, s)
    return {
        "active_block_rules": len(active),
        "sampled_files": sampled,
        "max_fp_rate": round(max(fps), 4) if fps else 0.0,
        "mean_fp_rate": round(sum(fps) / len(fps), 4) if fps else 0.0,
    }


def _collect_demotion_proposals(rules: dict) -> list[dict]:
    """Pending override-driven demotion proposals recorded in enforcement.json."""
    out: list[dict] = []
    for rule, meta in rules.items():
        if not isinstance(meta, dict):
            continue
        prop = meta.get("demotion_proposed")
        if isinstance(prop, dict):
            out.append({"rule": rule, **prop})
    out.sort(key=lambda d: d["rule"])
    return out


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
    - ``proposed_demotions`` (when non-empty) — rules whose override pressure
      crossed the demotion bar without multi-session evidence (or that are
      security-class), still blocking, awaiting a human decision.
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

    # A 64-hex repo_id that maps to no known repo must not be treated as a
    # relative path: find_repo_root would walk up from the CWD and report some
    # OTHER repo's enforcement state under the bogus id. Resolve ids via the
    # index and signal no_repo when unknown, like the path branch does.
    if _REPO_ID_RE.match(repo):
        repo_root = _resolve_repo_root_by_id(repo)
    else:
        try:
            repo_root = find_repo_root(Path(repo).expanduser())
        except (OSError, ValueError):
            repo_root = None
    if repo_root is None:
        return _envelope({"status": "no_repo"})

    profile_dir = _effective_profile_dir(repo_root)

    # A profile written by a newer engine carries verdicts this engine cannot
    # interpret; rendering its enforcement panel would describe guarantees
    # this engine does not honor. Refuse like the load-bearing read paths do.
    too_new = _profile_requires_newer_engine(profile_dir)
    if too_new is not None:
        return _envelope(
            {
                "status": "profile_too_new",
                "error": (
                    f"profile requires engine >= {too_new}; this engine is older. "
                    "Upgrade chameleon to read this profile."
                ),
            }
        )

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

    from chameleon_mcp.enforcement_calibration import (
        rule_inert_for_language,
        rule_inert_missing_signal,
    )

    # A profile calibrated by an older engine can carry active=True for a rule
    # that has no signal source in this profile's language, or whose driving
    # convention data was never derived; the read-time gates keep that stale
    # verdict out of the active list (the enforcement path applies the same
    # gates) until a refresh recalibrates.
    block_rules = load_block_rules(profile_dir)
    active, demoted = _partition_block_rules(
        block_rules,
        lang_inert=lambda r: rule_inert_for_language(r, profile_dir),
        signal_inert=lambda r: rule_inert_missing_signal(r, profile_dir),
    )

    # Live override-rate section. bootstrap fp_rate (above, calibration against
    # committed files) and the override rate (here, team contention on real AI
    # edits) are two distinct axes, not the same number: a rule can read
    # fp_rate=0.000 and still be overridden on most edits. Fail-open: a missing
    # drift.db / metrics log degrades to no section rather than crashing status.
    overrides = None
    try:
        from chameleon_mcp.review_ledger import build_override_audit

        _repo_path, repo_id = _resolve_repo_arg(repo)
        if repo_id is not None:
            overrides = build_override_audit(repo_id)
    except Exception:
        overrides = None

    enforcement = {
        "mode": mode,
        "active": active,
        "demoted": demoted,
        "idiom_review": idiom_review,
        "idiom_judge": idiom_judge,
        # Headline calibration-precision: the measured false-positive ceiling the
        # active block rules clear against this repo's own committed files.
        "precision": _block_precision_summary(block_rules, active),
    }
    # A pending proposal means the rule is still blocking; it is reported on
    # its own axis so a lead can act on the override evidence deliberately.
    proposed = _collect_demotion_proposals(block_rules)
    if proposed:
        enforcement["proposed_demotions"] = proposed
    if overrides is not None:
        enforcement["overrides"] = overrides

    # PR-review ledger section. Distinct axis from enforcement: this is the
    # persisted trail of /chameleon-pr-review verdicts, surfaced so a lead can
    # spot a BLOCK verdict that was merged anyway. Tamper-evident only (the
    # signing key is local to the reviewed developer), so it is an honest audit
    # trail, not a merge authority. Fail-open: a missing/unreadable ledger
    # degrades to no section rather than crashing status.
    review_ledger = None
    try:
        from chameleon_mcp.review_ledger import build_review_ledger_panel

        _rl_path, rl_repo_id = _resolve_repo_arg(repo)
        if rl_repo_id is not None:
            review_ledger = build_review_ledger_panel(rl_repo_id)
    except Exception:
        review_ledger = None

    # Degraded-delivery section. A separate axis again: how often chameleon's
    # guidance silently failed to reach the session (no interpreter, a crashed
    # spawn, or an in-process advisor failure). The events are already persisted
    # (.hook_errors.log + metrics.jsonl fail_open); this is the first surface that
    # counts them. Fail-open: a missing/corrupt log degrades to no section.
    degraded = None
    try:
        from chameleon_mcp._thresholds import threshold_int
        from chameleon_mcp.degraded_telemetry import read_degraded_summary

        degraded = read_degraded_summary(threshold_int("DEGRADED_WINDOW_DAYS"))
    except Exception:
        degraded = None

    out: dict = {"enforcement": enforcement}
    if review_ledger is not None:
        out["review_ledger"] = review_ledger
    if degraded is not None:
        out["degraded"] = degraded
    return _envelope(out)


def get_drift_antipatterns(repo: str, archetype: str | None = None) -> dict:
    """Per-archetype recurring-violation signals from this repo's drift history.

    For each archetype where edits repeatedly bumped a convention
    (``rule_overrides``) or drifted off-pattern (``decision_log``), returns the
    rule(s) and frequency so a deriver (e.g. ``/chameleon-auto-idiom``) can propose
    a counterexample-bearing idiom. Carries NO wrong-way code -- drift.db stores
    none -- so the deriver reads a flagged file to write the off-pattern form
    itself. Optionally filter to one ``archetype``. Fail-open: an unresolvable repo
    or any read error returns an empty result rather than raising.
    """
    from chameleon_mcp._thresholds import threshold_int

    window_days = threshold_int("DRIFT_ANTIPATTERN_WINDOW_DAYS")
    _repo_path, repo_id = _resolve_repo_arg(repo)
    archetypes: dict[str, dict] = {}
    if repo_id is not None:
        try:
            from chameleon_mcp.drift.observations import archetype_antipattern_signals

            archetypes = archetype_antipattern_signals(
                repo_id,
                window_days=window_days,
                min_count=threshold_int("DRIFT_ANTIPATTERN_MIN_COUNT"),
                max_rules_per_archetype=threshold_int("DRIFT_ANTIPATTERN_MAX_RULES"),
            )
        except Exception:
            archetypes = {}
    if archetype is not None:
        archetypes = {k: v for k, v in archetypes.items() if k == archetype}
    return _envelope({"window_days": window_days, "archetypes": archetypes})


def get_shadow_report(repo: str, window_days: int | None = None) -> dict:
    """Aggregate the shadow-mode would_block log for the shadow -> enforce decision.

    Reads the accumulating real-edit metrics record (not the one-shot
    bootstrap-time calibration ``get_status`` returns) for this repo over the
    last ``window_days``. Reports, per block rule: how often it would have fired,
    on how many distinct files and sessions, an advisory-only count, and a
    promotion verdict by would-block COUNT (``safe_to_enforce`` only when zero
    would-blocks across enough edits in a non-truncated window). The
    idiom/principle review gate has no single rule, so it is reported as a
    separate turn-level counter, not a promotion candidate.

    A ``sample`` of file:line+rule is returned for human spot-check: the report
    never computes a false-positive fraction because the metric rows carry no
    accept/override/fix outcome signal, so whether a would-block was genuine
    off-pattern code stays a human judgement on the sample.

    ``window_truncated`` is True when log rotation dropped rows older than the
    window, so the counts cannot claim full coverage of the requested period.

    Fail-open: a missing/unreadable metrics log degrades to an empty report
    rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo"})

    try:
        from chameleon_mcp.shadow_report import build_shadow_report

        report = build_shadow_report(repo_id, window_days)
    except Exception:
        # A corrupt or unreadable metrics log should not crash the status call;
        # report an empty window rather than raising.
        report = {
            "repo_id": repo_id,
            "window_days": window_days,
            "window_truncated": False,
            "total_edits": 0,
            "rules": {},
            "idiom_review": {"would_blocks": 0},
            "sample": [],
        }
    return _envelope(report)


def get_override_audit(repo: str, window_days: int | None = None) -> dict:
    """Per-rule inline-override audit for the override-rate decision.

    Reads the durable drift.db override history (which refresh does NOT wipe)
    plus the would-block metrics, and reports, per block rule: how often it was
    overridden via inline ``chameleon-ignore``, how often it would have blocked,
    the bare-blanket-directive share, and an override rate (overrides / fired
    edits). A high rate flags a rule that is fighting the team -- the convention
    is wrong (``/chameleon-teach``) or the rule is miscalibrated (reconcile via
    ``/chameleon-refresh``, which rewrites the verdict before the trust hash).

    This is a contention signal, not a false-positive rate: an override can mean
    the rule is wrong OR that this was a documented intentional deviation. The
    audit only surfaces; it never auto-demotes a trust-hashed enforcement rule.

    Fail-open: a missing drift.db / unreadable metrics log degrades to an empty
    audit rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo"})

    try:
        from chameleon_mcp.review_ledger import build_override_audit

        audit = build_override_audit(repo_id, window_days)
    except Exception:
        audit = {
            "repo_id": repo_id,
            "window_days": window_days,
            "total_overrides": 0,
            "rules": {},
            "flagged": [],
        }
    return _envelope(audit)


def get_longitudinal_signals(repo: str, window_days: int | None = None) -> dict:
    """The two honestly-labelled longitudinal health tracks for a repo.

    A lead deciding whether AI code is staying healthy has historically had one
    trailing number, the drift score, which measures structural mimicry, not
    correctness. This returns both signals chameleon records, each labelled for
    what it measures:

    - ``structural_conformance`` — the drift score relabelled (1 - mean
      structural-match confidence), explicitly NOT a quality bar.
    - ``enforcement_outcomes`` — aggregate would-block / idiom-review rates over
      the window, counting how often chameleon's own shape/idiom rules fired.

    Both carry the ``blind_spots`` / ``disclaimer`` caveat so an all-zeros result
    is not read as a correctness guarantee. The blind-spot classes (logic,
    dataflow, cross-file, auth) are exactly what neither track sees.

    Fail-open: a missing drift.db / unreadable metrics log degrades the affected
    track to None / zeros rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo"})

    try:
        from chameleon_mcp.shadow_report import build_longitudinal_signals

        signals = build_longitudinal_signals(repo_id, window_days)
    except Exception:
        from chameleon_mcp.shadow_report import CONFORMANCE_DISCLAIMER, SIGNAL_BLIND_SPOTS

        signals = {
            "repo_id": repo_id,
            "window_days": window_days,
            "blind_spots": list(SIGNAL_BLIND_SPOTS),
            "disclaimer": CONFORMANCE_DISCLAIMER,
            "structural_conformance": None,
            "enforcement_outcomes": {
                "total_edits": 0,
                "would_block_edits": 0,
                "idiom_review_blocks": 0,
                "block_rate": None,
                "idiom_review_rate": None,
                "window_truncated": False,
            },
        }
    return _envelope(signals)


def get_review_history(
    repo: str, limit: int | None = None, include_attestations: bool = False
) -> dict:
    """Return the persisted PR-review verdict trail for a repo, newest first.

    Once human review is optional, ``/chameleon-pr-review`` is the system of
    record for "this change was checked", but the skill is chat-only and
    persists nothing. The ledger fills that hole: each review run appends a
    signed record pinning the reviewed commit, the exact profile that reviewed
    it (``profile_sha256`` + generation + schema_version), the trust state, the
    verdict, a findings-by-severity summary, the engine version, and the
    reviewer. This read lets a lead see the trail and answer "which BLOCK
    verdicts shipped anyway".

    Each returned record carries ``verified`` -- whether its HMAC still matches.
    That is tamper-EVIDENCE against a third local user silently editing a line;
    it is NOT forgery resistance against the reviewed developer (who holds the
    signing key) and CI cannot verify these records (no shared key). So treat
    the trail as an honest self-attested audit log, not a merge authority.

    ``include_attestations`` additionally returns the most-recent session
    attestations (``attestations`` key) from the sibling ledger. The
    attestation is self-signed and raise-only: nothing recorded in it may ever
    lower scrutiny anywhere downstream. A consumer may use it only to RAISE
    gate depth (skipped checks, degraded spawns, ungoverned files, disable
    windows escalate) and to make post-incident replay honest. The merge
    gate's floor is computed from diff facts alone and trusts none of this
    without re-verification; a forged-clean attestation therefore buys
    nothing. Default off so existing callers' payloads are unchanged.

    ``limit`` defaults to ``CHAMELEON_REVIEW_HISTORY_DEFAULT_LIMIT``. Fail-open:
    a missing/unreadable ledger returns an empty history rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        # Carry the same data keys as the healthy shape (empty) so a consumer
        # parses one schema regardless of repo existence.
        return _envelope(
            {"status": "no_repo", "repo_id": None, "records": [], "total": 0, "unverified": 0}
        )

    try:
        from chameleon_mcp.review_ledger import read_review_history

        history = read_review_history(repo_id, limit)
    except Exception:
        history = {"repo_id": repo_id, "records": [], "total": 0, "unverified": 0}
    if include_attestations:
        try:
            from chameleon_mcp.review_ledger import read_session_attestations

            history["attestations"] = read_session_attestations(repo_id, limit=10)["records"]
        except Exception:
            history["attestations"] = []
    return _envelope(history)


def _peek_profile_provenance(repo_root: Path | None, repo_id: str | None) -> dict:
    """Best-effort profile provenance for a review-ledger record.

    Reads profile.json once to pin which knowledge base reviewed the code:
    ``profile_sha256`` (the trusted hash for this root, from the trust record),
    ``generation`` and ``schema_version`` (from the profile), and ``trust_state``
    at review time. Every field degrades to None on any read failure so the
    record still writes; provenance is enrichment, not a precondition.
    """
    out: dict = {
        "profile_sha256": None,
        "generation": None,
        "schema_version": None,
        "trust_state": None,
    }
    if repo_root is None or repo_id is None:
        return out

    profile_dir = repo_root / ".chameleon"
    profile_file = profile_dir / "profile.json"
    try:
        with profile_file.open("r", encoding="utf-8") as fh:
            peek = json.load(fh)
        if isinstance(peek, dict):
            gen = peek.get("generation")
            out["generation"] = gen if isinstance(gen, int) else None
            sv = peek.get("schema_version")
            out["schema_version"] = sv if isinstance(sv, int) else None
    except (OSError, ValueError):
        pass

    try:
        from chameleon_mcp.profile.trust import is_material_change, trust_state_for

        trust = trust_state_for(repo_id)
        if trust is None or not trust.grants_root(profile_dir.parent):
            out["trust_state"] = "untrusted"
        elif is_material_change(repo_id, profile_dir):
            out["trust_state"] = "stale"
        else:
            out["trust_state"] = "trusted"
        if trust is not None:
            sha = trust.hash_for_root(profile_dir.parent)
            out["profile_sha256"] = sha or None
    except Exception:
        pass

    return out


def record_review_verdict(
    repo: str,
    verdict: str,
    findings_count: int | None = None,
    commit_sha: str | None = None,
    pr_id: str | None = None,
    complexity_tier: str | None = None,
) -> dict:
    """Append one PR-review verdict to the repo's signed review ledger.

    The final step of ``/chameleon-pr-review``: after the verdict is shown in
    chat, this persists it so a lead can later audit which reviewed commits
    shipped and whether any BLOCK verdict merged anyway. ``findings_count`` is
    the total BLOCK+FIX+NIT count the run produced; it is stored under a
    ``total`` severity bucket. Profile provenance (``profile_sha256`` +
    generation + schema_version), the trust state at review time, the engine
    version, and the reviewing user are stamped here so the record pins exactly
    which knowledge base reviewed the code.

    Best-effort and never blocks the review: any failure (no repo, unwritable
    ledger) returns ``recorded: False`` rather than raising. The ledger is
    tamper-evident against a third local user, NOT forgery-proof against the
    reviewed developer (who holds the signing key) and NOT CI-verifiable. Read
    it back with ``get_review_history``.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )
    if not isinstance(verdict, str) or not verdict.strip():
        return _envelope({"status": "failed", "error": "expected a verdict string"})

    repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo", "recorded": False})

    findings: dict | None = None
    if findings_count is not None:
        try:
            findings = {"total": int(findings_count)}
        except (TypeError, ValueError):
            findings = None

    provenance = _peek_profile_provenance(repo_path, repo_id)

    try:
        from chameleon_mcp import __version__ as engine_version
    except Exception:
        engine_version = None

    try:
        from chameleon_mcp.review_ledger import record_review

        record = record_review(
            repo_id,
            commit_sha=commit_sha,
            verdict=verdict,
            findings=findings,
            profile_sha256=provenance["profile_sha256"],
            generation=provenance["generation"],
            schema_version=provenance["schema_version"],
            trust_state=provenance["trust_state"],
            engine_version=engine_version,
            pr_id=pr_id,
            complexity_tier=complexity_tier,
        )
    except Exception as exc:
        return _envelope(
            {
                "status": "failed",
                "recorded": False,
                "error": f"review ledger unavailable: {type(exc).__name__}",
            }
        )

    return _envelope(
        {
            "status": "ok",
            "recorded": True,
            "signed": bool(record.get("hmac")),
            "record": record,
        }
    )


def _normalize_decision_rel_path(repo_path: Path | None, file_path: str) -> str:
    """Repo-relative posix path for a decision_log lookup.

    Mirrors how the hook keys the row (``relative_to`` the repo root, posix
    form). Accepts an absolute path, a ``~`` path, or an already-relative path;
    falls back to the basename when the path resolves outside the repo, and to
    the input as-is when no repo root is known.
    """
    raw = (file_path or "").strip()
    try:
        p = Path(raw).expanduser()
    except (OSError, ValueError):
        return raw
    if repo_path is not None:
        try:
            return p.resolve().relative_to(repo_path.resolve()).as_posix()
        except (ValueError, OSError):
            pass
        if not p.is_absolute():
            # An already-relative argument is taken verbatim — the hook stores
            # the posix repo-relative form, so a caller passing that form matches.
            return Path(raw).as_posix()
        return p.name
    return Path(raw).as_posix() if not p.is_absolute() else p.name


def explain_edit(repo: str, file_path: str) -> dict:
    """Replay what chameleon knew and did the last time a file was edited.

    The post-incident recovery read: returns the most-recent decision_log row
    for ``file_path`` and classifies the gate's silence so a postmortem can route
    the fix. ``classification`` is:

    - ``coverage-gap`` — no archetype matched, or it matched at fallback/none
      quality, so the per-edit lint never had a calibrated shape to check against.
      Route to ``/chameleon-refresh`` (re-derive archetypes) or ``/chameleon-teach``
      (capture the missing convention).
    - ``in-scope-miss`` — an ast/exact archetype matched and chameleon raised
      NOTHING on the edit that later broke. The shape was covered but no rule
      fired; route to a new rule / idiom rather than a refresh.
    - ``advised`` — an ast/exact archetype matched and chameleon raised advisory
      violations (or shadow-logged a would-block) but did not block. The rules
      fired; they were advisory. Route to enforce-mode calibration or a stronger
      rule, not a refresh. Kept distinct from ``in-scope-miss`` so a raised
      advisory is not misread as silence.
    - ``blocked`` / ``overridden`` — the gate did fire: it blocked, or a block was
      waved through with an inline ``chameleon-ignore``. Surfaced so a postmortem
      sees the gate was not silent.

    ``found`` is False when no edit of this file was ever logged (e.g. it was
    edited outside a chameleon session, or before this log existed). Fail-open: a
    missing drift.db or unreadable row degrades to ``found: False``.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope(
            {
                "status": "failed",
                "error": "expected repo path or repo_id hex digest",
            }
        )
    if not isinstance(file_path, str) or not file_path.strip():
        return _envelope({"status": "failed", "error": "expected a file path"})

    repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo"})

    rel_path = _normalize_decision_rel_path(repo_path, file_path)

    try:
        from chameleon_mcp.drift.observations import latest_decision

        decision = latest_decision(repo_id, rel_path)
    except Exception:
        decision = None

    if decision is None:
        return _envelope(
            {
                "repo_id": repo_id,
                "rel_path": rel_path,
                "found": False,
                "decision": None,
                "classification": None,
            }
        )

    match_quality = decision.get("match_quality")
    outcome = decision.get("outcome")
    try:
        violations_raised = int(decision.get("violations_raised") or 0)
    except (TypeError, ValueError):
        violations_raised = 0
    if outcome in ("blocked", "overridden"):
        classification = outcome
    elif match_quality in (None, "none", "fallback"):
        classification = "coverage-gap"
    elif violations_raised > 0:
        # The gate was not silent: it raised advisories (or shadow-logged a
        # would-block) but did not block. Not a miss — the rules fired, they were
        # advisory — so a postmortem routes this apart from a true in-scope miss.
        classification = "advised"
    else:
        classification = "in-scope-miss"

    return _envelope(
        {
            "repo_id": repo_id,
            "rel_path": rel_path,
            "found": True,
            "decision": decision,
            "classification": classification,
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

    # conventions.json (taught competing imports) and principles.md are
    # protocol files, so atomic_profile_commit will NOT copy them forward from
    # the live profile. The partial path doesn't regenerate them, so they must
    # be carried into the transaction verbatim or a successful partial refresh
    # silently wipes every taught banned import and the principles doc.
    conventions_text: str | None = None
    conventions_path_partial = profile_dir / "conventions.json"
    if conventions_path_partial.is_file():
        from chameleon_mcp.safe_open import (
            UnsafeFileError as _UnsafeFileErrorC,
        )
        from chameleon_mcp.safe_open import (
            safe_read_profile_artifact as _safe_read_profile_artifact_c,
        )

        try:
            conventions_text = _safe_read_profile_artifact_c(conventions_path_partial)
        except (OSError, FileNotFoundError, _UnsafeFileErrorC):
            conventions_text = None

    principles_text = ""
    principles_path = profile_dir / "principles.md"
    if principles_path.is_file():
        try:
            principles_text = principles_path.read_text(encoding="utf-8")
        except OSError:
            principles_text = ""

    # calls_index.json is a protocol file because a FAILED full rebuild must
    # drop it rather than serve stale judge facts. A partial refresh is not a
    # failed rebuild: the committed snapshot stays valid as "callers at last
    # derivation" (exactly like the non-protocol indexes the commit carries
    # forward on its own), so re-emit it verbatim instead of wiping it.
    calls_index_text: str | None = None
    calls_index_path_partial = profile_dir / "calls_index.json"
    if calls_index_path_partial.is_file():
        from chameleon_mcp.safe_open import (
            UnsafeFileError as _UnsafeFileErrorCI,
        )
        from chameleon_mcp.safe_open import (
            safe_read_profile_artifact as _safe_read_profile_artifact_ci,
        )

        try:
            # Same ceiling the calls-index loader accepts, so any artifact it
            # would serve also survives a partial refresh.
            calls_index_text = _safe_read_profile_artifact_ci(
                calls_index_path_partial, max_bytes=16_000_000
            )
        except (OSError, FileNotFoundError, _UnsafeFileErrorCI):
            calls_index_text = None

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
            (txn_dir / "principles.md").write_text(principles_text, encoding="utf-8")
            if renames_text is not None:
                (txn_dir / "renames.json").write_text(renames_text, encoding="utf-8")
            if conventions_text is not None:
                (txn_dir / "conventions.json").write_text(conventions_text, encoding="utf-8")
            if calls_index_text is not None:
                (txn_dir / "calls_index.json").write_text(calls_index_text, encoding="utf-8")
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


def _maybe_fetch_production_ref(repo_root: Path):
    """Refresh-time, default-ON fetch of the locked production branch.

    Returns a ``FetchOutcome`` (or None when no production_ref is locked) so the
    refresh envelope can tell the user whether it derived from the latest tip or
    fell back to the last-fetched ref. Gated so it only ever does ONE bounded
    network fetch and only when it makes sense:
      - a production_ref is locked,
      - the env kill switch CHAMELEON_FETCH_PRODUCTION_REF != "0",
      - the config flag auto_refresh.fetch_production_ref is on,
      - not under CI (a fresh CI clone must not do an unasked network fetch),
      - the branch is actually origin-backed (re-detected, not inferred).
    On success it invalidates the in-process resolve memo so the tip-staleness
    check reads the freshly-fetched SHA. Never raises; never blocks refresh.
    """
    try:
        prod_branch = _persisted_production_ref(repo_root)
        if not prod_branch:
            # No lock yet, but an OLD profile gets migrated to one THIS refresh
            # when detection is clean + origin-backed (see _refresh_repo_locked).
            # Fetch the branch the migration will lock, so the migrating session
            # also derives from the latest tip instead of being one session
            # stale. Honor the explicit "production_ref": null opt-out.
            if _production_ref_explicitly_disabled(repo_root):
                return None
            from chameleon_mcp.production_ref import detect_production_branch

            det = detect_production_branch(repo_root)
            if not (det.branch and not det.conflict and det.from_origin):
                return None
            prod_branch = det.branch
        if os.environ.get("CHAMELEON_FETCH_PRODUCTION_REF") == "0":
            return None
        if os.environ.get("CI"):
            # A CI agent on a fresh clone fires auto-refresh; it must not do an
            # unasked network fetch and won't know the kill switch exists.
            return None
        try:
            from chameleon_mcp.profile.config import load_config

            if not load_config(repo_root / ".chameleon").auto_refresh.fetch_production_ref:
                return None
        except Exception:
            return None  # config unreadable: do not fetch

        from chameleon_mcp.production_ref import (
            branch_is_origin_backed,
            fetch_production_ref,
            invalidate_resolve_memo,
        )

        # The LOCKED branch must itself be origin-backed (origin/<branch>
        # exists), not merely that some branch is: a local-only locked branch
        # must never trigger a doomed network fetch.
        if not branch_is_origin_backed(repo_root, prod_branch):
            return None

        from chameleon_mcp._thresholds import threshold_float
        from chameleon_mcp.profile.trust import repo_data_dir

        data_dir = repo_data_dir(_compute_repo_id(repo_root))
        outcome = fetch_production_ref(
            repo_root,
            prod_branch,
            repo_data_dir=data_dir,
            timeout_seconds=threshold_float("PRODUCTION_REF_FETCH_TIMEOUT_SECONDS"),
            backoff_hours=threshold_float("PRODUCTION_REF_FETCH_BACKOFF_HOURS"),
        )
        if outcome.status == "ok":
            invalidate_resolve_memo(repo_root, prod_branch)
        # Log so the auto-refresh child's redirected stderr records it in
        # auto_refresh.log, and the manual path leaves a server-log trail.
        if outcome.attempted:
            try:
                import sys

                msg = f"chameleon: fetch origin {prod_branch}: {outcome.status}"
                if outcome.reason:
                    msg += f" ({outcome.reason})"
                print(msg, file=sys.stderr)
            except Exception:
                pass
        return outcome
    except Exception:
        return None


def _inject_production_ref_fetch(envelope: dict, outcome) -> None:
    """Fold the fetch outcome into the refresh envelope so /chameleon-refresh can
    render it. Omitted when the fetch was gated off (disabled / not attempted /
    not locked) — the user sees a fetch line only when one actually ran."""
    if outcome is None or not getattr(outcome, "attempted", False):
        return
    if not isinstance(envelope, dict):
        return
    data = envelope.get("data")
    if not isinstance(data, dict):
        return
    data["production_ref_fetch"] = outcome.as_dict()


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

    # refresh resolves its path directly (never through find_repo_root), so it
    # must apply the unsafe-root guard itself — same hole bootstrap_repo had.
    refusal = _unsafe_root_refusal(repo_path)
    if refusal is not None:
        return _envelope({"status": "failed", "error": refusal})

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
            # Default-ON: fetch the locked production tip first (one bounded
            # network call, under the refresh lock) so the tip-staleness check
            # and any re-derive below see the latest production, not the user's
            # last fetch. Fails open; the outcome rides out in the envelope.
            _prod_fetch = _maybe_fetch_production_ref(repo_path.resolve())
            envelope = _refresh_repo_locked(repo_path, force=force)
            _inject_production_ref_fetch(envelope, _prod_fetch)
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

        # Grant under the CURRENT repo_id, not the pre-refresh one. A remote-less
        # repo whose committed profile shipped without config.json resolves by
        # path hash before refresh; refresh persists a repo_uuid into config.json,
        # flipping the id to the uuid form. Granting under the stale pre-refresh
        # id would leave the repo (now uuid-keyed) orphaned and untrusted.
        current_repo_id = _compute_repo_id(repo_path)
        grant_trust(current_repo_id, profile_dir)
        if current_repo_id != pre_state.get("repo_id"):
            # Keep the old-id grant too so a tool still holding the pre-refresh id
            # resolves consistently during the same session.
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
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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
    - the generated indexes must exist and parse as JSON objects:
      ``calls_index.json`` and ``function_catalog.json`` for every supported
      language, plus ``exports_index.json`` / ``reverse_index.json`` when the
      manifest says the profile is TypeScript (a Ruby profile never writes
      the symbol indexes, so their absence must not force a rebuild there);
    - ``profile.summary.md`` must exist;
    - ``principles.md`` must carry the anti-hallucination protocol.

    ``idioms.md`` is user-taught content (preserved across a re-derive), so a
    missing idioms file does NOT force a rebuild.
    """
    import json as _json

    from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

    for name in ("archetypes.json", "canonicals.json", "rules.json", "conventions.json"):
        try:
            obj = _json.loads((profile_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(obj, dict):
            return True
    # The manifest itself (profile.json) must exist, parse, and carry a schema
    # version this engine supports. A corrupt or unsupported-schema (too-new /
    # non-int) manifest is rejected at READ time, but a plain refresh would
    # otherwise noop on unchanged sources and never repair it -- leaving the user
    # with no slash-command recovery. An OLDER supported schema loads fine and is
    # NOT a reason to re-derive; only a missing/corrupt manifest or one whose
    # schema_version is non-int or above the supported max forces the rebuild.
    try:
        manifest = _json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(manifest, dict):
        return True
    schema = manifest.get("schema_version")
    if schema is not None and (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema > MAX_SUPPORTED_SCHEMA_VERSION
    ):
        return True
    # Generated index artifacts: the noop paths preserve the profile dir
    # verbatim, so a deleted or corrupt index would otherwise stay missing
    # forever -- the loaders fail open to "no facts", silently degrading the
    # judge caller facts, the duplication prefilter, and the phantom-symbol /
    # cross-file checks.
    index_names = ["calls_index.json", "function_catalog.json", "symbol_signatures.json"]
    if manifest.get("language") == "typescript":
        index_names += ["exports_index.json", "reverse_index.json"]
    for name in index_names:
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

    cached_files = cached.get("files_indexed") or 0
    last_seen_iso = cached.get("last_seen_at") or ""
    last_seen_epoch = _iso_to_epoch(last_seen_iso)
    idioms_path = profile_dir / "idioms.md"

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

    # Production-pinned refresh: when a production_ref is locked (or an old
    # profile migrates to one here), staleness is the REF TIP, not working-tree
    # mtimes. The checkout is some feature branch whose churn says nothing
    # about the production tree; conversely a moved tip must re-derive even
    # though the checkout is untouched. Unresolvable refs fall through to the
    # working-tree logic below — pinning degrades, it never blocks refresh.
    prod_branch = _persisted_production_ref(repo_root)
    if prod_branch is None and not _production_ref_explicitly_disabled(repo_root):
        from chameleon_mcp.production_ref import (
            detect_production_branch,
            git_toplevel,
            resolve_production_ref,
        )

        # A sidecar profile (bootstrap root below the git toplevel) follows
        # the repo root's decision: its explicit null opt-out blocks the
        # migration, and its configured lock is inherited instead of
        # re-detected.
        toplevel = git_toplevel(repo_root)
        is_subdir = toplevel is not None and toplevel != repo_root.resolve()
        if is_subdir and _production_ref_explicitly_disabled(toplevel):
            pass
        else:
            inherited = _persisted_production_ref(toplevel) if is_subdir else None
            if inherited:
                _persist_production_ref(repo_root, inherited)
                prod_branch = inherited
            else:
                det = detect_production_branch(repo_root)
                if det.branch and not det.conflict and det.from_origin:
                    if resolve_production_ref(repo_root, det.branch) is not None:
                        # Old-profile migration: persist the lock so every later
                        # refresh (and the session-start auto-refresh) is tip-keyed.
                        _persist_production_ref(repo_root, det.branch)
                        prod_branch = det.branch
    elif prod_branch is None and _recorded_derivation_sha(profile_dir) is not None:
        # Explicit opt-out, but the profile still carries production-pinned
        # provenance from before the user disabled the lock. The noop paths
        # preserve artifacts verbatim, so the stale "production-pinned"
        # summary line and derivation_source would otherwise survive every
        # refresh. One full working-tree re-derive re-stamps the profile.
        return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)
    if prod_branch is not None:
        from chameleon_mcp.production_ref import resolve_production_ref

        resolved = resolve_production_ref(repo_root, prod_branch)
        if resolved is not None:
            recorded = _recorded_derivation_sha(profile_dir)
            # idioms.md is user-authored, not derived: a taught idiom since
            # the last derive must not be swallowed by the tip-unchanged noop
            # (the re-derive folds it into summary/principles and re-snapshots
            # the trust mirror).
            idioms_newer = False
            try:
                if idioms_path.is_file() and last_seen_epoch > 0.0:
                    idioms_newer = idioms_path.stat().st_mtime > last_seen_epoch
            except OSError:
                idioms_newer = False
            if recorded == resolved.sha and not missing_artifacts and not idioms_newer:
                index_db.upsert_repo(
                    repo_id,
                    str(repo_root),
                    archetype_count=cached.get("archetype_count"),
                    files_indexed=cached_files,
                    bootstrap_ms=cached.get("bootstrap_ms"),
                    # The migration above may have just rewritten config.json;
                    # mirror the actual on-disk hash, not the cached one.
                    profile_sha256=_hash_profile_or_cached(profile_dir, cached),
                )
                return _envelope(
                    {
                        "status": "noop",
                        "reason": (
                            f"production tip unchanged ({resolved.ref} @ "
                            f"{resolved.sha[:12]}); working-tree changes do not "
                            "affect a production-pinned profile"
                        ),
                        "archetypes_detected": cached.get("archetype_count") or 0,
                        "files_processed": cached_files,
                        "duration_ms": 0,
                        "profile_path": str(profile_dir),
                        "production_ref": {
                            "locked": True,
                            "branch": prod_branch,
                            "ref": resolved.ref,
                            "sha": resolved.sha,
                            "source": "config",
                        },
                    }
                )
            # Tip moved (or pre-feature profile without recorded provenance):
            # full re-derive from the new tip. The bootstrap path re-resolves
            # the lock and materializes the tree itself.
            return bootstrap_repo(str(repo_path), force=True, paths_glob=persisted_pg)

    # Working-tree staleness needs an extractor and a discovery pass; the
    # production-pinned gate above deliberately runs first because it needs
    # neither — a workspace-coordinator root (no root tsconfig/TS deps) has
    # no root-level extractor, and the tip-keyed noop must still engage there.
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

    refresh_inputs = list(candidates) + [idioms_path]
    max_mtime = index_db.max_mtime_over(refresh_inputs)
    cardinality_match = cached_files > 0 and len(candidates) == cached_files
    nothing_newer = last_seen_epoch > 0.0 and max_mtime <= last_seen_epoch

    if cardinality_match and nothing_newer and not missing_artifacts:
        index_db.upsert_repo(
            repo_id,
            str(repo_root),
            archetype_count=cached.get("archetype_count"),
            files_indexed=cached_files,
            bootstrap_ms=cached.get("bootstrap_ms"),
            profile_sha256=cached.get("profile_sha256"),
        )
        noop_data: dict = {
            "status": "noop",
            "reason": "no files changed since last refresh",
            "archetypes_detected": cached.get("archetype_count") or 0,
            "files_processed": cached_files,
            "duration_ms": 0,
            "profile_path": str(profile_dir),
        }
        if prod_branch is not None:
            # Reaching here with a lock means the ref did not resolve and the
            # working-tree logic took over — say so instead of silently
            # dropping the block this path's sibling envelope carries.
            noop_data["production_ref"] = {
                "locked": True,
                "branch": prod_branch,
                "resolvable": False,
                "note": (
                    f"production_ref {prod_branch!r} did not resolve; "
                    "working-tree staleness was used for this refresh"
                ),
            }
        return _envelope(noop_data)

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
            # A partial refresh amends the profile in place, so re-baseline the
            # drift window just like the full re-derive path (which resets
            # inside bootstrap_repo). Partial success reports "partial_refresh",
            # not "success". See reset_drift_baseline for why.
            if partial_envelope.get("data", {}).get("status") in ("success", "partial_refresh"):
                from chameleon_mcp.drift.observations import reset_drift_baseline

                try:
                    reset_drift_baseline(repo_id)
                except Exception:
                    pass
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


# Branch/ref-name shape; ".." is additionally rejected (git refuses it in
# refnames anyway — cheap defense in depth against path-looking input).
_PRODUCTION_REF_ARG_RE = re.compile(r"^(?!.*\.\.)[0-9A-Za-z._/-]{1,200}$")


def bootstrap_repo(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
    production_ref: str | None = None,
) -> dict:
    """First-time analysis, serialized by a per-repo advisory lock.

    Acquires a ``.bootstrap.lock`` in plugin-data so two concurrent inits on the
    same repo don't both run the clusterer and race the COMMITTED check. The
    lock is SEPARATE from ``.refresh.lock``: refresh calls this while holding
    its own lock, so the two distinct locks nest without re-entering the same
    flock (no deadlock). The actual work lives in ``_bootstrap_repo_unlocked``.

    ``production_ref`` is the init skill's confirmed answer to "which branch
    is production?": it pins this derivation to that branch's tree and
    persists the lock in ``.chameleon/config.json``. Omitted, the lock comes
    from the persisted config or (for origin-backed repos) auto-detection.
    """
    from chameleon_mcp.bootstrap.transaction import ProfileCommitError
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    if production_ref is not None:
        if not isinstance(production_ref, str) or not _PRODUCTION_REF_ARG_RE.match(
            production_ref.strip()
        ):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        "production_ref must be a branch or ref name "
                        "([0-9A-Za-z._/-], at most 200 chars)"
                    ),
                }
            )
        production_ref = production_ref.strip()

    resolved_path, _ = _resolve_repo_arg(path)
    if resolved_path is None or not resolved_path.is_dir():
        # Degenerate input (or by-id): let the core emit the precise envelope.
        return _bootstrap_repo_unlocked(path, paths_glob, force, now, production_ref)
    try:
        repo_root = resolved_path.resolve()
    except (OSError, ValueError):
        repo_root = resolved_path
    # Same policy find_repo_root enforces for the hooks: refuse before any
    # lock-dir side effect, so a refused repo leaves zero plugin-data state.
    refusal = _unsafe_root_refusal(repo_root)
    if refusal is not None:
        return _envelope({"status": "failed", "error": refusal})
    from chameleon_mcp.profile.trust import repo_data_dir

    repo_id = _compute_repo_id(repo_root)
    lock_dir = repo_data_dir(repo_id)
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        with acquire_advisory_lock(lock_dir / ".bootstrap.lock"):
            result = _bootstrap_repo_unlocked(path, paths_glob, force, now, production_ref)
        # A successful (re-)derive re-baselines drift: observations were scored
        # against the now-superseded profile, so the drift window resets to
        # empty. Harmless on a first bootstrap (no observations exist yet).
        if result.get("data", {}).get("status") == "success":
            from chameleon_mcp.drift.observations import reset_drift_baseline

            try:
                reset_drift_baseline(repo_id)
            except Exception:
                pass
        return result
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
    except ProfileCommitError as e:
        # Read-only repo root / full disk / revoked permission: fail open with a
        # clean envelope instead of letting the typed commit error escape.
        return _envelope({"status": "failed", "error": f"could not write profile: {e}"})


def _fan_out_block(files_changed: int, lines_changed: int) -> dict:
    """Fan-out recommendation for the pr-review skill. Default-ON over threshold;
    CHAMELEON_REVIEW_FANOUT=0 forces off. Skill partitions the diff itself."""
    import os

    from chameleon_mcp._thresholds import threshold_int

    if os.environ.get("CHAMELEON_REVIEW_FANOUT") == "0":
        return {
            "recommended": False,
            "files_changed": int(files_changed),
            "lines_changed": int(lines_changed),
            "reason": "fan-out disabled (CHAMELEON_REVIEW_FANOUT=0)",
        }
    over = files_changed > threshold_int("REVIEW_FANOUT_FILES") or lines_changed > threshold_int(
        "REVIEW_FANOUT_LINES"
    )
    return {
        "recommended": bool(over),
        "files_changed": int(files_changed),
        "lines_changed": int(lines_changed),
        "reason": ("diff over fan-out threshold" if over else "diff under fan-out threshold"),
    }


# Extensions the contract diff parses. Aligned with the bootstrap extractor glob
# (**/*.{ts,tsx,js,jsx,mjs,cjs} + **/*.rb): .mts/.cts are intentionally absent
# because bootstrap does not scan them, so they are never in the calls index and
# a contract finding for one could never be attributed. (.d.ts ends with .ts and
# is covered, but declaration files carry no positional call sites.)
_CONTRACT_DIFF_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rb")


def get_contract_breaks(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: deterministic caller-contract breaks for a branch diff vs ``base_ref``.

    For each changed TypeScript/Ruby source file, compares its callables'
    POSITIONAL parameter contract at the merge-base of ``base_ref`` and HEAD vs
    HEAD and flags a NARROWING
    (a new required positional arg, or an optional positional flipped required)
    that has committed callers -- the deterministic signal the LLM correctness
    judge derives from the diff, surfaced as a tool result a reviewer can cite.

    Each finding names the callable, its required-arg delta, and the committed
    call sites that may now mis-call it. Tool-time only (git show + AST re-parse);
    no network, no repo-code execution. Default-on; fails open to a no-signal
    result; never blocks. The pr-review skill cites these as FIX findings.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.judge import _git_available, _run_git

    repo_root, _repo_id = _resolve_repo_arg(repo)
    try:
        if repo_root is not None and not repo_root.is_dir():
            repo_root = None
    except (OSError, ValueError):
        repo_root = None
    if repo_root is None:
        return _envelope(
            {
                "status": "failed",
                "error": "repo could not be resolved to a directory; pass an absolute repo path",
                "findings": [],
            }
        )
    if not _git_available(repo_root):
        return _envelope(
            {"status": "degraded", "reason": "not_a_git_worktree", "findings": [], "advisory": True}
        )
    if not isinstance(base_ref, str) or not base_ref.strip():
        return _envelope(
            {"status": "failed", "error": "base_ref must be a non-empty ref name", "findings": []}
        )
    if base_ref == "main":
        locked = _persisted_production_ref(repo_root)
        if locked and locked != "main":
            base_ref = locked

    res = _run_git(["diff", "--numstat", f"{base_ref}...HEAD"], cwd=repo_root)
    if res is None or res.returncode != 0:
        return _envelope(
            {
                "status": "degraded",
                "reason": "git_diff_failed",
                "error": f"git diff against {base_ref!r} failed; the change set is unknown",
                "findings": [],
                "advisory": True,
            }
        )
    _count, details = _compute_contract_breaks(
        repo_root, res.stdout or "", base_ref, threshold_int("AUTOPASS_MAX_FILES")
    )
    return _envelope({"status": "ok", "base_ref": base_ref, "findings": details, "advisory": True})


def _compute_contract_breaks(
    repo_root: Path, numstat_text: str, base_ref: str, max_files: int
) -> tuple[int, list]:
    """Deterministic caller-contract breaks for the auto-pass router, fail-open.

    Compares each changed source file's positional contract at ``base_ref`` vs
    ``HEAD`` and joins narrowings to committed callers from the calls index.
    Returns (count, detail-rows). Off (0, []) when the config flag is false, the
    calls index is absent, the change is over the file cap (it routes to a human
    regardless), or anything raises. git show + AST re-parse, tool-time only.
    """
    try:
        from chameleon_mcp import signature_diff
        from chameleon_mcp.autopass import parse_numstat
        from chameleon_mcp.calls_index import load_calls_index
        from chameleon_mcp.judge import _run_git as _sig_run_git
        from chameleon_mcp.profile.config import load_config

        try:
            enabled = load_config(repo_root / ".chameleon").enforcement.signature_contract_diff
        except Exception:
            enabled = True  # fail-open to on, mirroring judge_crossfile_facts
        if not enabled:
            return 0, []

        rows = parse_numstat(numstat_text)
        if len(rows) > max_files:
            # A change this large already routes to a human on size; the contract
            # diff would not change the verdict and is not worth the re-parse cost.
            return 0, []
        changed_src = [
            r["path"] for r in rows if str(r["path"]).lower().endswith(_CONTRACT_DIFF_EXTS)
        ]
        if not changed_src:
            return 0, []
        index = load_calls_index(repo_root)
        if index is None:
            return 0, []
        # Use the merge-base as the OLD ref so the contract diff matches the
        # `base_ref...HEAD` (three-dot) semantics the rest of the router uses: a
        # divergent base that independently changed a signature must not read as
        # this branch's narrowing. Fall back to base_ref if merge-base can't be
        # resolved (a shallow clone, or HEAD not yet committed).
        mb = _sig_run_git(["merge-base", base_ref, "HEAD"], cwd=repo_root)
        old_ref = (
            mb.stdout.strip()
            if mb is not None and mb.returncode == 0 and (mb.stdout or "").strip()
            else base_ref
        )
        findings = signature_diff.contract_breaks(
            repo_root,
            changed_src,
            old_ref=old_ref,
            new_ref="HEAD",
            callers_fn=index.callers_of,
            run_git=_sig_run_git,
        )
        # Symbol names and caller paths are repo-derived (untrusted under the
        # chameleon threat model) and reach the model through the autopass /
        # contract-breaks tool envelopes, so sanitize every echoed string.
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        def _san_caller(c: dict) -> dict:
            out = dict(c)
            if isinstance(out.get("path"), str):
                out["path"] = sanitize_for_chameleon_context(out["path"])
            return out

        details = [
            {
                "file": sanitize_for_chameleon_context(f.rel),
                "name": sanitize_for_chameleon_context(f.name),
                "old_required_positional": f.old_required_positional,
                "new_required_positional": f.new_required_positional,
                "caller_total": f.caller_total,
                "callers": [_san_caller(c) for c in f.callers if isinstance(c, dict)],
            }
            for f in findings
        ]
        return len(findings), details
    except Exception:
        return 0, []


def get_autopass_verdict(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: is this branch's diff vs ``base_ref`` safe to auto-pass, or does
    it need a human? Never gates -- it informs a review decision. The honest goal
    is not "catch every bug" (no machine does) but to mark the routine slice that
    is safe to skip and route the rest to a human with a reason.

    Fails open toward "needs human": when a signal can't be read, the change is
    treated as the more conservative case rather than waved through. Blast radius
    covers the reverse index's JS/TS extensions: an unreadable fan-out on a
    covered file reads as UNKNOWN and routes to a human, while files outside the
    extension set (Ruby etc.) contribute 0 by design and are gated by the other
    signals until a Ruby cross-file index ships.

    The verdict envelope also carries a ``typecheck`` field (three-state:
    unavailable / clean / errors via the opt-in repo-local tsc runner) and the
    deterministic content/test-integrity facts scanned from the unified diff.
    ``unavailable`` -- including the default opt-in-not-set case -- is a recorded
    fact, never a routing reason; type errors on changed files route needs-human.
    """
    from chameleon_mcp import typecheck
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.autopass import build_autopass_verdict
    from chameleon_mcp.enforcement_calibration import active_block_rules
    from chameleon_mcp.judge import _git_available, _run_git
    from chameleon_mcp.safe_open import safe_read_text

    def _degraded(reason: str, message: str) -> dict:
        d = {
            "status": "degraded",
            "reason": reason,
            "auto_pass_eligible": False,
            "risk": "high",
            # Cannot read the diff -> cannot vouch for it; route as the
            # highest tier so the verdict shape always carries the field and
            # a degraded read never reads as a low-tier auto-pass.
            "complexity_tier": "complex",
            "reasons": [message],
            "fan_out": {
                "recommended": False,
                "files_changed": 0,
                "lines_changed": 0,
                "reason": "autopass degraded; no fan-out sizing",
            },
        }
        return _envelope(d)

    repo_root, repo_id = _resolve_repo_arg(repo)
    if repo_root is None:
        return _degraded("repo_unresolved", "repo could not be resolved")
    # No git work tree means no diff to assess; degrade to "needs human" rather
    # than read git's empty output as "no changes, safe to auto-pass".
    if not _git_available(repo_root):
        return _degraded("not_a_git_worktree", "not a git work tree; cannot assess the change")
    # An empty base_ref would make the range spec "...HEAD", which git accepts
    # with empty output — reading as "no changes" and auto-passing anything.
    if not isinstance(base_ref, str) or not base_ref.strip():
        return _degraded("invalid_base_ref", "base_ref must be a non-empty ref name")
    # A caller leaving the "main" default on a production-pinned repo almost
    # certainly means "the repo's mainline" — which the lock names better. An
    # explicit non-default base_ref is always honored as given.
    if base_ref == "main":
        _locked = _persisted_production_ref(repo_root)
        if _locked and _locked != "main":
            base_ref = _locked
    repo_arg = repo_id or str(repo_root)

    def _git_out(args: list[str]) -> str | None:
        # None means the fetch FAILED (timeout, spawn error, or nonzero exit
        # such as an unresolvable base_ref); "" means git succeeded and the
        # diff is genuinely empty. Collapsing the two would let a bogus
        # base_ref read as "no changes" and auto-pass — the unsafe direction.
        res = _run_git(args, cwd=repo_root)
        if res is None or res.returncode != 0:
            return None
        return res.stdout or ""

    numstat_text = _git_out(["diff", "--numstat", f"{base_ref}...HEAD"])
    name_status_text = _git_out(["diff", "--name-status", f"{base_ref}...HEAD"])
    if numstat_text is None or name_status_text is None:
        return _degraded(
            "git_diff_failed",
            f"git diff against {base_ref!r} failed; the change set is unknown",
        )
    # The plain diff feeds the deterministic content signals (removed guards,
    # in-diff ignore directives, skip markers, assertion delta). It rides the
    # same short git timeout as the other fetches; a diff too big to return in
    # time reads as empty here, and a branch that large already routes to a
    # human on size, so the lost content signal changes nothing. (The numstat
    # and name-status fetches above already proved the ref resolves.)
    diff_text = _git_out(["diff", "--no-ext-diff", "--unified=0", f"{base_ref}...HEAD"]) or ""
    diff_cap = threshold_int("AUTOPASS_MAX_DIFF_BYTES")
    diff_truncated = len(diff_text) > diff_cap

    # Typecheck is three-state: "unavailable" (the default when the opt-in is
    # unset, and the fail-open for any runner error) is a recorded fact that
    # never routes the change; only errors on changed files do.
    typecheck_fact: dict = {
        "status": "unavailable",
        "reason": f"opt-in not set ({typecheck.ALLOW_ENV}=1)",
    }
    type_error_files = None
    if typecheck.is_enabled():
        try:
            typecheck_fact = typecheck.run_tsc(repo_root)
        except Exception as exc:
            # The runner is written never to raise; this keeps an unexpected
            # error from surfacing as a tool crash, mirroring dep_audit.
            typecheck_fact = {
                "status": "unavailable",
                "reason": f"typecheck failed open: {type(exc).__name__}",
            }
        if typecheck_fact.get("status") in ("clean", "errors"):
            type_error_files = set(typecheck_fact.get("files") or ())

    # Test run is three-state like the typecheck: "unavailable" (the default when
    # the opt-in is unset, and the fail-open for any runner error) never routes;
    # a "failures" status routes the change to a human like a type error does.
    from chameleon_mcp import testrun

    tests_fact: dict = {
        "status": "unavailable",
        "reason": f"opt-in not set ({testrun.ALLOW_ENV}=1)",
    }
    tests_failed = False
    if testrun.is_enabled():
        try:
            tests_fact = testrun.run_tests(repo_root)
        except Exception as exc:
            tests_fact = {
                "status": "unavailable",
                "reason": f"test run failed open: {type(exc).__name__}",
            }
        tests_failed = tests_fact.get("status") == "failures"

    try:
        active = active_block_rules(repo_root / ".chameleon")
    except Exception:
        active = set()

    def _archetype_of(rel: str):
        try:
            data = get_archetype(repo_arg, str(repo_root / rel)).get("data") or {}
            return data.get("archetype"), data.get("match_quality")
        except Exception:
            return None, "none"

    def is_unarchetyped(rel: str) -> bool:
        arch, mq = _archetype_of(rel)
        # No archetype, or only a fallback/no match: the engine has no canonical
        # to vouch for the file, so it cannot be auto-passed.
        return not arch or mq in ("none", "fallback")

    _REVERSE_INDEX_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

    def importers_of(rel: str) -> int | None:
        # The reverse index covers the JS/TS module graph only. A file outside
        # those extensions is uncovered by design and contributes 0 (not
        # "unknown"); for a covered file, any unreadable answer -- untrusted
        # profile, missing index, deleted/unreadable module, a raise -- returns
        # None so the router counts it as UNKNOWN fan-out instead of assuming 0,
        # which is the auto-pass direction and the wrong default.
        if not str(rel).lower().endswith(_REVERSE_INDEX_EXTS):
            return 0
        try:
            data = query_symbol_importers(repo_arg, str(repo_root / rel)).get("data") or {}
            if not data.get("found"):
                return None
            return sum(int(i.get("count", 0)) for i in (data.get("importers") or []))
        except Exception:
            return None

    def block_findings_for(rel: str) -> int:
        if not active:
            return 0
        arch, _ = _archetype_of(rel)
        if not arch:
            return 0
        try:
            content = safe_read_text(repo_root, rel)
            data = lint_file(repo_arg, arch, content, str(repo_root / rel)).get("data") or {}
            return sum(1 for v in (data.get("violations") or []) if v.get("rule") in active)
        except Exception:
            return 0

    # Deterministic caller-contract signature diff (default-on; fail-open). The
    # auto-pass router has no per-symbol contract signal otherwise, so a narrowed
    # positional signature in a low-importer file would pass on blast radius
    # alone. Tool-time only (git show + AST re-parse); skipped when the change is
    # already over the file cap (it routes to a human regardless), and silently
    # off when the calls index is absent or the config flag is false.
    max_files_cap = threshold_int("AUTOPASS_MAX_FILES")
    contract_break_count, contract_break_details = _compute_contract_breaks(
        repo_root, numstat_text, base_ref, max_files_cap
    )

    verdict = build_autopass_verdict(
        numstat_text,
        name_status_text,
        is_unarchetyped=is_unarchetyped,
        importers_of=importers_of,
        block_findings_for=block_findings_for,
        type_error_files=type_error_files,
        diff_text=diff_text[:diff_cap],
        diff_truncated=diff_truncated,
        max_files=max_files_cap,
        max_lines=threshold_int("AUTOPASS_MAX_LINES"),
        max_blast_radius=threshold_int("AUTOPASS_MAX_BLAST_RADIUS"),
        test_deletion_net_lines=threshold_int("AUTOPASS_TEST_DELETION_NET_LINES"),
        assertion_delta_floor=threshold_int("AUTOPASS_ASSERTION_DELTA_FLOOR"),
        tests_failed=tests_failed,
        caller_contract_breaks=contract_break_count,
    )
    verdict["advisory"] = True
    verdict["base_ref"] = base_ref
    verdict["contract_breaks"] = contract_break_details
    verdict["typecheck"] = typecheck_fact
    verdict["tests"] = tests_fact
    _facts = verdict.get("facts", {})
    verdict["fan_out"] = _fan_out_block(
        int(_facts.get("files_changed", 0)),
        int(_facts.get("lines_changed", 0)),
    )
    return _envelope(verdict)


def _override_rates_for_demotion(repo_id: str | None, window_days: int | None = None) -> dict:
    """Per-rule override (dismissal) rate, shaped for apply_override_feedback_demotion.

    Reuses the override-audit computation so the rate definition (overrides over
    overrides+would_blocks) stays single-sourced. A rule below the audit's
    min-events floor reports rate None there; it carries no evidence and is
    omitted here so an unseen rule is never demoted. ``distinct_sessions``
    counts the sessions whose inline overrides back the rate; the demotion
    floor reads it so single-session evidence proposes rather than applies.
    """
    from chameleon_mcp.review_ledger import build_override_audit

    out: dict = {}
    audit = build_override_audit(repo_id, window_days)
    for rule, meta in (audit.get("rules") or {}).items():
        rate = meta.get("override_rate")
        if rate is None:
            continue
        events = int(meta.get("overrides", 0)) + int(meta.get("would_blocks", 0))
        out[rule] = {
            "rate": rate,
            "events": events,
            "distinct_sessions": int(meta.get("distinct_sessions", 0) or 0),
        }
    return out


def _calibrate_block_rules_for_repo(repo_root: Path) -> None:
    """Measure block-eligible rules against the repo's own files and persist the
    verdict to ``.chameleon/enforcement.json``.

    Best-effort: a calibration failure must never fail bootstrap/refresh. When
    the artifact is absent or empty no rule is allowed to block (advisory only),
    which is the safe default.
    """
    try:
        from chameleon_mcp._thresholds import threshold_float, threshold_int
        from chameleon_mcp.enforcement_calibration import (
            apply_override_feedback_demotion,
            calibrate_block_rules,
            write_block_rules,
        )
        from chameleon_mcp.profile.loader import load_profile_dir

        profile_dir = repo_root / ".chameleon"
        loaded = load_profile_dir(profile_dir)
        verdicts = calibrate_block_rules(repo_root, loaded)
        # Feed the team's lived override behavior back into the verdict: a rule the
        # team keeps overriding in practice drops to advisory. Isolated so an
        # unreadable override stream leaves the structural calibration untouched.
        try:
            rates = _override_rates_for_demotion(_compute_repo_id(repo_root))
            if rates:
                verdicts = apply_override_feedback_demotion(
                    verdicts,
                    rates,
                    threshold=threshold_float("RULE_FP_DEMOTE_THRESHOLD"),
                    min_events=threshold_int("OVERRIDE_AUDIT_MIN_EVENTS"),
                    min_distinct_sessions=threshold_int("OVERRIDE_DEMOTION_MIN_SESSIONS"),
                )
        except Exception:
            pass
        write_block_rules(profile_dir, verdicts)
    except Exception:
        pass


_NONGIT_PARENT_SCAN_LIMIT = 200


def _nongit_parent_with_git_children(repo_root: Path) -> list[str]:
    """Return immediate child dir names that are their own git repos when
    ``repo_root`` itself is NOT a git working tree.

    Bootstrapping a non-git parent that contains independent git repos plants a
    ``.chameleon/`` at the parent level that then shadows every child repo's
    profile (find_repo_root walks up to the parent). Returns [] when repo_root
    is itself a git repo (the normal case) or has no git children. Best-effort:
    a scan error yields [] so detection never fails bootstrap.
    """
    try:
        if (repo_root / ".git").exists():
            return []
        children: list[str] = []
        for i, child in enumerate(sorted(repo_root.iterdir())):
            if i >= _NONGIT_PARENT_SCAN_LIMIT:
                break
            try:
                if child.is_dir() and (child / ".git").exists():
                    children.append(child.name)
            except OSError:
                continue
        return children
    except OSError:
        return []


def _bootstrap_repo_unlocked(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
    production_ref: str | None = None,
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

    # Defense in depth for every bootstrap path (including the degenerate
    # delegation from the locked wrapper): a profile written under a temp or
    # world-writable root would be unloadable by the hooks, a dead install.
    refusal = _unsafe_root_refusal(repo_root)
    if refusal is not None:
        return _envelope({"status": "failed", "error": refusal})

    git_children = _nongit_parent_with_git_children(repo_root)
    nongit_parent_warning: dict | None = None
    if git_children:
        shown = git_children[:10]
        nongit_parent_warning = {
            "code": "nongit_parent_with_git_children",
            "git_child_dirs": shown,
            "git_child_count": len(git_children),
            "message": (
                f"This directory is not a git repo but contains {len(git_children)} "
                f"git repo(s) ({', '.join(shown)}"
                + ("..." if len(git_children) > len(shown) else "")
                + "). A profile here shadows those child repos: edits inside them "
                "will match this parent profile instead of their own. Bootstrap each "
                "child repo directly instead of this parent."
            ),
        }

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
            already_data = {
                "status": "already_bootstrapped",
                "profile_path": profile_path,
                "message": (
                    "A committed profile already exists at this path. "
                    "Pass force=true to overwrite, or run /chameleon-refresh "
                    "to re-analyze without clearing trust state."
                ),
            }
            if nongit_parent_warning is not None:
                already_data["nongit_parent_warning"] = nongit_parent_warning
            return _envelope(already_data)

    prod_state = _prepare_production_derivation(repo_root, requested_ref=production_ref)
    # Snapshot the envelope block now: release() nulls the tree, and the
    # block's `locked` must reflect what THIS run derived from.
    prod_block = prod_state.envelope_block()
    try:
        report = _bootstrap(
            repo_root,
            paths_glob=paths_glob,
            now=now,
            analysis_root=prod_state.tree,
            derivation_source=(prod_state.derivation_source() if prod_state.tree else None),
        )

        if report.status == "success":
            # Stamp a stable uuid for no-remote repos so the id survives a move,
            # then drop the cache so this repo resolves to the uuid-based id for the
            # index/hash snapshot and every downstream call.
            _persist_repo_uuid_if_no_remote(repo_root)
            # Persist the production lock before the hash snapshot for the same
            # reason as the uuid: config.json is trust-hashed, so the index.db
            # mirror must capture the post-write bytes.
            if prod_state.persist and prod_state.locked and prod_state.branch:
                _persist_production_ref(repo_root, prod_state.branch)
            _clear_repo_id_cache()
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
                file_cluster_rows = _compute_file_cluster_map(
                    prod_state.tree if prod_state.tree is not None else repo_root,
                    paths_glob=paths_glob,
                )
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
            _persist_repo_uuid_if_no_remote(ws_root)
            _clear_repo_id_cache()
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
                # Hash the tree the workspace bootstrap actually analyzed; under
                # a pinned derivation that is the materialized worktree, not the
                # checkout (else every sha_hint describes the wrong bytes).
                ws_scan_root = Path(ws.get("analysis_root") or ws_root_str)
                ws_rows = _compute_file_cluster_map(ws_scan_root, paths_glob=paths_glob)
            except Exception:
                ws_rows = None
            if ws_rows is not None:
                index_db.delete_all_file_clusters(ws_repo_id)
                if ws_rows:
                    index_db.upsert_file_clusters(ws_repo_id, ws_rows)
        # The per-ws analysis_root is internal plumbing for the indexing pass
        # above; a disposable worktree path in the user-visible envelope only
        # confuses. Drop it before serialization.
        for ws in report.workspace_reports or []:
            ws.pop("analysis_root", None)
    finally:
        _release_production_derivation(repo_root, prod_state)

    _notify_daemon_cache_invalidation()
    report_dict = report.to_dict()
    report_dict["production_ref"] = prod_block
    if nongit_parent_warning is not None:
        report_dict["nongit_parent_warning"] = nongit_parent_warning
    return _envelope(report_dict)


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

    from chameleon_mcp.profile.trust import is_material_change

    base = plugin_data_dir()
    profiles = []
    for row in page_rows:
        repo_id = row["repo_id"]
        trust = trust_state_for(repo_id) if (base / repo_id).is_dir() else None
        # Freshness-aware trust state, agreeing with detect_repo: a grant whose
        # profile has materially changed since it was reviewed reads "stale",
        # not "trusted" (this tool used to report the raw record while
        # detect_repo reported staleness, and the two disagreed for the same
        # repo). Falls back to the raw record when the row carries no
        # resolvable root to hash the profile against.
        trust_state = "untrusted"
        if trust is not None:
            trust_state = "trusted"
            row_root = row.get("repo_root")
            if row_root:
                try:
                    profile_dir = Path(row_root) / ".chameleon"
                    if profile_dir.is_dir() and is_material_change(repo_id, profile_dir):
                        trust_state = "stale"
                except OSError:
                    pass
        row_stats = (row.get("archetype_count"), row.get("files_indexed"))
        incomplete = all(v is None for v in row_stats)
        profile_row: dict = {
            "repo_id": repo_id,
            "trust_state": trust_state,
            "trusted_at": trust.granted_at if trust else None,
            "trusted_by": trust.granted_by_user if trust else None,
            "repo_root": row.get("repo_root"),
            "archetype_count": row.get("archetype_count"),
            "files_indexed": row.get("files_indexed"),
            "bootstrap_ms": row.get("bootstrap_ms"),
            "last_seen_at": row.get("last_seen_at"),
            # A row with no stats is an aborted/incomplete bootstrap that
            # persisted; flag it so a reader doesn't mistake it for a healthy
            # profile with null metrics.
            "incomplete": incomplete,
        }
        if incomplete and trust_state == "trusted":
            # Legitimate but easy to misread as contradictory: the user trusted a
            # profile dir whose bootstrap later aborted, so the trust record
            # outlives a usable profile. Spell it out rather than leaving the two
            # fields looking inconsistent.
            profile_row["incomplete_note"] = (
                "trusted record predates an aborted/incomplete bootstrap; "
                "re-run /chameleon-init or /chameleon-refresh to complete it"
            )
        profiles.append(profile_row)

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
    "derivation_source",
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

    The base argument is the merge base: used by the idioms.md union path and
    otherwise only for conflict-detection logging. Canonical-correct three-way
    JSON merging requires re-bootstrap from the merged repo state, which the
    user can trigger with /chameleon-refresh after accepting the merge.
    """

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

    # Undecodable bytes at a profile path (a binary blob routed through the
    # merge driver) fail like the non-JSON case: leave the conflict for manual
    # resolution instead of leaking a traceback to the driver's stderr.
    try:
        ours_text = ours_path.read_text(encoding="utf-8")
        theirs_text = theirs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _envelope(
            {
                "status": "failed",
                "error": f"profile artifact is not readable UTF-8 text: {e}",
                "merged_profile_path": None,
            }
        )

    # idioms.md is markdown, not JSON. Route it to the structural union merge so
    # two branches that each taught idioms keep both sets (git's built-in union
    # driver corrupts the fenced code blocks). Detected by content, so the merge
    # driver needs no extra path argument.
    from chameleon_mcp.idiom_coverage import looks_like_idioms_markdown, merge_idioms_markdown

    if looks_like_idioms_markdown(ours_text) or looks_like_idioms_markdown(theirs_text):
        base_text = ""
        try:
            base_text = Path(base).read_text(encoding="utf-8") if base else ""
        except (OSError, UnicodeDecodeError):
            base_text = ""
        merged_md = merge_idioms_markdown(base_text, ours_text, theirs_text)
        # Atomic write: a SIGKILL mid-write would otherwise leave OURS truncated
        # for git to stage. tmp + replace means the driver result is all-or-nothing.
        _tmp = ours_path.with_name(ours_path.name + ".chameleon-merge.tmp")
        _tmp.write_text(merged_md, encoding="utf-8")
        _tmp.replace(ours_path)
        return _envelope(
            {
                "status": "success",
                "merged_profile_path": str(ours_path),
                "artifact": "idioms.md",
            }
        )

    try:
        ours_data = json.loads(ours_text)
        theirs_data = json.loads(theirs_text)
    except json.JSONDecodeError as e:
        return _envelope(
            {
                "status": "failed",
                "error": f"profile JSON parse error: {e}",
                "merged_profile_path": None,
            }
        )

    # A valid-JSON but non-object document (array / scalar / null) would crash
    # the data_key probe below on .get(); fail open like the parse-error path.
    if not isinstance(ours_data, dict) or not isinstance(theirs_data, dict):
        return _envelope(
            {
                "status": "failed",
                "error": "profile JSON must be a top-level object",
                "merged_profile_path": None,
            }
        )

    # The merge driver runs per-file over profile.json / archetypes.json /
    # rules.json / canonicals.json (each a different shape). Branch on the
    # data key the file actually carries instead of assuming "archetypes" —
    # otherwise a canonicals.json/rules.json conflict gets filtered to
    # _SAFE_TOP_LEVEL_KEYS (which lacks 'canonicals'/'rules'), wiping the
    # real payload, and a profile.json conflict gets its archetype_count
    # zeroed. Generated indexes (exports_index/reverse_index/function_catalog/
    # calls_index) are deliberately NOT routed here: accept either side and
    # /chameleon-refresh regenerates them.
    data_key = None
    for key in ("archetypes", "canonicals", "rules", "conventions"):
        if isinstance(ours_data.get(key), dict) or isinstance(theirs_data.get(key), dict):
            data_key = key
            break

    if data_key == "archetypes":
        ours_archs = ours_data.get("archetypes", {}) or {}
        theirs_archs = theirs_data.get("archetypes", {}) or {}

        # data_key fires when EITHER side is a dict; the merge loop assumes both
        # are. A side carrying a JSON array would crash on `.items()`, so fail
        # open with a clean envelope like the JSON-parse path does.
        if not isinstance(ours_archs, dict) or not isinstance(theirs_archs, dict):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        "archetypes payload is not an object on one side; "
                        "leaving the conflict for manual resolution"
                    ),
                    "merged_profile_path": None,
                }
            )

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

    # Atomic write: a SIGKILL mid-write would otherwise leave OURS truncated
    # for git to stage. tmp + replace means the driver result is all-or-nothing.
    _tmp = ours_path.with_name(ours_path.name + ".chameleon-merge.tmp")
    _tmp.write_text(json.dumps(merged_data, indent=2, sort_keys=True), encoding="utf-8")
    _tmp.replace(ours_path)

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


def _profile_trusted_now(repo_id: str | None, profile_dir: Path) -> bool:
    """True iff the profile is currently trusted for this user: a grant exists,
    covers this root, and the profile is not already a material change (i.e.
    trusted, not stale and not untrusted).

    A user-initiated teach edits a hashed trust artifact. Capturing this BEFORE
    the write lets the caller re-grant afterward so the user's own edit does not
    stale their own trust. Only a genuine pre-existing grant is preserved — an
    untrusted or already-stale profile returns False, so teaching never mints
    trust the user did not hold. Fail-closed: any error reads as not-trusted.
    """
    if not repo_id:
        return False
    try:
        from chameleon_mcp.profile.trust import is_material_change, trust_state_for

        rec = trust_state_for(repo_id)
        return (
            rec is not None
            and rec.grants_root(profile_dir.parent)
            and not is_material_change(repo_id, profile_dir)
        )
    except Exception:  # noqa: BLE001
        return False


def _regrant_trust_if_was_trusted(
    was_trusted: bool, repo_id: str | None, profile_dir: Path
) -> None:
    """Re-stamp the trust grant to the post-write profile hash when the profile
    was trusted before the write.

    Best-effort: a failed re-grant leaves the profile stale (the pre-fix
    behavior), never raises. ``grant_trust`` re-runs its idioms.md/principles.md
    injection scan, so a poisoned teach is refused here and stays stale rather
    than silently re-trusted.
    """
    if not (was_trusted and repo_id):
        return
    try:
        from chameleon_mcp.profile.trust import grant_trust

        grant_trust(repo_id, profile_dir)
    except Exception:  # noqa: BLE001
        pass


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

    # A user-initiated teach edits idioms.md, a hashed trust artifact, which would
    # otherwise stale the trust grant and bounce the user to re-confirm their own
    # change. Capture whether the profile was trusted so we can re-grant after the
    # write (mirrors refresh's trust preservation for the user's own edits).
    _profile_dir = repo_path / ".chameleon"
    _was_trusted = _profile_trusted_now(_repo_id, _profile_dir)

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

    _regrant_trust_if_was_trusted(_was_trusted, _repo_id, _profile_dir)

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
    from chameleon_mcp.profile.trust import ProfileInjectionError, grant_trust

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

    try:
        record = grant_trust(repo_id, profile_dir)
    except ProfileInjectionError as exc:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "profile failed the injection/secret scan and was NOT trusted; "
                    f"review .chameleon/ for poisoned content: {exc}"
                ),
            }
        )
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
        re.compile(
            r"ignore\s+(all\s+)?previous\s+(instructions|directives|rules|guidance|prompts?)",
            re.IGNORECASE,
        ),
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
    - calls_index.json: preserved verbatim (keyed by file paths and callable
      names, never archetype names)

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
            for _section in (
                "imports",
                "naming",
                "inheritance",
                "method_calls",
                "key_exports",
                "class_contract",
            ):
                _sub = _conv_block.get(_section)
                if isinstance(_sub, dict):
                    _conv_block[_section] = {effective.get(k, k): v for k, v in _sub.items()}

    principles_path = profile_dir / "principles.md"
    principles_text = (
        principles_path.read_text(encoding="utf-8") if principles_path.is_file() else None
    )

    # calls_index.json is a protocol file too, so the swap deletes whatever
    # the txn does not re-emit. A rename never invalidates caller facts (the
    # index is keyed by file paths and callable names, not archetype names),
    # so carry the artifact forward verbatim — same posture and 16MB ceiling
    # as the partial-refresh path.
    calls_index_text: str | None = None
    calls_index_path = profile_dir / "calls_index.json"
    if calls_index_path.is_file():
        from chameleon_mcp.safe_open import (
            UnsafeFileError as _UnsafeFileErrorRn,
        )
        from chameleon_mcp.safe_open import (
            safe_read_profile_artifact as _safe_read_profile_artifact_rn,
        )

        try:
            calls_index_text = _safe_read_profile_artifact_rn(
                calls_index_path, max_bytes=16_000_000
            )
        except (OSError, FileNotFoundError, _UnsafeFileErrorRn):
            calls_index_text = None

    idioms_path = profile_dir / "idioms.md"
    idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

    # Rewrite taught-idiom archetype references so a rename does not leave a
    # dangling "Archetype: <old>" pointing at an archetype that no longer exists.
    for _old, _new in effective.items():
        idioms_text = re.sub(
            rf"(?m)^Archetype: {re.escape(_old)}$",
            f"Archetype: {_new}",
            idioms_text,
        )

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
            if calls_index_text is not None:
                (txn_dir / "calls_index.json").write_text(calls_index_text, encoding="utf-8")
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

    # The rename rewrites canonicals.json (the witness set), so the block-rule
    # verdict in enforcement.json must be re-measured against the renamed profile;
    # otherwise it stays pinned to the pre-rename witnesses. Calibrate before the
    # hash snapshot so enforcement.json (part of the trust-hashed surface) is
    # reflected in new_profile_sha256 and the index.db mirror.
    _calibrate_block_rules_for_repo(repo_root)

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

    # conventions.json is a hashed trust artifact; capture trust before the write
    # so the user's own teach does not stale their own trust.
    _was_trusted = _profile_trusted_now(_repo_id, profile_dir)

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

    # A real write staled the user's own trust; re-grant so they aren't bounced
    # to re-confirm their own change. No-op when the pair was already present.
    if not already:
        _regrant_trust_if_was_trusted(_was_trusted, _repo_id, profile_dir)

    # The archetype is regex-validated but not required to exist (a teammate may
    # be capturing a rule for a renamed/refreshed archetype the profile does not
    # yet reflect). But unlike a teach_profile idiom, this rule DRIVES a lint:
    # an archetype no committed file matches makes the rule silently dead. Surface
    # a non-fatal warning when the archetype is absent from the current profile so
    # a typo is visible; the write still succeeds. Best-effort: skip the warning if
    # the catalog is unreadable.
    warning = None
    try:
        _arch = json.loads(safe_read_profile_artifact(profile_dir / "archetypes.json"))
        _known = _arch.get("archetypes") if isinstance(_arch, dict) else None
        if isinstance(_known, dict) and archetype not in _known:
            warning = (
                f"archetype {archetype!r} is not in the current profile; the rule was "
                "recorded but will not match any file until an archetype by that name "
                "exists. Check for a typo, or /chameleon-refresh if it was renamed."
            )
    except Exception:
        warning = None

    result = {
        "status": "success",
        "archetype": archetype,
        "competing": {"preferred": preferred, "over": over},
        "already_present": already,
        "note": (
            "this wrapper-preference pair was already present; nothing changed"
            if already
            else (
                "wrapper-preference recorded in conventions.json; your trust was preserved."
                if _was_trusted
                else "wrapper-preference recorded in conventions.json."
            )
        ),
    }
    if warning:
        result["warning"] = warning
    return _envelope(result)


def unteach_competing_import(
    repo: str,
    *,
    archetype: str,
    preferred: str,
    over: str,
) -> dict:
    """Remove a taught wrapper-preference pair from an archetype.

    The inverse of :func:`teach_competing_import`: deletes the matching
    ``{preferred, over}`` entry from ``conventions.imports.<archetype>.competing``
    so a pair taught in error stops driving the banned-import lint, without
    hand-editing ``conventions.json``. Same in-place, flock-serialized, atomic
    single-file write; touches no other artifact. A no-op (``removed: False``)
    when the pair, archetype, or conventions file is absent.
    """
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
    if not conv_path.is_file():
        return _envelope(
            {
                "status": "success",
                "archetype": archetype,
                "removed": False,
                "note": "no conventions.json; nothing to remove",
            }
        )

    # conventions.json is a hashed trust artifact; capture trust before the write
    # so removing the user's own taught rule does not stale their own trust.
    _was_trusted = _profile_trusted_now(_repo_id, profile_dir)

    removed = False
    lock_path = _rdd(_compute_repo_id(repo_path)) / ".conventions.lock"
    try:
        with acquire_advisory_lock(lock_path):
            try:
                conv = json.loads(safe_read_profile_artifact(conv_path))
            except Exception:
                return _envelope(
                    {"status": "failed", "error": "conventions.json unreadable or invalid"}
                )
            if not isinstance(conv, dict):
                return _envelope(
                    {
                        "status": "success",
                        "archetype": archetype,
                        "removed": False,
                        "note": "conventions.json is not an object; nothing to remove",
                    }
                )
            block = conv.get("conventions")
            imports = block.get("imports") if isinstance(block, dict) else None
            entry = imports.get(archetype) if isinstance(imports, dict) else None
            competing = entry.get("competing") if isinstance(entry, dict) else None
            if isinstance(competing, list):
                kept = [
                    c
                    for c in competing
                    if not (
                        isinstance(c, dict)
                        and c.get("preferred") == preferred
                        and c.get("over") == over
                    )
                ]
                if len(kept) != len(competing):
                    entry["competing"] = kept
                    removed = True
            if removed:
                # Atomic write, matching teach_competing_import: a truncated
                # conventions.json bricks the whole profile on load.
                _tmp = conv_path.with_suffix(".json.tmp")
                _tmp.write_text(json.dumps(conv, indent=2, sort_keys=True), encoding="utf-8")
                _tmp.replace(conv_path)
    except LockHeldError as e:
        return _envelope(
            {"status": "failed", "error": f"another conventions write is in progress: {e}"}
        )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"conventions write failed: {e}"})

    # A real removal staled the user's own trust; re-grant. No-op when nothing matched.
    if removed:
        _regrant_trust_if_was_trusted(_was_trusted, _repo_id, profile_dir)

    return _envelope(
        {
            "status": "success",
            "archetype": archetype,
            "competing": {"preferred": preferred, "over": over},
            "removed": removed,
            "note": (
                (
                    "wrapper-preference removed from conventions.json; your trust was preserved."
                    if _was_trusted
                    else "wrapper-preference removed from conventions.json."
                )
                if removed
                else "no matching wrapper-preference pair found; nothing changed"
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
    source: str | None = None,
) -> dict:
    """Structured-form idiom capture.

    Renders to .chameleon/idioms.md as a fully-formed idiom entry that
    matches the format the chameleon-teach skill emits in free-form mode.

    Validation:
    - slug matches ``^[a-z][a-z0-9-]{2,63}$``
    - rationale must be non-empty after strip
    - len(rationale) + len(example or '') + len(counterexample or '') + len(source or '') ≤ 50KB
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
    # Fail soft on a non-string example/counterexample (the signature declares
    # str | None). Without this, len(example or "") raises TypeError on a
    # bool/int instead of returning the documented failed envelope.
    if example is not None and not isinstance(example, str):
        return _envelope({"status": "failed", "error": "example must be a string or null"})
    if counterexample is not None and not isinstance(counterexample, str):
        return _envelope({"status": "failed", "error": "counterexample must be a string or null"})
    if source is not None and not isinstance(source, str):
        return _envelope({"status": "failed", "error": "source must be a string or null"})

    total = len(rationale) + len(example or "") + len(counterexample or "") + len(source or "")
    if total > _STRUCTURED_TOTAL_CAP:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"rationale + example + counterexample + source size {total} exceeds "
                    f"50KB cap ({_STRUCTURED_TOTAL_CAP})"
                ),
            }
        )

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())
    # Source is single-line provenance metadata: collapse all whitespace (incl.
    # newlines) so a multi-line value can never inject a `### slug` / `## section`
    # heading into idioms.md.
    clean_source = " ".join(source.split()) if source else ""
    lines: list[str] = [f"### {slug}"]
    if status == "active":
        lines.append(f"Status: active (added {timestamp})")
    else:
        lines.append(f"Status: deprecated {timestamp}")
    if archetype:
        lines.append(f"Archetype: {archetype}")
    if clean_source:
        # Provenance: where this idiom was derived from (evidence file(s) + ref),
        # so a poisoned idiom is traceable and the trust gate can show its origin.
        lines.append(f"Source: {clean_source}")
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
    # The deprecated-idiom paths write idioms.md directly (the active path
    # delegates to teach_profile, which preserves trust on its own). Capture
    # trust before the write and re-grant on success so deprecating an idiom does
    # not stale the user's own trust.
    profile_dir = repo_path / ".chameleon"
    if in_active and status == "deprecated":
        was_trusted = _profile_trusted_now(_repo_id, profile_dir)
        result = _transition_slug_to_deprecated(
            idioms_path,
            slug,
            archetype=archetype,
            rationale=rationale.strip(),
            timestamp=timestamp,
            example=example,
            counterexample=counterexample,
            source=clean_source or None,
        )
        if result.get("data", {}).get("status") == "success":
            _regrant_trust_if_was_trusted(was_trusted, _repo_id, profile_dir)
        return result

    if status == "active":
        return teach_profile(repo, rendered)
    was_trusted = _profile_trusted_now(_repo_id, profile_dir)
    result = _write_new_deprecated_idiom(
        idioms_path,
        slug,
        archetype=archetype,
        rationale=rationale.strip(),
        timestamp=timestamp,
        example=example,
        counterexample=counterexample,
        source=clean_source or None,
    )
    if result.get("data", {}).get("status") == "success":
        _regrant_trust_if_was_trusted(was_trusted, _repo_id, profile_dir)
    return result


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
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        # Fence-aware, matching idiom_coverage.parse_idiom_blocks: a `### slug`
        # line inside a fenced example is payload, not a real heading. Without
        # this, the gate (fence-aware) and this duplicate-checker disagree and
        # teach falsely refuses a slug that only appears as example code.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
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
    source: str | None = None,
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
    if source:
        lines.append(f"Source: {source}")
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
    source: str | None = None,
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
        source=source,
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
    source: str | None = None,
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
        source=source,
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


def _resolve_profile_dir_for_idiom_tools(repo: str) -> tuple[Path | None, dict | None]:
    """Shared repo/profile resolution + trust gate for the idiom tools.

    Returns (profile_dir, error_data); exactly one side is non-None. The
    error carries ``status: "untrusted"`` when the profile exists but is not
    trusted, so the caller can withhold content (these tools are
    model-callable and idioms.md / principles.md / conventions.json are
    committed, attacker-controllable prose). Stale still flows (trusted once),
    mirroring get_rules / get_pattern_context.

    Resolves to the WORKING-TREE ``.chameleon`` (not the canonical-ref pin):
    the novelty gate must read the same idioms.md that
    teach_profile_structured writes, or a near-duplicate of a just-taught
    working-tree idiom would sail through on a pinned repo.
    """
    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None or not repo_path.is_dir():
        return None, {
            "status": "failed",
            "error": "expected repo path or repo_id hex digest",
        }
    cham = repo_path / ".chameleon"
    if not cham.is_dir():
        return None, {
            "status": "failed",
            "error": "no profile in this repo (run /chameleon-init)",
        }
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    _gate = _trust_state_for(_compute_repo_id(repo_path))
    if _gate is None or not _gate.grants_root(repo_path):
        return None, {"status": "untrusted"}
    return cham, None


def get_idiom_coverage(repo: str) -> dict:
    """Map of guidance chameleon ALREADY captures for a repo, for /chameleon-auto-idiom.

    Read-only. Returns existing idioms (active + deprecated, with per-idiom
    summaries), auto-derived principle lines, structured conventions
    (preferred/competing imports, file-naming casing, inheritance bases,
    error-handling shape, non-empty convention kinds), lint sources, and
    archetype names. The auto-idiom skill reads this BEFORE drafting
    candidates so it never proposes something chameleon already derives.

    Withholds all profile-derived content (returns ``status: "untrusted"``)
    for an untrusted profile, mirroring the rest of the model-callable read
    surface. Fail-open: each missing/corrupt artifact skips its own section
    and is listed in ``checks_skipped``; only a missing profile fails the call.
    """
    from chameleon_mcp.idiom_coverage import build_coverage

    profile_dir, error = _resolve_profile_dir_for_idiom_tools(repo)
    if error is not None:
        if error.get("status") == "untrusted":
            return _envelope(
                {
                    "status": "untrusted",
                    "existing_idioms": {"active": [], "active_count": 0, "deprecated": []},
                    "covered": {},
                    "checks_skipped": [],
                }
            )
        return _envelope(error)
    try:
        data, skipped = build_coverage(profile_dir)
    except Exception as exc:
        return _envelope({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    data["status"] = "ok"
    data["checks_skipped"] = skipped
    return _envelope(data)


def check_idiom_candidates(repo: str, candidates: list) -> dict:
    """Deterministic novelty gate for /chameleon-auto-idiom candidates.

    Each candidate is {slug, rationale, example?, counterexample?, archetype?}.
    Verdicts: ``novel`` (safe to teach), ``duplicate`` (slug exists, text is
    near-identical to an existing idiom, or repeats an earlier candidate in
    the same batch), ``covered`` (restates an auto-derived principle,
    competing-import pair, naming/inheritance convention, or lint/format
    rule), ``invalid`` (bad slug / missing rationale / over the 50KB cap).
    ``quality_warnings`` flags missing example/counterexample and thin
    rationales so the skill can raise candidate quality before writing.

    Read-only: this judges candidates; the write still goes through
    teach_profile_structured (append-only — existing idioms are never
    modified or removed). Withholds judging (returns ``status: "untrusted"``)
    for an untrusted profile. Fail-open: damaged artifacts skip their own
    check (reported in ``checks_skipped``) and never crash the call.
    """
    from chameleon_mcp.idiom_coverage import check_candidates

    profile_dir, error = _resolve_profile_dir_for_idiom_tools(repo)
    if error is not None:
        if error.get("status") == "untrusted":
            return _envelope(
                {"status": "untrusted", "results": [], "novel_count": 0, "checks_skipped": []}
            )
        return _envelope(error)
    try:
        return _envelope(check_candidates(profile_dir, candidates))
    except Exception as exc:
        return _envelope({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})


def dep_audit(repo: str) -> dict:
    """Opt-in dependency / supply-chain audit of a repo's manifests. Advisory only.

    Runs ``npm audit --json`` and/or ``bundler-audit check`` (whichever manifests
    exist) in the repo root and returns a structured advisory summary. Gated behind
    ``CHAMELEON_ALLOW_DEP_AUDIT=1`` because it hits the network; without the flag it
    refuses with a clear message rather than spawning a network process unasked.

    Fails open: a missing binary, an offline registry, or a timeout yields an
    ``unavailable`` no-signal result per ecosystem, never an error. Nothing here
    blocks an edit or a turn. The no-network manifest/lockfile diff checks live in
    the pr-review skill and run regardless of this tool.
    """
    from chameleon_mcp import dep_audit as _dep_audit

    # Validate the repo argument BEFORE the env gate: a bad path must surface
    # as a bad path, not as an opt-in refusal that masks the real problem (and
    # path validation is read-only, so there is nothing the gate protects).
    resolved_path, _ = _resolve_repo_arg(repo)
    repo_root = resolved_path
    try:
        if repo_root is not None and not repo_root.is_dir():
            repo_root = None
    except (OSError, ValueError):
        repo_root = None
    if repo_root is None:
        return _envelope(
            {
                "status": "failed",
                "error": ("repo could not be resolved to a directory; pass an absolute repo path"),
            }
        )

    if not _dep_audit.is_enabled():
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "dependency audit is opt-in and hits the network; set "
                    f"{_dep_audit.ALLOW_ENV}=1 to enable it for this invocation. The "
                    "no-network manifest/lockfile checks run in /chameleon-pr-review "
                    "regardless."
                ),
            }
        )

    try:
        result = _dep_audit.run_dep_audit(repo_root)
    except Exception as exc:  # noqa: BLE001
        # The helper is written never to raise, but a defensive fail-open here
        # keeps an unexpected error from surfacing as a tool crash.
        return _envelope(
            {
                "audits": [],
                "ran": [],
                "skipped": [],
                "note": f"audit failed open: {type(exc).__name__}",
            }
        )
    result["advisory"] = True
    return _envelope(result)


def scan_dependency_changes(repo: str, base_ref: str = "main") -> dict:
    """No-network supply-chain review of a branch's manifest/lockfile changes.

    Parses the ``base_ref...HEAD`` git diff of changed package manifests and
    lockfiles (``package.json``, the npm/yarn/pnpm lockfiles, ``Gemfile``,
    ``Gemfile.lock``) for the four deterministic pr-review Step 2.5 signals:
    a new install-lifecycle script (FIX), a lockfile entry resolving from a
    non-registry host (FIX), a non-registry dependency source (FIX), and a new
    direct dependency (NIT listing). Each finding cites the exact added line, so
    the pr-review refuter can ground it against this tool result rather than prose.

    PURE PARSE: no network, no install -- that is :func:`dep_audit`'s opt-in,
    network-gated job. Default-on (qualifies under the default-on principle: it
    only reads local git diff text). Fails open: a non-git tree or a failed diff
    degrades to a no-signal result, never a crash and never a fabricated finding.
    Advisory only; nothing here blocks.
    """
    from chameleon_mcp import dep_diff
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.judge import _git_available, _run_git
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    repo_root, _repo_id = _resolve_repo_arg(repo)
    try:
        if repo_root is not None and not repo_root.is_dir():
            repo_root = None
    except (OSError, ValueError):
        repo_root = None
    if repo_root is None:
        return _envelope(
            {
                "status": "failed",
                "error": "repo could not be resolved to a directory; pass an absolute repo path",
                "findings": [],
            }
        )
    if not _git_available(repo_root):
        return _envelope(
            {
                "status": "degraded",
                "reason": "not_a_git_worktree",
                "findings": [],
                "advisory": True,
            }
        )
    if not isinstance(base_ref, str) or not base_ref.strip():
        return _envelope(
            {
                "status": "failed",
                "error": "base_ref must be a non-empty ref name",
                "findings": [],
            }
        )
    # A "main" default on a production-pinned repo means the repo's mainline,
    # which the lock names better; an explicit non-default base_ref is honored.
    if base_ref == "main":
        locked = _persisted_production_ref(repo_root)
        if locked and locked != "main":
            base_ref = locked

    def _git_out(args: list[str]) -> str | None:
        res = _run_git(args, cwd=repo_root)
        if res is None or res.returncode != 0:
            return None
        return res.stdout or ""

    names = _git_out(["diff", "--name-only", f"{base_ref}...HEAD"])
    if names is None:
        return _envelope(
            {
                "status": "degraded",
                "reason": "git_diff_failed",
                "error": f"git diff against {base_ref!r} failed; the change set is unknown",
                "findings": [],
                "advisory": True,
            }
        )
    changed = [ln.strip() for ln in names.splitlines() if ln.strip()]
    cap = threshold_int("DEP_DIFF_MAX_BYTES")
    # "no silent caps": record which manifest diffs were truncated at the cap so a
    # large lockfile change cannot read as fully reviewed when its tail was dropped.
    truncated_files: list[str] = []

    def _fetch(rel_path: str) -> str:
        out = _git_out(["diff", "--no-ext-diff", f"{base_ref}...HEAD", "--", rel_path]) or ""
        if len(out) > cap:
            truncated_files.append(rel_path)
            return out[:cap]
        return out

    try:
        findings = dep_diff.collect_dependency_findings(changed, _fetch)
    except Exception:
        # collect_dependency_findings is written never to raise; defensive fail-open.
        findings = []

    # Every field echoes untrusted manifest/lockfile text to the model, so each
    # string is run through the chameleon-context sanitizer before serialization.
    def _san(value):
        if isinstance(value, str):
            return sanitize_for_chameleon_context(value)
        if isinstance(value, dict):
            return {k: _san(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_san(v) for v in value]
        return value

    serialized = [
        {
            "check": f.check,
            "severity": f.severity,
            "path": _san(f.path),
            "evidence": _san(f.evidence),
            "message": _san(f.message),
            "detail": _san(f.detail),
        }
        for f in findings
    ]
    summary = {
        "fix": sum(1 for f in findings if f.severity == "FIX"),
        "nit": sum(1 for f in findings if f.severity == "NIT"),
    }
    data = {
        "status": "ok",
        "base_ref": base_ref,
        "manifests_changed": [
            p for p in changed if p.rsplit("/", 1)[-1] in dep_diff.MANIFEST_LOCKFILE_BASENAMES
        ],
        "findings": serialized,
        "summary": summary,
        "advisory": True,
    }
    if truncated_files:
        # Surfaced inside `data` (not just the envelope flag) so the consuming
        # skill sees the coverage gap and can say so rather than claim clean.
        data["truncated"] = True
        data["truncated_files"] = truncated_files
    return _envelope(data, truncated=bool(truncated_files))


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


def _conflict_marked_artifacts(profile_dir: Path) -> list[str]:
    """Names of profile artifacts carrying unresolved git conflict markers.

    Only the markdown artifacts and the sentinel need the scan: a marker-laden
    JSON artifact already fails its parse and reads as corrupt everywhere.
    """
    found: list[str] = []
    for name in ("COMMITTED", "idioms.md", "principles.md", "profile.summary.md"):
        p = profile_dir / name
        try:
            if not p.is_file():
                continue
            head = p.read_bytes()[:262_144]
        except OSError:
            continue
        anchored = b"\n" + head
        if b"\n<<<<<<< " in anchored and b"\n>>>>>>> " in anchored:
            found.append(name)
    return found


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

    # Hook interpreter probe. Every hook resolves its Python through one shared
    # ladder (hooks/_resolve-python.sh): the bundled venv, then version-named
    # python3.x, then `uv run` (the same dep-complete resolver the MCP server
    # uses via uvx), and only a version-validated bare python3 — never a blind
    # one, since macOS's /usr/bin/python3 is 3.9.x, below the >=3.11 floor and
    # without chameleon's deps. Run that exact resolver here so doctor reports
    # the command the hooks actually pick, then confirm the winner is >=3.11 and
    # imports the deps. The hot-path hooks are stdlib-only, but the hook-spawned
    # refresh imports the extractors and falls back to `uv run` when the winner
    # lacks deps, so a >=3.11-but-depless winner with uv present is a warn,
    # without uv an error.
    if plugin_root_env:
        try:
            import subprocess as _subp

            mcp_dir = Path(plugin_root_env) / "mcp"
            resolver = Path(plugin_root_env) / "hooks" / "_resolve-python.sh"
            bash = shutil.which("bash")
            hook_cmd: list[str] | None = None
            if resolver.is_file() and bash:
                res = _subp.run(
                    [bash, str(resolver), str(mcp_dir)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res.returncode == 0:
                    hook_cmd = [ln for ln in res.stdout.splitlines() if ln.strip()]
            probe_env = {**os.environ, "PYTHONPATH": str(mcp_dir)}
            if not bash or not resolver.is_file():
                checks.append(
                    {
                        "name": "hook_interpreter_deps",
                        "status": "warn",
                        "detail": (
                            "cannot probe the hook interpreter: "
                            f"{'bash not on PATH' if not bash else 'resolver script missing'}"
                        ),
                    }
                )
            elif not hook_cmd:
                checks.append(
                    {
                        "name": "hook_interpreter_deps",
                        "status": "error",
                        "detail": (
                            "no Python >=3.11 resolves for the hooks and `uv` is not on PATH; "
                            "enforcement and guidance are OFF. Install uv or a Python >=3.11."
                        ),
                    }
                )
            else:
                cmd_str = " ".join(hook_cmd)
                ver = _subp.run(
                    [*hook_cmd, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=probe_env,
                )
                ver_str = ver.stdout.strip() if ver.returncode == 0 else "?"
                probe = _subp.run(
                    [*hook_cmd, "-c", "import xxhash, yaml, detect_secrets, chameleon_mcp"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=probe_env,
                )
                if probe.returncode == 0:
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "ok",
                            "detail": f"hooks resolve `{cmd_str}` (Python {ver_str}); imports deps",
                        }
                    )
                elif shutil.which("uv"):
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "warn",
                            "detail": (
                                f"hooks resolve `{cmd_str}` (Python {ver_str}); missing deps "
                                "(e.g. xxhash) — the hook-spawned refresh falls back to `uv run`. "
                                "Create mcp/.venv with the deps to remove the fallback."
                            ),
                        }
                    )
                else:
                    checks.append(
                        {
                            "name": "hook_interpreter_deps",
                            "status": "error",
                            "detail": (
                                f"hooks resolve `{cmd_str}` (Python {ver_str}); cannot import "
                                "chameleon deps and `uv` is not on PATH; hook-spawned "
                                "refresh/bootstrap will fail. Install uv or create mcp/.venv."
                            ),
                        }
                    )
        except Exception as exc:
            checks.append(
                {
                    "name": "hook_interpreter_deps",
                    "status": "warn",
                    "detail": f"could not probe hook interpreter: {type(exc).__name__}: {exc}",
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
        # Same precedence as the hook wrappers' LOG_DIR fallback: a
        # CHAMELEON_PLUGIN_DATA override (tests, isolated sessions) keeps its
        # hook errors out of the real data dir, and doctor reads where the
        # wrappers wrote.
        data_env = os.environ.get("CHAMELEON_PLUGIN_DATA")
        base = Path(data_env) if data_env else Path.home() / ".local" / "share" / "chameleon"
        log = base / ".hook_errors.log"
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
                "production_ref": cfg.production_ref,
                "auto_refresh.enabled": cfg.auto_refresh.enabled,
                "auto_refresh.drift_threshold": cfg.auto_refresh.drift_threshold,
                "auto_refresh.max_age_hours": cfg.auto_refresh.max_age_hours,
                "trust.auto_preserve_when": cfg.trust.auto_preserve_when,
                "auto_rename": cfg.auto_rename,
            }
            checks.append({"name": "config_json", "status": "ok", "detail": detail})
            # A locked production_ref that no longer resolves means every
            # bootstrap/refresh silently degrades to working-tree derivation
            # — worth a loud check of its own (deleted branch, renamed
            # remote, shallow clone without the ref).
            if cfg.production_ref:
                try:
                    from chameleon_mcp.production_ref import resolve_production_ref

                    _doctor_resolved = resolve_production_ref(Path.cwd(), cfg.production_ref)
                    if _doctor_resolved is None:
                        checks.append(
                            {
                                "name": "production_ref",
                                "status": "warn",
                                "detail": (
                                    f"production_ref {cfg.production_ref!r} does not "
                                    "resolve to a commit in this repo; derivation "
                                    "falls back to the working tree. Fetch the "
                                    "branch, fix the name in .chameleon/config.json, "
                                    "or remove the key."
                                ),
                            }
                        )
                    else:
                        checks.append(
                            {
                                "name": "production_ref",
                                "status": "ok",
                                "detail": (
                                    f"locked to {_doctor_resolved.ref} @ "
                                    f"{_doctor_resolved.sha[:12]}"
                                ),
                            }
                        )
                except Exception as _prod_exc:  # noqa: BLE001
                    checks.append(
                        {
                            "name": "production_ref",
                            "status": "warn",
                            "detail": f"{type(_prod_exc).__name__}: {_prod_exc}",
                        }
                    )
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
                    "canonical_ref (branch pinning) and production_ref (ref-pinned "
                    "derivation; auto-locked at init/refresh for origin-backed "
                    "repos). Add a config.json to change these, e.g. "
                    '{"trust": {"auto_preserve_when": null}} to be '
                    "re-prompted for trust on each material refresh."
                ),
            }
        )

    # Dead-install detectors: an install can pass every plumbing check above
    # while chameleon does nothing — generated artifacts missing so resolution
    # is degenerate, every turn-end reviewer spawn failing, or a trusted repo
    # whose edits never resolve an archetype. Each detector fails open: absence
    # of a profile / attestations / metrics is healthy-unknown, never a warn.
    cwd_root = Path.cwd()
    cwd_profile_dir = cwd_root / ".chameleon"
    try:
        cwd_repo_id: str | None = _compute_repo_id(cwd_root)
    except Exception:
        cwd_repo_id = None

    try:
        if (cwd_profile_dir / "profile.json").is_file():
            import json as _json

            try:
                _lang = _json.loads(
                    (cwd_profile_dir / "profile.json").read_text(encoding="utf-8")
                ).get("language")
            except Exception:
                _lang = None
            expected_artifacts = ["calls_index.json", "function_catalog.json"]
            if _lang == "typescript":
                expected_artifacts += ["exports_index.json", "reverse_index.json"]
            artifact_problems: list[str] = []
            for art in expected_artifacts:
                apath = cwd_profile_dir / art
                if not apath.is_file():
                    artifact_problems.append(f"{art} missing")
                    continue
                try:
                    _json.loads(apath.read_text(encoding="utf-8"))
                except Exception:
                    artifact_problems.append(f"{art} corrupt")
            if artifact_problems:
                checks.append(
                    {
                        "name": "profile_artifacts",
                        "status": "warn",
                        "detail": (
                            "generated profile artifacts unhealthy: "
                            + "; ".join(artifact_problems)
                            + ". Cross-file facts and duplication checks degrade "
                            "without them; run /chameleon-refresh to regenerate."
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "name": "profile_artifacts",
                        "status": "ok",
                        "detail": (
                            f"{len(expected_artifacts)} generated artifacts present and parseable"
                        ),
                    }
                )
        else:
            checks.append(
                {
                    "name": "profile_artifacts",
                    "status": "ok",
                    "detail": "no profile in the current directory",
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "profile_artifacts",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        from chameleon_mcp.profile.trust import plugin_data_dir as _pdd

        judge_detail = "no attested sessions for this repo"
        judge_warn = False
        # The .is_dir() guard keeps doctor read-only here: the attestation
        # reader's path resolution would otherwise CREATE the repo data dir.
        if cwd_repo_id and (_pdd() / cwd_repo_id).is_dir():
            from chameleon_mcp.review_ledger import read_session_attestations

            _hist = read_session_attestations(cwd_repo_id, limit=5)
            _records = _hist.get("records") or []
            judge_events = [
                e
                for rec in _records
                for e in (rec.get("checks") or [])
                if isinstance(e, dict) and e.get("check") == "correctness_judge"
            ]
            degraded = [e for e in judge_events if e.get("status") == "degraded_spawn"]
            # Every spawn attempt logs "spawned/started"; only "spawned/completed"
            # marks a reviewer that actually ran. Degradations with no completion
            # anywhere in the window mean the turn-end review layer is dead.
            completed = [
                e
                for e in judge_events
                if e.get("status") == "spawned" and e.get("reason") == "completed"
            ]
            if degraded and not completed:
                judge_warn = True
                _reasons = sorted({str(e.get("reason") or "unknown") for e in degraded})
                judge_detail = (
                    f"turn-end reviewer failing to spawn ({', '.join(_reasons)}) across the "
                    f"last {len(_records)} attested session(s); correctness review is not "
                    "running. Check the claude binary/auth, then verify with a new session."
                )
            elif judge_events:
                judge_detail = "reviewer spawning normally in recent sessions"
        checks.append(
            {
                "name": "judge_spawn_health",
                "status": "warn" if judge_warn else "ok",
                "detail": judge_detail,
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "judge_spawn_health",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        from chameleon_mcp.metrics import _metrics_path
        from chameleon_mcp.shadow_report import _iter_rows

        # Rows usually carry file_rel=None (it is only attributed at the block
        # gates); when present, a non-source attribution (README edits) is a
        # normal null-archetype row, so it is excluded rather than counted.
        _source_exts = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".rb")
        recent_rows: list[dict] = []
        if cwd_repo_id:
            for _row in _iter_rows(_metrics_path()):
                if _row.get("hook") != "preflight-and-advise":
                    continue
                if _row.get("repo_id") != cwd_repo_id:
                    continue
                if _row.get("trust_state") != "trusted":
                    continue
                _fr = _row.get("file_rel")
                if isinstance(_fr, str) and not _fr.endswith(_source_exts):
                    continue
                recent_rows.append(_row)
                if len(recent_rows) > 30:
                    recent_rows.pop(0)
        null_count = sum(1 for r in recent_rows if r.get("archetype") is None)
        if len(recent_rows) >= 5 and null_count == len(recent_rows):
            checks.append(
                {
                    "name": "advisory_emission",
                    "status": "warn",
                    "detail": (
                        f"advisories are not firing: the last {len(recent_rows)} trusted "
                        "edits in this repo resolved no archetype; archetype resolution "
                        "may be broken. Run /chameleon-refresh and /chameleon-status."
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "advisory_emission",
                    "status": "ok",
                    "detail": (
                        "no trusted preflight rows recorded for this repo"
                        if not recent_rows
                        else f"{len(recent_rows) - null_count}/{len(recent_rows)} recent "
                        "trusted edits resolved an archetype"
                    ),
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "advisory_emission",
                "status": "ok",
                "detail": f"could not inspect: {type(exc).__name__}: {exc}",
            }
        )

    try:
        from chameleon_mcp import index_db as _index_db_mod

        conn = _index_db_mod.init_index_db()
        row = conn.execute("SELECT v FROM schema_meta WHERE k='schema_version'").fetchone()
        checks.append(
            {
                "name": "index_db",
                "status": "ok",
                "detail": f"schema_version={row[0] if row else 'missing'}",
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "index_db", "status": "warn", "detail": f"{type(exc).__name__}: {exc}"}
        )

    try:
        from chameleon_mcp.bootstrap.transaction import is_committed
        from chameleon_mcp.profile.loader import load_profile_dir

        lp = list_profiles(limit=20)
        profiles = lp.get("data", {}).get("profiles", [])
        repo_states = []
        any_corrupt = False
        for r in profiles:
            root = r.get("repo_root")
            if root and is_committed(Path(root) / ".chameleon"):
                # A committed profile can still be unloadable (corrupt or
                # incomplete JSON); load_profile_dir rejects it on every edit,
                # so report it as corrupt rather than a healthy profile_present.
                try:
                    load_profile_dir(Path(root) / ".chameleon")
                    status = "profile_present"
                except Exception:
                    status = "profile_corrupt"
                    any_corrupt = True
            elif root:
                status = "no_profile"
            else:
                status = "unknown"
            entry = {
                "repo_root": root,
                "profile_status": status,
                "trust_state": r.get("trust_state"),
            }
            # An unresolved git merge leaves conflict markers inside the
            # markdown artifacts (the JSON ones fail their parse and already
            # read as corrupt). Markers in principles/summary inject garbage
            # into sessions and markers in idioms.md hide taught idioms, so
            # surface them here with the resolution.
            if root:
                conflicted = _conflict_marked_artifacts(Path(root) / ".chameleon")
                if conflicted:
                    entry["merge_conflict_markers"] = conflicted
                    entry["resolution"] = (
                        "accept one side of the merge for these files, then "
                        "run /chameleon-refresh to regenerate"
                    )
                    any_corrupt = True
            repo_states.append(entry)
        checks.append(
            {
                "name": "known_repos",
                "status": "warn" if any_corrupt else "ok",
                "detail": repo_states,
            }
        )
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
