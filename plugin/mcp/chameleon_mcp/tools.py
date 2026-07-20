"""MCP tool implementations for chameleon.

Each registered MCP tool is fully implemented and returns the standard API
versioning envelope:
{ "api_version": "1", "data": {...}, "truncated"?: bool, "next_cursor"?: str }
"""

from __future__ import annotations

import contextlib
import contextvars
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


def _sanitize_rules_value(value: object, *, drop_prose_strings: bool = False) -> object:
    """Recursively screen a profile config value (rules.json / enforcement.json)
    for the model surface. Trust now persists across changes, so the staleness gate
    no longer keeps a poisoned-after-grant value out of the model-callable response.

    A dict KEY that trips the prompt-injection scan drops its whole entry: config
    KEYS are always identifiers (eslint/rubocop rule names, source names), so the
    scan never false-drops a legit one. Tag-boundary tokens the scan does not cover
    are neutralized on every surviving string.

    String VALUES are handled by ``drop_prose_strings``:
      - default (rules.json): tag-sanitize only. A lint message template legitimately
        contains ``eval()`` / ``exec()`` / ``system:`` (e.g. "Avoid eval() calls"),
        which trips the scan -- prose-dropping values would blank real rule messages,
        a higher-frequency harm than a rare low-potency poisoned message value.
      - True (enforcement.json): also drop a prose-tripping string value / list item.
        That artifact carries only rule NAMES (identifiers) and engine-generated
        reasons, never free-text messages, so dropping is safe and closes a poisoned
        rule name that reaches the model as an ``active``/``demoted`` list entry.
    """
    from chameleon_mcp.profile.loader import _prose_injection_unsafe
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    if isinstance(value, str):
        if drop_prose_strings and _prose_injection_unsafe(value):
            return ""
        return sanitize_for_chameleon_context(value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if isinstance(k, str) and _prose_injection_unsafe(k):
                continue
            clean_key = sanitize_for_chameleon_context(k) if isinstance(k, str) else k
            out[clean_key] = _sanitize_rules_value(v, drop_prose_strings=drop_prose_strings)
        return out
    if isinstance(value, (list, tuple)):
        if drop_prose_strings:
            return [
                _sanitize_rules_value(v, drop_prose_strings=True)
                for v in value
                if not (isinstance(v, str) and _prose_injection_unsafe(v))
            ]
        return [_sanitize_rules_value(v) for v in value]
    return value


def _write_idioms_atomic(idioms_path: Path, new_content: str) -> None:
    """Write idioms.md via tmp + os.replace, never truncate-in-place.

    idioms.md is a trust-hashed, unregenerable artifact. A plain write_text
    opens O_TRUNC; a crash after the truncate but before the write completes
    leaves a torn file that either fails to parse (carry-forward warning) or,
    worse, parses cleanly as a valid prefix and silently drops the tail idioms.
    A tmp write + os.replace makes the swap atomic, matching the conventions.json
    write path. The caller already holds .idioms.lock, which serializes writers
    but does nothing for crash-atomicity.

    The memory-channel mirror carries per-idiom gists, so EVERY idioms.md write
    re-syncs it here — structurally, not per call site, so a future write path
    cannot forget. Best-effort and content-compared: an unchanged active set
    (e.g. a direct-to-deprecated append) renders identical text and skips the
    mirror rewrite.
    """
    import os as _os

    _tmp = idioms_path.with_suffix(idioms_path.suffix + ".tmp")
    _tmp.write_text(new_content, encoding="utf-8")
    _os.replace(_tmp, idioms_path)
    _sync_conventions_md_from_disk(idioms_path.parent)


def _sanitize_rule_items(items: list) -> list:
    """Sanitize a list of (source_key, config_value) rule entries for the model.

    Both the source key and the (nested) config value are profile-derived
    strings, so both go through _sanitize_rules_value. A source key that trips the
    injection scan drops its whole entry: source keys are identifiers
    (eslint/rubocop/formatting/typescript), so a legit one never trips, mirroring
    the dict-key drop _sanitize_rules_value applies to the nested rule names.
    """
    from chameleon_mcp.profile.loader import _prose_injection_unsafe

    clean: list = []
    for entry in items:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            key, val = entry
            if isinstance(key, str) and _prose_injection_unsafe(key):
                continue
            clean_key = (
                _sanitize_rules_value(key) if isinstance(key, (str, dict, list, tuple)) else key
            )
            clean.append((clean_key, _sanitize_rules_value(val)))
        else:
            clean.append(_sanitize_rules_value(entry))
    return clean


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
    # is_absolute() is lexical, but is_dir() stat-probes the filesystem and on
    # CPython raises ValueError ("embedded null byte") for a NUL-byte arg. repo
    # args do not pass through _validate_file_path_arg, so guard the shape probe
    # here: a malformed repo must yield a not-resolvable result, not an uncaught
    # raise that escapes the tool with no envelope.
    try:
        if not path.is_absolute():
            return None, None
        is_dir = path.is_dir()
    except (OSError, ValueError):
        return None, None
    if is_dir:
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
    from chameleon_mcp.worktree import resolve_profile_root

    # A linked git worktree has no .chameleon of its own; resolve to the main
    # worktree's profile. Identity for every non-worktree root, so the canonical
    # -ref / working-tree behavior below is unchanged off the worktree path.
    working = resolve_profile_root(repo_root) / ".chameleon"
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


def _orphaned_uuid_trust_hint(repo_root: Path, repo_id: str) -> dict | None:
    """Detect an orphaned no-remote trust/history grant left by a lost repo_uuid.

    A no-remote repo's identity falls back to config.json's ``repo_uuid``, then
    to the raw resolved-path hash (see ``_compute_repo_id``). If that uuid is
    lost -- deletion, a bad merge, a restored old backup -- while the repo
    still has no git remote, the freshly computed repo_id silently shifts to
    the path-hash branch with zero diagnostic, unlike the engine-changed-the-
    hash-algorithm case (``_legacy_path_repo_id``), which surfaces
    ``legacy_trust_hint``. This mirrors that mechanism for the uuid-loss case:
    scan the plugin data root for another repo_id's ``.trust`` record whose OWN
    ``repo_root`` resolves to this same working tree. A match under a
    DIFFERENT id than the one just computed means that prior grant (and its
    drift/review history) is now orphaned.

    Fails open (``None``) on any read error, when a git remote exists (the
    uuid branch never applied), or when nothing matches. Bounded by
    ``ORPHANED_TRUST_SCAN_CAP`` so a plugin data dir holding many repos cannot
    make the scan unbounded.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.profile.trust import plugin_data_dir, trust_state_for

    try:
        if _git_remote_url(repo_root):
            return None
        cfg_path = repo_root / ".chameleon" / "config.json"
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("repo_uuid"):
            return None
        # A persisted `production_ref` is only ever auto-stamped for an
        # origin-backed repo (a local-only repo never auto-locks -- see
        # docs/architecture.md "Production-ref derivation"), so its presence
        # here -- alongside no remote existing right now -- is evidence this
        # repo HAD a remote and lost it, not that it lost a repo_uuid it never
        # needed while remote-identified. Attributing the orphaned grant to a
        # missing repo_uuid in that case would be a wrong diagnosis pointing
        # at the wrong remediation (there is no uuid to restore), so bail out
        # rather than guess between two indistinguishable-from-the-hash causes.
        if raw.get("production_ref") is not None:
            return None
        current = str(repo_root.resolve())
    except (OSError, ValueError):
        return None

    try:
        candidate_ids = [d.name for d in plugin_data_dir().iterdir() if d.is_dir()]
    except OSError:
        return None

    cap = threshold_int("ORPHANED_TRUST_SCAN_CAP")
    for candidate_id in candidate_ids[:cap]:
        if candidate_id == repo_id:
            continue
        try:
            rec = trust_state_for(candidate_id)
        except Exception:
            continue
        if rec is None:
            continue
        if rec.repo_root == current or current in rec.repo_root_specific_hashes:
            return {
                "reason": (
                    "This repo has no git remote; its identity derives from "
                    "config.json's repo_uuid, which is now missing. Trust and "
                    "drift/review history were recorded under a different "
                    "(uuid-derived) repo_id for this same working tree."
                ),
                "orphaned_repo_id": candidate_id,
                "current_repo_id": repo_id,
                "recommended_action": (
                    "Restore repo_uuid in .chameleon/config.json if you still "
                    "have it, or re-run /chameleon-trust to grant the new repo_id"
                ),
            }
    return None


def _coordinator_production_ref_path(toplevel: Path) -> Path:
    """Off-profile home for a coordinator-only monorepo's resolved production_ref.

    A coordinator-only bootstrap (status ``success_workspaces_only``) never
    gets its own ``.chameleon/`` -- the root has no language signal of its
    own, so ``_persist_production_ref``'s usual ``.chameleon/config.json``
    target does not exist there and a resolved lock has nowhere to land.
    Mirrors WP-C5's cross-workspace index (``cross_reverse_index.json``):
    a small file keyed by the toplevel's own repo_id, off the trust-hashed
    profile surface, so it survives even though the coordinator root itself
    carries no profile.
    """
    from chameleon_mcp.profile.trust import repo_data_dir

    return repo_data_dir(_compute_repo_id(toplevel)) / "production_ref.json"


def _persisted_coordinator_production_ref(toplevel: Path) -> str | None:
    """Read the coordinator-scoped production_ref lock, if one was persisted.

    Fail-open: any read/parse error, or no lock ever having been persisted,
    returns None -- callers then fall back to their normal no-lock behavior.
    """
    try:
        raw = _coordinator_production_ref_path(toplevel).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ref = data.get("production_ref")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _persist_coordinator_production_ref(toplevel: Path, branch: str) -> None:
    """Stamp the coordinator-scoped production_ref lock (best-effort).

    Same read-modify-write shape as ``_persist_production_ref``, just
    targeting the plugin-data file instead of a (nonexistent) root
    ``.chameleon/config.json``.
    """
    try:
        path = _coordinator_production_ref_path(toplevel)
        existing: dict = {}
        if path.is_file():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    existing = parsed
            except (OSError, json.JSONDecodeError, ValueError):
                existing = {}
        if existing.get("production_ref") == branch:
            return
        existing["production_ref"] = branch
        text = json.dumps(existing, indent=2, sort_keys=True) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
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
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    # A valid-JSON but non-dict profile.json (e.g. a top-level array) would raise
    # AttributeError on .get; guard it so the caller reads "unknown SHA" (None)
    # rather than having the exception swallowed into a false "profile is fresh".
    if isinstance(data, dict):
        src = data.get("derivation_source")
        if isinstance(src, dict):
            sha = src.get("sha")
            if isinstance(sha, str) and sha:
                return sha
    return None


def detect_repo(file_path: str) -> dict:
    """Detect the repo a given file path belongs to.

    The envelope also carries a ``production_branch`` block: for a locked
    repo ``{locked: true, branch, resolvable}``; otherwise the detection
    result ``{locked: false, branch, source, conflict, candidates,
    from_origin}`` the init skill reads to decide whether to auto-lock,
    ask, or skip.

    trust_state values (trust is one-time by default: it persists across profile
    changes and "stale" only occurs under CHAMELEON_TRUST_REVALIDATE=1):
    - "n/a"        — no repo root detected
    - "untrusted"  — repo found, no .trust record
    - "trusted"    — .trust record covers the root. By default this holds even
                     after the profile changed since the grant
    - "stale"      — kill-switch-only (CHAMELEON_TRUST_REVALIDATE=1): the record
                     exists but the profile hash changed since grant; the user
                     re-confirms via /chameleon-trust. Unreachable by default

    Three distinct ``legacy_trust_hint`` surfaces are emitted, mutually
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

    3. **Orphaned repo_uuid hint** (dict, ``_orphaned_uuid_trust_hint``):
       fires when a no-remote repo's ``config.json`` lost its ``repo_uuid``
       (deletion, bad merge, restored old backup), silently shifting the
       computed repo_id to the path-hash branch. Declines (returns nothing)
       when ``config.json`` carries a persisted ``production_ref`` — that
       key is only ever auto-stamped for an origin-backed repo, so its
       presence alongside "no remote right now" means this repo more likely
       lost its git remote than a repo_uuid it never needed.
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
    if not p.is_absolute():
        # detect_repo takes no `repo` arg to resolve a relative path against
        # (unlike get_archetype/get_callers/etc.), so a relative file_path
        # would otherwise silently resolve against the MCP server process's
        # own CWD via find_repo_root -- disclosing whatever repo happens to be
        # there instead of failing on input the caller never fully specified.
        # Reject it outright rather than guess.
        return _envelope(
            {
                "repo_id": None,
                "repo_root": None,
                "profile_status": "no_repo",
                "trust_state": "n/a",
                "reason": "file_path must be an absolute path",
            }
        )
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
    from chameleon_mcp.worktree import resolve_profile_root

    # repo_id is git-remote-derived (identical across a repo's worktrees); the
    # profile and trust live at the main worktree, which a linked worktree has
    # none of its own. resolve_profile_root is the identity off the worktree path.
    profile_dir = resolve_profile_root(repo_root) / ".chameleon"
    profile_file = profile_dir / "profile.json"
    profile_present = profile_file.exists()
    trust = trust_state_for(repo_id)

    profile_corrupted = False
    profile_unsupported_schema = False
    profile_too_new = False
    profile_framework: str | None = None
    profile_language: str | None = None
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

            if isinstance(_peek, dict):
                _fw = _peek.get("framework")
                if isinstance(_fw, str) and _fw:
                    profile_framework = _fw
                _lang = _peek.get("language")
                if isinstance(_lang, str) and _lang:
                    profile_language = _lang
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

    if not (profile_corrupted or profile_unsupported_schema or profile_too_new):
        # profile.json alone is not enough: a missing/corrupt/wrong-type core
        # artifact (archetypes/canonicals/rules) or a generation mismatch makes
        # load_profile_dir refuse and the hooks fail open, so detect_repo must not
        # report profile_present/trusted -- matching get_pattern_context and
        # get_status on the identical directory state. Called even when
        # profile.json itself is absent: _profile_unrenderable_status reports
        # profile_corrupted for a torn-down profile (core artifacts remain but
        # the commit sentinel is gone) and None for a genuine no-profile dir,
        # so this must not be gated on profile_present.
        _unrend = _profile_unrenderable_status(profile_dir)
        if _unrend == "profile_corrupted":
            profile_corrupted = True
        elif _unrend == "profile_unsupported_schema_version":
            profile_unsupported_schema = True

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
    # Probe form, not trust_state_for: this speculates about an identity that
    # usually has no data, and the reading form would mkdir a permanently
    # orphaned directory per probed repo.
    from chameleon_mcp.profile.trust import trust_state_probe as _trust_state_probe

    if trust is None and legacy_id != repo_id and _trust_state_probe(legacy_id) is not None:
        legacy_trust_hint_value = (
            "Trust record found at the legacy path-derived repo_id "
            f"{legacy_id[:8]}…; the canonical repo_id is now derived from the "
            "git remote URL. Run /chameleon-trust to re-grant under the new id."
        )
        legacy_repo_id_value = legacy_id

    if trust is None and legacy_trust_hint_value is None:
        # A no-remote repo whose repo_uuid vanished shifts silently to the
        # path-hash repo_id with no diagnostic at all, unlike the migration
        # case just above -- check for that orphaned uuid-derived grant too.
        legacy_trust_hint_value = _orphaned_uuid_trust_hint(repo_root, repo_id)

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
    # Descriptive profile metadata, surfaced when a healthy profile declares it.
    if profile_language is not None:
        data["language"] = profile_language
    if profile_framework is not None:
        data["framework"] = profile_framework
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
        from chameleon_mcp.worktree import resolve_profile_root

        # Hint reads the main worktree's config in a linked worktree (identity
        # off the worktree); repo_root stays the worktree for repo_id / repo_root.
        _lock_root = resolve_profile_root(repo_root)
        locked_branch = _persisted_production_ref(_lock_root)
        if locked_branch is None and not _production_ref_explicitly_disabled(_lock_root):
            # Monorepo workspace inheritance: only the toplevel auto-locks at
            # bootstrap (the root profile), and a workspace does not inherit
            # the lock into its OWN config.json until its first refresh
            # (mirrors refresh_repo's production-ref resolution). Without this
            # walk, a workspace immediately after init reports locked:false
            # even though its profile was derived from the locked production
            # branch. Read-only: this mirrors the lock for reporting, it does
            # not persist it into the workspace's config.
            from chameleon_mcp.production_ref import git_toplevel

            _toplevel = git_toplevel(_lock_root)
            if (
                _toplevel is not None
                and _toplevel != _lock_root.resolve()
                and not _production_ref_explicitly_disabled(_toplevel)
            ):
                locked_branch = _persisted_production_ref(_toplevel)
                if locked_branch is None:
                    # A coordinator-only toplevel (no language of its own,
                    # bootstrap status success_workspaces_only) never gets a
                    # root .chameleon/config.json to carry the lock, so the
                    # check above always misses -- fall back to the
                    # coordinator-scoped plugin-data lock bootstrap persisted
                    # there instead.
                    locked_branch = _persisted_coordinator_production_ref(_toplevel)
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
    from chameleon_mcp.signatures import python_role_for_path

    file_dir = rel_str.rsplit("/", 1)[0] if "/" in rel_str else ""
    file_segments = [s for s in file_dir.split("/") if s]
    file_ext = rel_str.rsplit(".", 1)[-1] if "." in rel_str.rsplit("/", 1)[-1] else ""
    # A framework role signal (blueprints/, routes/, models/, ...) beats raw
    # cluster size as the tiebreak: a brand-new Flask blueprint under blueprints/
    # must not fall to the largest "util" cluster just because it shares one
    # leading dir segment. python_role_for_path returns None for non-Python files,
    # so TS/Ruby scoring is unchanged.
    file_role = python_role_for_path(rel_str)
    scored: list[tuple[int, int, int, str]] = []
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
        role_match = 0
        if file_role is not None:
            arch_role = python_role_for_path(arch_dir.rstrip("/") + "/_probe.py")
            if arch_role == file_role:
                role_match = 1
        cluster_size = int(arch.get("cluster_size") or 0)
        scored.append((-overlap, -role_match, -cluster_size, name))
    if not scored:
        return None, []
    scored.sort()
    primary = scored[0][3]
    alternatives = [name for _o, _r, _c, name in scored[1:]]
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
    if language not in ("typescript", "ruby", "python"):
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
    if not p.is_absolute():
        # Mirror the call-graph tools (v2.38.1): a repo-relative file_path is a
        # natural input form, so resolve it against the repo arg's root before
        # find_repo_root, which otherwise walks up from the server CWD and
        # silently returns archetype=null with file_exists=false.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()

    content_signal_value: str = _content_signal_for_path(p)

    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(_empty_archetype_envelope(content_signal_value, p.is_file()))

    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + archetype lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible negative.
    # Proceed with the file's own repo so the answer reflects the workspace the
    # file lives in; trust is still gated on expected_repo_id below.
    expected_repo_id = _compute_repo_id(repo_root)

    # Trust-gate: archetype classification is profile-derived content served from
    # the committed, attacker-controllable .chameleon/ profile, so refuse it from
    # an untrusted profile like every sibling read tool does. Internal hook
    # callers already gate trust upstream, so on a trusted repo this is a no-op.
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    _gate = _trust_state_for(expected_repo_id)
    if _gate is None or not _gate.grants_root(repo_root):
        _untrusted = _empty_archetype_envelope(content_signal_value, p.is_file())
        _untrusted["status"] = "untrusted"
        return _envelope(_untrusted)

    profile_dir = _effective_profile_dir(repo_root)
    try:
        loaded: LoadedProfile = load_profile_dir(profile_dir)
    except Exception:
        # A corrupt / unloadable profile is NOT a clean "no archetype match":
        # collapsing it to the empty no-match payload hid a torn profile as
        # healthy (every sibling read tool reports degraded here). Flag it so the
        # caller and the per-edit hook treat guidance as unknown, not clean.
        _degraded = _empty_archetype_envelope(content_signal_value, p.is_file())
        _degraded["status"] = "degraded"
        _degraded["reason"] = "profile_unavailable"
        return _envelope(_degraded)

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
    if language not in ("typescript", "ruby", "python"):
        language = None
    snapshot = extract_dimensions(content, language=language, file_path=str(p))

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
    # A parseable profile that declares a newer engine_min_version is not corrupt
    # either -- it needs a chameleon upgrade, not a re-derive. Distinguish it so
    # the degraded banner (and detect_repo) steer to upgrade, not a dead-end
    # /chameleon-refresh on the too-old engine.
    try:
        if _profile_requires_newer_engine(profile_file.parent) is not None:
            return "profile_too_new"
    except Exception:
        pass
    return "profile_corrupted"


_IDIOM_ARCHETYPE_LINE_RE = re.compile(r"(?im)^[ \t]*Archetype:[ \t]*(.+?)[ \t]*$")


def _reorder_idioms_by_archetypes(idioms_text: str, archetypes) -> str:
    """Surface idioms for the given archetypes FIRST so a downstream char-cap
    truncation keeps them.

    idioms.md is a sequence of ``### <name>`` blocks, each carrying an optional
    ``Archetype: <name>`` line. The per-edit block and the turn-end self-review
    nudge both cap the text by taking its top, so an unrelated archetype's idioms
    at the top of the file can crowd out the ones relevant to what was edited.
    Reorder (not filter, so nothing is lost) into three stable groups: blocks
    matching ANY given archetype, then untagged/general blocks, then
    other-archetype blocks. Empty archetype set, no ``### `` blocks, or nothing
    matching -> returned unchanged.

    Shares :func:`_parse_idiom_blocks` so the header split is fence-aware here too:
    a ``### `` line inside an idiom's fenced example is example code, not a block
    boundary, and never becomes a spurious reorder unit.
    """
    wanted = {a.strip().lower() for a in archetypes if a and a.strip()}
    if not wanted:
        return idioms_text
    preamble, blocks = _parse_idiom_blocks(idioms_text)
    if not blocks:
        return idioms_text
    matching: list[str] = []
    general: list[str] = []
    other: list[str] = []
    for _name, arch, text in blocks:
        if arch is None:
            general.append(text)
        elif arch in wanted:
            matching.append(text)
        else:
            other.append(text)
    if not matching:
        return idioms_text
    return preamble + "".join(matching + general + other)


def _reorder_idioms_by_archetype(idioms_text: str, archetype: str | None) -> str:
    """Single-archetype wrapper over :func:`_reorder_idioms_by_archetypes` for the
    per-edit path, which resolves exactly one archetype for the edited file."""
    return _reorder_idioms_by_archetypes(idioms_text, [archetype] if archetype else [])


# Metadata lines inside a `### <name>` idiom block: skipped when extracting the
# one-line summary, since they carry no descriptive prose. Mirrors
# idiom_coverage.py's sibling parser, which also treats Source: (provenance:
# a doc path:line, a git ref, or a note) as metadata, not description prose.
_IDIOM_META_LINE_RE = re.compile(r"(?i)^[ \t]*(Language|Status|Archetype|Source):")


def _active_idioms_only(idioms_text: str) -> str:
    """idioms.md with the ``## deprecated`` section removed.

    Deprecated idioms are RETIRED guidance -- a team deprecates an idiom exactly
    when it no longer applies. They must not be injected into the per-edit
    spotlight or re-checked at the Stop idiom self-review as if active. Cuts
    everything from the first ``## deprecated`` heading onward.

    Fence-aware, matching :func:`_parse_idiom_blocks`'s ``` tracking: a taught
    idiom's ``Example:``/``Counterexample:`` body can itself contain a line that
    looks like ``## deprecated`` (arbitrary user prose), and treating that as the
    real section marker would truncate every idiom that follows it.
    """
    if not idioms_text:
        return idioms_text
    lines = idioms_text.splitlines(keepends=True)
    in_fence = False
    offset = 0
    for ln in lines:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"(?i)\s*##\s+deprecated\b", ln):
            return idioms_text[:offset].rstrip() + "\n"
        offset += len(ln)
    return idioms_text


def _parse_idiom_blocks(idioms_text: str):
    """Split idioms.md into ``(preamble, blocks)``.

    Each block is ``(name, archetype, text)`` where ``name`` is the ``### <name>``
    header text, ``archetype`` is the lowercased ``Archetype:`` value or ``None``
    (untagged/general), and ``text`` is the full block verbatim (header through the
    line before the next header). ``preamble`` is everything before the first
    header. No ``### `` headers -> ``(idioms_text, [])`` so the caller can fall back
    to a raw dump rather than silently dropping non-standard content.

    A ``### `` line INSIDE a fenced code block (an idiom's ``Example:`` /
    ``Counterexample:`` snippet -- e.g. a Ruby/shell ``### comment`` or a markdown
    heading) is NOT a header: it is example code. Tracking ```` ``` ```` fences
    prevents a snippet line from splitting into a spurious block whose "gist" would
    then be rendered as a real idiom -- the exact confusion the terse rendering
    must avoid. Fences are assumed balanced (idioms.md is chameleon-generated); an
    unbalanced fence at worst merges a later real block into the prior one (grouped,
    never leaked as its own idiom).
    """
    # Defense in depth for callers that pass a raw idioms.md: never collect blocks
    # from the ## deprecated section (retired guidance must not be re-imposed).
    idioms_text = _active_idioms_only(idioms_text)
    lines = idioms_text.splitlines(keepends=True)
    starts = []
    in_fence = False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and ln.startswith("### "):
            starts.append(i)
    if not starts:
        return idioms_text, []
    preamble = "".join(lines[: starts[0]])
    blocks = []
    for j, s in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(lines)
        text = "".join(lines[s:end])
        name = lines[s][4:].strip()
        m = _IDIOM_ARCHETYPE_LINE_RE.search(text)
        arch = m.group(1).strip().lower() if m else None
        blocks.append((name, arch, text))
    return preamble, blocks


def _summarize_idiom_block(block_text: str, *, max_chars: int) -> str:
    """First prose sentence of an idiom block, for the terse turn-end rendering.

    Skips the ``### name`` header and the ``Language/Status/Archetype/Source``
    metadata lines, then takes the first description paragraph and returns its first sentence
    (hard-capped to ``max_chars``). Stops at the ``Example:`` / ``Counterexample:``
    label or a code fence so no example code bleeds into the summary. ``""`` when the
    block carries no description (the caller then renders the name alone).
    """
    lines = block_text.splitlines()
    desc: list[str] = []
    for ln in lines[1:]:  # skip the '### name' header
        s = ln.strip()
        if not s:
            if desc:
                break  # blank line ends the description paragraph
            continue
        if _IDIOM_META_LINE_RE.match(ln):
            continue
        low = s.lower()
        if low.startswith("example:") or low.startswith("counterexample:") or s.startswith("```"):
            break
        desc.append(s)
    if not desc:
        return ""
    text = " ".join(desc)
    # First sentence: sentence-ending punctuation FOLLOWED BY whitespace or end, so
    # an intra-token dot ("errors.count", "spec_helper.rb") never cuts it short.
    m = re.search(r"^(.*?[.!?])(?:\s|$)", text)
    summary = m.group(1) if m else text
    if len(summary) > max_chars:
        # Idiom directives often lead with a long parenthetical enumeration
        # ("Write every derived profile artifact (a.json, b.json, ...) only
        # inside ..."): a hard cut inside that list would drop the verbs the
        # summary exists to carry, so elide long parentheticals first and cut
        # only if the sentence is still over budget.
        summary = _elide_long_parens(summary)
        if len(summary) > max_chars:
            summary = summary[:max_chars].rstrip() + "..."
    return summary


# Parenthetical spans this long carry enumerations/asides, not operators like
# ``threshold_int("X")`` — only these are elided when a summary runs over cap.
# A span may contain earlier-pass ``(...)`` markers (so a long outer aside
# around an elided inner one still collapses), but never any other paren.
_PAREN_SPAN_RE = re.compile(r"\((?:[^()]|\(\.\.\.\)){20,}\)")


def _elide_long_parens(text: str) -> str:
    """Replace long ``(...)`` spans with a literal ``(...)`` marker.

    Innermost-first with a bounded pass count, so nested parentheticals converge
    to a single marker; the bare 3-char marker itself never re-matches. Short
    spans (call syntax, one-word asides) are kept verbatim. Invariant the tests
    pin: the marker's inner ``...`` must stay shorter than the regex's span
    floor, or the marker would re-collapse forever (bounded only by the pass
    count) — shrink the floor below 5 only together with a new marker.
    """
    for _ in range(4):
        new = _PAREN_SPAN_RE.sub("(...)", text)
        if new == text:
            return new
        text = new
    return text


# Header line of the mirror's idiom-digest section. Owned here, next to its
# producer (render_idiom_gists), so the section grammar stays a single source
# of truth for what conventions.py's render_conventions_md emits.
MIRROR_IDIOMS_HEADER = (
    "TEAM IDIOMS (taught; follow on every edit — full text with examples in .chameleon/idioms.md):"
)


def render_idiom_gists(idioms_text: str) -> str:
    """One ``- name: gist`` line per active idiom, for the conventions.md mirror.

    The mirror rides the CLAUDE.md memory channel (materially higher instruction
    authority than hook injection — migration A/B 2026-07-11), so carrying each
    taught idiom's name + first-sentence directive there makes the rule ambient in
    every session without any per-turn injection; the Stop self-review can then
    reference these idioms by gist instead of re-dumping full text. Deprecated
    blocks are excluded (``_parse_idiom_blocks`` is fed active-only text), output
    is sanitized at the boundary, and ``""`` means "no section" to the caller.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    if not idioms_text or not idioms_text.strip():
        return ""
    _pre, blocks = _parse_idiom_blocks(idioms_text)
    if not blocks:
        return ""
    max_items = threshold_int("MIRROR_IDIOM_MAX_ITEMS")
    gist_chars = threshold_int("MIRROR_IDIOM_GIST_CHARS")
    lines: list[str] = []
    for name, _arch, text in blocks[:max_items]:
        nm = name.strip()
        if not nm:
            continue
        gist = _summarize_idiom_block(text, max_chars=gist_chars)
        lines.append(f"- {nm}: {gist}" if gist else f"- {nm}")
    if not lines:
        return ""
    overflow = len(blocks) - max_items
    if overflow > 0:
        lines.append(f"- (+{overflow} more; see .chameleon/idioms.md)")
    return sanitize_for_chameleon_context("\n".join(lines))


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
    if not p.is_absolute():
        # get_pattern_context takes no `repo` arg to resolve a relative path
        # against (unlike get_archetype/get_callers/etc.), so a relative
        # file_path would otherwise silently resolve against the MCP server
        # process's own CWD via find_repo_root -- disclosing whatever repo
        # happens to be there instead of failing on input the caller never
        # fully specified. Reject it outright rather than guess.
        return _envelope(_empty_pattern_envelope(None, "no_repo", "n/a"))

    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope(_empty_pattern_envelope(None, "no_repo", "n/a"))

    repo_id = _compute_repo_id(repo_root)
    profile_dir = _effective_profile_dir(repo_root)
    profile_file = profile_dir / "profile.json"
    if not profile_file.exists():
        # profile.json alone missing is not necessarily a clean no-profile dir:
        # a torn-down profile that still carries core artifacts (archetypes/
        # canonicals/rules) is corrupt, not absent -- matching detect_repo and
        # get_status on the identical directory state.
        _unrend = _profile_unrenderable_status(profile_dir)
        _status = _unrend if _unrend is not None else "no_profile"
        return _envelope(_empty_pattern_envelope(repo_id, _status, "n/a"))

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

    # Trust gate (parity with the public get_archetype, which refuses an untrusted
    # profile): an untrusted profile is attacker-controllable, so its archetype
    # CLASSIFICATION, witness PATH, and sha_hint must not be disclosed -- not only
    # the canonical content. _get_archetype_with_loaded bypasses get_archetype's
    # gate, so null the archetype here; the witness/sha block below is keyed on
    # arch_data["archetype"] and therefore stays empty, and the canonical_excerpt
    # keeps its untrusted redaction marker.
    if trust_state_str == "untrusted":
        arch_data = _empty_archetype_envelope(content_signal_value, p.is_file())

    if arch_data.get("archetype"):
        arch_entry = loaded.archetypes.get("archetypes", {}).get(arch_data["archetype"], {}) or {}
        sub_buckets = arch_entry.get("sub_buckets") or {}
        arch_data["sub_buckets_count"] = len(sub_buckets) if isinstance(sub_buckets, dict) else 0
        summary = arch_entry.get("summary") or ""
        if summary:
            # Free prose from archetypes.json (attacker-controllable in a committed
            # profile). Two defenses, for parity with the idioms/principles prose:
            # (1) drop it entirely if it trips the prompt-injection scan -- trust
            # persists across profile changes, so the staleness gate no longer keeps
            # a poisoned-after-grant summary out of the model-callable response; then
            # (2) sanitize tag-escape / forged-header tricks the scan does not cover.
            from chameleon_mcp.profile.loader import _prose_injection_unsafe
            from chameleon_mcp.sanitization import sanitize_for_chameleon_context

            if _prose_injection_unsafe(summary):
                summary = ""
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
            # When an archetype has several witnesses, prefer one that still exists
            # on disk: the nearest-by-shape entry may have been deleted/renamed in
            # the working tree, and selecting it would flag the whole archetype's
            # witness as missing while live sibling witnesses of the SAME archetype
            # go unused. Fall through to the nearest LIVE witness; keep the full
            # list only if none are live, so the missing-witness flag still fires.
            selection_pool = canonicals
            if len(canonicals) > 1:

                def _witness_live(c: object) -> bool:
                    try:
                        wp = (c.get("witness") or {}).get("path") if isinstance(c, dict) else None
                        return bool(wp) and (repo_root / wp).is_file()
                    except OSError:
                        return False

                _live = [c for c in canonicals if _witness_live(c)]
                if _live:
                    selection_pool = _live
            first = _nearest_canonical_entry(rel_str, selection_pool, snapshot=snapshot)
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
                        # The witness passed the secret/injection scan at bootstrap,
                        # but the working-tree file may have been edited since (trust
                        # is one-time, so a post-grant edit never re-prompts). Re-scan
                        # the freshly-read content and drop it on a hit, exactly as
                        # get_canonical_excerpt does -- otherwise a secret or natural-
                        # language injection added to a committed witness after the
                        # grant reaches model context on the per-edit hot path
                        # (sanitize_for_chameleon_context keeps secrets and does not
                        # neutralize injection prose). Fail-open if the scanner can't
                        # be imported, matching the get_canonical_excerpt path.
                        try:
                            from chameleon_mcp.bootstrap.canonical_scanner import (
                                is_safe_canonical,
                            )

                            if not is_safe_canonical(raw):
                                return "", False
                        except Exception:
                            pass
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

    # Deprecated idioms are retired guidance: strip the ## deprecated section
    # before any injection / idiom-review so a retired rule is never re-imposed.
    idioms_text = _active_idioms_only(loaded.idioms_text or "")
    if idioms_text:
        # A scaffold-only idioms.md ("## active" + "_(no idioms yet …)_") is the
        # common case (most repos never run /chameleon-teach). Treat it as empty:
        # injecting the scaffold into the per-edit spotlight or the idiom judge
        # spends the model's attention on a "no idioms yet" placeholder framed as
        # content to imitate. Real content (active OR deprecated blocks, or a
        # hand-edited file) still flows.
        from chameleon_mcp.idiom_coverage import has_idiom_content

        if not has_idiom_content(idioms_text):
            idioms_text = ""
    if idioms_text:
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context

        idioms_text = sanitize_for_chameleon_context(idioms_text)
        # Surface the edited file's archetype idioms first so the per-edit block's
        # (and the idiom judge's) char-cap keeps them instead of truncating them
        # away behind an unrelated archetype's idioms at the top of idioms.md.
        idioms_text = _reorder_idioms_by_archetype(idioms_text, arch_data.get("archetype"))

    # Rule keys and config values are profile-derived strings; sanitize them
    # before they reach the model, matching get_rules' direct-call path.
    rules_out = _sanitize_rule_items(list(loaded.rules.get("rules", {}).items()))

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

    # Pause gate. pause_session's ``.pause_until`` marker is meant to quiet
    # chameleon's advisory guidance for the requested window, but only the
    # PreToolUse hook checked it before calling this tool -- a direct call
    # here (bypassing the hook) got the exact same guidance, active pause or
    # not. Blank the same guidance-bearing fields the untrusted gate blanks;
    # metadata (archetype name, witness_path, trust_state) still flows so a
    # caller can tell "paused" from "no guidance available". Session-scoped
    # disable is intentionally not checked here (this tool carries no
    # session_id); repo_skip / CHAMELEON_DISABLE stay hook-only by design.
    from chameleon_mcp.optouts import is_chameleon_suppressed

    paused = is_chameleon_suppressed(repo_root, repo_id) == "pause"
    if paused:
        canonical_data = {**canonical_data, "content": "", "redacted_reason": "paused"}
        idioms_text = ""
        arch_data.pop("summary", None)
        rules_out = []

    return _envelope(
        {
            "repo": {
                "id": repo_id,
                "profile_status": "profile_present",
                "trust_state": trust_state_str,
                "paused": paused,
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

    # never-raise / fail-open contract: a non-string archetype (a JSON array or
    # object from a malformed MCP call) would raise `TypeError: unhashable type`
    # at the `archetype not in known_archetypes` dict membership test BEFORE the
    # trust gate. Reject it with a typed envelope instead, like get_rules does.
    if not isinstance(archetype, str):
        return _envelope(
            {
                "status": "failed",
                "error": "archetype must be a string",
                "content": None,
                "witness_path": None,
                "truncated": False,
                "sha_hint": None,
            }
        )

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

    def _with_repo_root(env: dict) -> dict:
        """Attach the physical repo_root actually resolved and served from.

        A bare repo_id argument can resolve to any of several physical
        checkouts sharing that id (e.g. two local clones of the same git
        remote) -- ``_resolve_repo_root_by_id`` picks one deterministically
        but silently, with no way for the caller to tell it apart from a
        single-clone result. Surfacing the resolved root lets a caller
        detect a mismatch against the checkout it actually meant.
        """
        env.setdefault("repo_root", str(repo_root))
        return _envelope(env)

    # Explicit-path / by-id resolution bypasses find_repo_root, so re-apply the
    # unsafe-root guard here: a profile planted under /tmp or a world-writable
    # dir by another local user must not be served to the model surface.
    _unsafe = _unsafe_root_refusal(repo_root)
    if _unsafe is not None:
        return _with_repo_root(
            {
                "status": "failed",
                "error": _unsafe,
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
        return _with_repo_root(
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
            return _with_repo_root(
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
    # A structurally-malformed value (a non-list, or a list whose first entry is
    # not a dict, or a non-dict `witness`) can pass _loads_hardened, which only
    # validates the top-level object shape. Guard every access so a corrupt or
    # hand-/merge-mangled canonicals.json DEGRADES rather than raising: the tool
    # contract is never-raise / fail-open, and this crash sits BEFORE the trust
    # gate below, so an untrusted attacker-controllable profile must not crash it.
    if not isinstance(canonicals, list) or not canonicals:
        return _with_repo_root(
            {
                "status": "no_witness",
                "reason": (
                    "archetype has no canonical witness (all candidates excluded "
                    "from the canonical pool, e.g. test/legacy paths; or trivial/"
                    "empty; or below the confidence threshold; or none passed the "
                    "secret/injection scans)"
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
    witness = first.get("witness", {}) if isinstance(first, dict) else {}
    if not isinstance(witness, dict):
        witness = {}
    witness_rel = witness.get("path")
    # sha_hint comes from the committed (attacker-controllable) canonicals.json
    # and is emitted from the pre-trust-gate no_witness branch below, so sanitize
    # it before it reaches the model surface. (The untrusted branch nulls both
    # path and hint; the post-gate branches are behind the trust gate and re-scan
    # the witness content separately.)
    _raw_sha = witness.get("sha_hint")
    _safe_sha = sanitize_for_chameleon_context(str(_raw_sha)) if _raw_sha is not None else None
    if not witness_rel:
        return _with_repo_root(
            {
                "status": "no_witness",
                "reason": (
                    "archetype has no canonical witness (all candidates excluded "
                    "from the canonical pool, e.g. test/legacy paths; or trivial/"
                    "empty; or below the confidence threshold; or none passed the "
                    "secret/injection scans)"
                ),
                "archetype_name": archetype,
                "repo_id": repo_id,
                "content": None,
                "witness_path": None,
                "truncated": False,
                "sha_hint": _safe_sha,
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
        # An untrusted profile is attacker-controllable, so emit NO profile-derived
        # string from it: witness_rel / sha_hint come straight from canonicals.json
        # and could carry ANSI / newline / injection prose to the model surface.
        # Withhold them (null), matching the sibling read tools' untrusted contract.
        return _with_repo_root(
            {
                "status": "untrusted",
                "reason": "profile is not trusted for this user; grant with /chameleon-trust",
                "archetype_name": archetype,
                "repo_id": gate_repo_id,
                "content": None,
                "witness_path": None,
                "truncated": False,
                "sha_hint": None,
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
        return _with_repo_root(
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
        return _with_repo_root(
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
            return _with_repo_root(
                {
                    "content": "",
                    "witness_path": witness_rel,
                    "truncated": False,
                    "missing": True,
                    "sha_hint": witness.get("sha_hint"),
                }
            )
        # Security rejection (traversal, symlink, etc.): leave content empty.
        return _with_repo_root(
            {
                "content": "",
                "witness_path": witness_rel,
                "truncated": False,
                "sha_hint": witness.get("sha_hint"),
            }
        )
    except OSError:
        # Read error or other I/O failure: leave content empty.
        return _with_repo_root(
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
            return _with_repo_root(
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
    return _with_repo_root(
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

    # A non-string source would crash the `source in str(k)` substring filter
    # below; the never-raise contract requires a clean envelope instead.
    if source is not None and not isinstance(source, str):
        return _envelope({"status": "failed", "error": "source must be a string or null"})

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
        # Mark the unresolvable case so it is not read as a healthy zero-rules
        # repo (which returns a bare {"rules": []}); the untrusted / degraded
        # branches below carry a status the same way.
        env = {"rules": [], "status": "unresolved"}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _envelope(env)

    def _with_repo_root(env: dict) -> dict:
        """Attach the physical repo_root actually resolved and served from.

        A bare repo_id argument can resolve to any of several physical
        checkouts sharing that id (e.g. two local clones of the same git
        remote) -- ``_resolve_repo_root_by_id`` picks one deterministically
        but silently, with no way for the caller to tell it apart from a
        single-clone result. Surfacing the resolved root lets a caller
        detect a mismatch against the checkout it actually meant.
        """
        env.setdefault("repo_root", str(repo_root))
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
        return _with_repo_root(env)

    try:
        loaded = load_profile_dir(_effective_profile_dir(repo_root))
    except Exception:
        # A repo with no configured lint rules and a repo whose profile failed
        # to load must not look identical: the caller needs the degraded flag
        # to avoid reading corruption as "nothing to enforce".
        env = {"rules": [], "status": "degraded", "reason": "profile_unavailable"}
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _with_repo_root(env)

    rules_dict = loaded.rules.get("rules", {}) or {}
    # _loads_hardened validates only the top-level object shape, so a corrupt
    # rules.json whose `rules` value is a truthy non-dict (a JSON list/string/int)
    # survives the load. Coerce it to an empty dict so the parse-warning loop and
    # source iteration below cannot raise -- get_rules must degrade, not crash.
    if not isinstance(rules_dict, dict):
        rules_dict = {}

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

    # The rule keys and config VALUES are profile-derived strings, so sanitize
    # them on the way to the model surface (the per-source parse_warning above is
    # already scrubbed; this closes the asymmetry for the rule entries beside it).
    if source is None:
        env = _with_warnings({"rules": _sanitize_rule_items(list(rules_dict.items()))})
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _with_repo_root(env)

    if source in rules_dict:
        env = _with_warnings({"rules": _sanitize_rule_items([(source, rules_dict[source])])})
        if deprecation_note:
            env["deprecation"] = deprecation_note
        return _with_repo_root(env)

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
        return _with_repo_root(env)

    filtered = [(k, v) for k, v in rules_dict.items() if source in str(k)]
    env = {"rules": _sanitize_rule_items(filtered)}
    if deprecation_note:
        env["deprecation"] = deprecation_note
    return _with_repo_root(env)


def lint_file(
    repo: str,
    archetype: str,
    content: str,
    file_path: str | None = None,
    content_truncated: bool | None = None,
) -> dict:
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
    if file_path is not None and not isinstance(file_path, str):
        # Optional arg: a non-string file_path (a list/dict from a malformed call)
        # would crash the language/sink detection below (`.lower()` on a list).
        # Drop it -- the secret and structural scans still run; only the
        # path-derived sink/language scan is skipped.
        file_path = None

    content_size = len(content)
    # `content_truncated` lets a caller that already capped an oversized file to
    # its prefix (the PostToolUse hook reads file[:100_000] on the hot path) tell
    # us the received content is a prefix. Without it, content_size sits at/under
    # the cap so the size check alone reads the content as whole, and every export
    # defined past the cap reads as removed -- spurious removed-export violations
    # on any file above the cap. Honoring the caller's signal skips the
    # removed-export check exactly as an in-house over-cap read would.
    truncated = content_size > 100_000 or bool(content_truncated)
    working_content = content[:100_000] if content_size > 100_000 else content

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
    if _sink_lang not in ("typescript", "ruby", "python"):
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
    if language not in ("typescript", "ruby", "python"):
        language = None

    snapshot = _extract_dimensions(working_content, language=language, file_path=file_path)
    best_ast_violations: list = []
    best_confidence = 0.0
    best_struct_count = float("inf")
    for cq in candidate_queries:
        v_list = _lint(snapshot, cq, language=language)
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
        # class_contract drives the missing-required-method advisory. Threaded here
        # too so the rule fires on this path (the daemon lints via this tool); the
        # in-process posttool path threads it identically.
        if conv_data.get("class_contract", {}).get(archetype):
            arch_conv["class_contract"] = conv_data["class_contract"][archetype]
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
                # A >100KB file is scanned as a capped prefix; when that prefix
                # happens to parse cleanly, every export defined past the cap
                # would read as removed. The flag skips the removed-export
                # check; importer-count advisories still apply to visible names.
                content_truncated=truncated,
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


# Languages with ANY cross-file existence surface for get_crossfile_context:
# the reverse index (TS/Python) or the Ruby constant graph. A profile in one of
# these that still cannot load an index is damaged, not unsupported.
_CROSSFILE_SURFACE_LANGUAGES: frozenset[str] = frozenset({"typescript", "python", "ruby"})


def _crossfile_unavailable_reason(
    repo_root: Path, *, surfaces: frozenset[str] | None = None
) -> str:
    """Why no cross-file index loads here: unsupported language vs damage.

    ``surfaces`` is the set of profile languages whose bootstrap writes the
    index the caller just failed to load. A stored language outside that set
    makes the absence by design (``unsupported-language``); a language inside
    it means the artifact should exist, so its absence is damage
    (``index-unavailable``, repaired by /chameleon-refresh). The old shape of
    this check reported ``typescript-only`` for every non-TS profile, which
    mislabeled a damaged Python reverse index as by-design and suppressed the
    repair suggestion (the indexes have been built for Python too since the
    Python program landed).
    """
    from chameleon_mcp.enforcement_calibration import _stored_profile_languages
    from chameleon_mcp.symbol_index import REVERSE_INDEXED_LANGUAGES

    if surfaces is None:
        surfaces = REVERSE_INDEXED_LANGUAGES
    langs = _stored_profile_languages(_effective_profile_dir(repo_root))
    if langs and not (langs & surfaces):
        return "unsupported-language"
    return "index-unavailable"


def _ruby_constant_importers(repo_root: Path, file_path: Path) -> dict:
    """query_symbol_importers for Ruby: the constants the edited file defines and
    the files that reference each (the constant-reference blast radius).

    Ruby has no named-export reverse index, so the TS "importers/broken-export"
    pair maps to: ``importers`` = each defined constant with its referencing
    files (the rename blast radius), ``broken`` = [] (no removed-named-export
    class -- a removed-method-still-called check would need method-level data the
    constant index does not carry). Every site is a row the bootstrap recorded.
    """
    from chameleon_mcp.constant_index import (
        constants_defined_in,
        load_constant_index,
        referencing_files,
    )
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context

    empty = {
        "found": False,
        "module": None,
        "importers": [],
        "broken": [],
        "export_set_open": False,
    }
    index = load_constant_index(repo_root)
    if index is None:
        out = dict(empty)
        # Only a Ruby profile ever writes constant_index.json. A stray .rb
        # file in a TS/Python-profiled repo lands here by file extension, and
        # suggesting a refresh there is a dead-end loop (refresh will never
        # create the artifact) — that absence is by design, same contract as
        # _crossfile_unavailable_reason.
        from chameleon_mcp.enforcement_calibration import _stored_profile_languages

        langs = _stored_profile_languages(_effective_profile_dir(repo_root))
        if langs and "ruby" not in langs:
            out["reason"] = "unsupported-language"
        else:
            out["reason"] = "index-unavailable (no constant index; re-run /chameleon-refresh)"
        return out
    try:
        rel = file_path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return dict(empty)
    importers = []
    for const in constants_defined_in(index, rel):
        refs = referencing_files(index, const)
        if refs:
            importers.append(
                {
                    "name": sanitize_for_chameleon_context(const),
                    "count": len(refs),
                    "sites": [
                        {"path": sanitize_for_chameleon_context(r), "line": None} for r in refs
                    ],
                }
            )
    out = dict(empty)
    out["found"] = True
    out["module"] = rel
    out["importers"] = importers
    return out


def query_symbol_importers(repo: str, file_path: str) -> dict:
    """Who imports a module's bindings (TS/JS + Python; Ruby via the constant
    graph), and which imports it now breaks.

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
    from chameleon_mcp.lint_engine import detect_language
    from chameleon_mcp.phantom_imports import _current_export_names, _python_current_export_names
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
    if not p.is_absolute():
        # A repo-relative file_path is the natural input form: the calls index
        # keys, search_codebase, and describe_codebase all emit relative paths.
        # Resolve it against the repo arg's root before find_repo_root, which
        # otherwise walks up from the server CWD and silently fails open.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({**empty, "reason": "path-unresolved"})

    expected_repo_id = _compute_repo_id(repo_root)
    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + index lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible
    # "repo-arg-mismatch" negative indistinguishable from a real no-match. Proceed
    # with the file's own repo instead so the query answers for the workspace the
    # file actually lives in (origin-backed monorepos share one repo_id, so this
    # is a no-op there). Trust is still gated on expected_repo_id below.

    # Trust-gate: the index is an attacker-controllable committed artifact, so
    # its importer paths must not reach the model surface from an untrusted
    # profile (mirrors lint_file / the other read tools).
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    # Ruby has no named-export reverse index; its cross-file surface is the
    # constant graph. Serve the constant-reference blast radius instead.
    if detect_language(str(p)) == "ruby":
        return _envelope(_ruby_constant_importers(repo_root, p))

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
        _arg_root_ni, _ = _resolve_repo_arg(repo)
        out["module"] = _reroot_rel(target_key, repo_root, _arg_root_ni)
        return _envelope(out)

    if _module_file_missing(repo_root, target_key):
        # A DELETED module exports nothing and the set is CLOSED, so every indexed
        # importer that still names a binding is a genuine break. Separating this
        # from an unreadable module (below) mirrors get_crossfile_context; the old
        # code lumped deletion in with oversized/unsafe and returned found:false,
        # missing every broken importer of a removed file.
        current, open_set = frozenset(), False
    else:
        try:
            content = safe_read_text(repo_root, target_key, max_size_bytes=1_000_000)
        except Exception:
            # The module can't be READ (oversized, unsafe path -- not deletion,
            # handled above); without its current export set the existence check
            # can't run -- fail open.
            return _envelope(dict(empty))

        # The reverse index spans the TS and Python module graphs, so the live
        # export set must be read with the file's own language reader -- the TS
        # regex finds zero exports in a Python module, which would misreport every
        # Python importer as a broken reference. Pass the absolute path so the
        # Python reader can add an __init__.py package's sibling re-exports.
        if detect_language(str(p)) == "python":
            current, open_set = _python_current_export_names(content, p)
        else:
            current, open_set = _current_export_names(content)

    importers_out: list[dict] = []
    broken_out: list[dict] = []
    _qsi_lang = detect_language(str(p))
    _qsi_resolver = _crossfile_module_resolver(repo_root, _qsi_lang)

    def _qsi_site(imp) -> dict:
        # A through-barrel importer carries the re-export chain it was chased
        # across; surface it so a rename blast radius shows WHY a file that never
        # names this module still depends on it. Rerooted / sanitized in _clean.
        site = {"path": imp.path, "line": imp.line}
        if imp.via:
            site["via"] = list(imp.via)
        return site

    for name, importers in sorted(indexed.items()):
        if name in current:
            sites = [_qsi_site(imp) for imp in importers]
            importers_out.append({"name": name, "count": len(importers), "sites": sites})
        elif not open_set:
            # Re-verify each recorded importer still references `name` FROM this
            # module on disk before calling it broken: the index is a snapshot, so
            # a fully-migrated importer (reference dropped, or import repointed to a
            # new module) is NOT a broken site. Without this the documented
            # high-confidence `broken` channel emits phantom breaks during the
            # normal index-stale-vs-working-tree state of active editing.
            live = [
                imp
                for imp in importers
                if _live_importer_break(
                    repo_root, imp.path, name, imp.line, target_key, _qsi_lang, _qsi_resolver
                )
            ]
            if live:
                sites = [_qsi_site(imp) for imp in live]
                broken_out.append({"name": name, "count": len(live), "sites": sites})

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    # Emit paths in the repo-ARG root space (like search/describe) so they
    # round-trip in a nested-workspace monorepo (see _reroot_rel).
    _arg_root, _ = _resolve_repo_arg(repo)

    def _clean(rows: list[dict]) -> list[dict]:
        for row in rows:
            row["name"] = _sanitize(row["name"])
            for s in row["sites"]:
                if isinstance(s.get("path"), str):
                    s["path"] = _sanitize(_reroot_rel(s["path"], repo_root, _arg_root))
                if isinstance(s.get("via"), list):
                    s["via"] = [
                        _sanitize(_reroot_rel(v, repo_root, _arg_root))
                        for v in s["via"]
                        if isinstance(v, str)
                    ]
        return rows

    return _envelope(
        {
            "found": True,
            "module": _sanitize(_reroot_rel(target_key, repo_root, _arg_root)),
            "importers": _clean(importers_out),
            "broken": _clean(broken_out),
            "export_set_open": open_set,
        }
    )


def _reroot_rel(rel_path, answering_root: Path, arg_root) -> object:
    """Translate a path relative to the ANSWERING profile root into one relative to
    the repo-ARG root.

    In a nested-workspace monorepo the call-graph tools answer from the file's own
    workspace profile (find_repo_root(p)), emitting workspace-relative paths, while
    search_codebase / describe_codebase answer from the top-level profile arg and
    emit repo-root-relative paths. Chaining a workspace-relative caller path back
    into get_callers re-resolved against the top-level index (key absent) and
    returned a false total=0. Emitting call-graph paths in the SAME (repo-arg-root)
    space as search/describe makes them round-trip: an emitted path fed back in
    resolves to the right workspace again. Identity when the two roots match
    (non-monorepo) or arg_root is absent / not an ancestor, so single-profile repos
    are byte-for-byte unchanged."""
    if not isinstance(rel_path, str) or not rel_path:
        return rel_path
    if arg_root is None:
        return rel_path
    try:
        answering = Path(answering_root).resolve()
        arg = Path(arg_root).resolve()
        if answering == arg:
            return rel_path
        return (answering / rel_path).resolve().relative_to(arg).as_posix()
    except (ValueError, OSError):
        return rel_path


# These notes carry the whole honesty burden of an empty answer, so they must
# name the LARGEST blind spot first. The index resolves a call whose receiver is
# a module or namespace import (`from pkg import mod; mod.f()`), plus bare and
# `self` calls -- but a call through an INSTANCE (`service.charge()`, where
# `service` is a constructor result, a parameter, or an injected attribute)
# needs type inference to resolve, which a static snapshot does not do. In
# object-oriented code that is the dominant call form, so an empty answer there
# is routine rather than exceptional.
#
# The earlier wording blamed only "dynamic dispatch, reflection, and callers
# added since the last refresh". None of those applies to an ordinary
# statically-written `service.charge()` in a freshly refreshed profile, so it
# read as "rare edge cases may be missing" and invited the reader to trust a
# zero. Measured on a Flask column: a service method with four grep-verified
# call sites returned total=0. The plugin's own digest tells the model to check
# blast radius BEFORE a rename, which makes a trusted zero actively dangerous.
EMPTY_CALLERS_NOTE = (
    "No caller in the committed calls snapshot. Absence is NOT evidence of dead "
    "code, and on object-oriented code it is expected: a call made through an "
    "instance (obj.method(), self.dep.method()) needs type inference to resolve "
    "and is NOT indexed, so whole call classes are invisible here. Dynamic "
    "dispatch, reflection, and callers added since the last refresh are invisible "
    "too. Before renaming, deleting, or changing this signature, confirm with a "
    "grep; run /chameleon-refresh if the snapshot may be stale."
)

EMPTY_CALLEES_NOTE = (
    "No callee in the committed calls snapshot. Absence is NOT evidence that this "
    "function calls nothing, and on object-oriented code it is expected: a call "
    "made through an instance (obj.method(), self.dep.method()) needs type "
    "inference to resolve and is NOT indexed. Dynamic dispatch, reflection, and "
    "edges added since the last refresh are invisible too. Confirm with a grep "
    "before relying on this; run /chameleon-refresh if the snapshot may be stale."
)

DUMP_CAPPED_NOTE = (
    " Some files in this repo had more call sites than the per-file derivation "
    "cap records (see dump_capped_files); an edge inside those files may be "
    "missing from this answer even though the file was analyzed."
)


def _dump_capped_payload(index, repo_root, arg_root, sanitize) -> dict | None:
    """Compact ``{count, examples}`` for capped dump files, or None when none.

    Names at most three files so the caller can grep exactly where the graph
    may undercount; ``count`` carries the true total.
    """
    capped = getattr(index, "capped_files", None)
    if not capped:
        return None
    examples = [sanitize(_reroot_rel(rel, repo_root, arg_root)) for rel in sorted(capped)[:3]]
    return {"count": len(capped), "examples": examples}


def _calls_index_unavailable_reason(repo_root: Path) -> str:
    """Why load_calls_index returned None: never built vs present-but-stale.

    Both cases zero the caller graph, but the fix differs: a stale index (an
    older engine's schema, or one over the read ceiling) IS repaired by
    /chameleon-refresh, while a never-built one means the profile predates the
    feature. Distinguishing them lets the tool result carry the actionable
    signal instead of a flat "no-calls-index" the language-depth pass flagged as
    indistinguishable from "never built". Mirrors load_calls_index's own path
    resolution so the file-presence probe matches what the loader read.
    """
    try:
        from chameleon_mcp.calls_index import CALLS_INDEX_FILENAME
        from chameleon_mcp.worktree import resolve_profile_root

        # .resolve() before resolve_profile_root mirrors load_calls_index's own
        # path handling, so the file-presence probe matches exactly what the
        # loader read (no divergence on a symlinked or relative root).
        root = resolve_profile_root(Path(repo_root).resolve())
        if (root / ".chameleon" / CALLS_INDEX_FILENAME).is_file():
            return "calls-index-stale"
    except Exception:
        pass
    return "no-calls-index"


def get_callers(repo: str, file_path: str, function_name: str) -> dict:
    """Who calls a function, from the committed calls snapshot (deterministic grades only).

    Reads the prebuilt ``calls_index.json`` artifact and returns the recorded
    caller rows for ``function_name`` defined in the file at ``file_path``,
    GROUPED one row per (path, caller, grade, via) with every call line in
    ``lines`` (ascending; barrel-chained edges keep separate rows) --
    ``total`` still counts individual call sites.

    Grades are deterministic: ``same_file`` (bare call to a file-local name or
    ``this.``/``self.`` to a class member defined in the same file),
    ``import`` (a named-import or namespace-import call against the target's
    closed export set -- TypeScript named/namespace imports AND Python
    ``from m import f; f()`` / ``import a.b as x; x.f()`` calls),
    ``constant_receiver`` (Ruby ``Const.method`` with exactly one defining
    class). Name-only / dynamic / inheritance-based call paths are
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
    if not isinstance(function_name, str):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    if not p.is_absolute():
        # A repo-relative file_path is the natural input form: the calls index
        # keys, search_codebase, and describe_codebase all emit relative paths.
        # Resolve it against the repo arg's root before find_repo_root, which
        # otherwise walks up from the server CWD and silently fails open.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({**empty, "reason": "path-unresolved"})

    expected_repo_id = _compute_repo_id(repo_root)
    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + index lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible
    # "repo-arg-mismatch" negative indistinguishable from a real no-match. Proceed
    # with the file's own repo instead so the query answers for the workspace the
    # file actually lives in (origin-backed monorepos share one repo_id, so this
    # is a no-op there). Trust is still gated on expected_repo_id below.

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
        out["reason"] = _calls_index_unavailable_reason(repo_root)
        return _envelope(out)

    rel = module_key_for_path(p, repo_root)
    if rel is None:
        out = dict(empty)
        out["reason"] = "file-outside-repo"
        return _envelope(out)

    # Emit paths in the repo-ARG root space (like search/describe) so they
    # round-trip in a nested-workspace monorepo (see _reroot_rel).
    _arg_root, _ = _resolve_repo_arg(repo)

    def _rr(v):
        return _reroot_rel(v, repo_root, _arg_root)

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    _capped = _dump_capped_payload(index, repo_root, _arg_root, _sanitize)

    entry = index.callers_of(rel, function_name)
    if entry is None:
        # The (file, name) pair was not recorded -- a known-absent callee is a
        # real answer (no deterministic callers at derivation time), not an error.
        # Sanitize the echoed module/function for shape parity with the success
        # branch (both derive from the calls index / caller-supplied path).
        result = {
            "found": True,
            "module": _sanitize(_rr(rel)),
            "function": _sanitize(function_name),
            "callers": [],
            "total": 0,
            "truncated": False,
            "note": EMPTY_CALLERS_NOTE + (DUMP_CAPPED_NOTE if _capped else ""),
        }
        if _capped:
            result["dump_capped_files"] = _capped
        # Self-correcting negative: when the module HAS recorded callees under
        # other names, a near-miss (typo, camel/snake drift, renamed symbol)
        # is far likelier than a genuinely uncalled function -- name the
        # closest recorded names so the caller can retry without a detour
        # through search_codebase. Strictly additive: any failure here must
        # never break the known-absent answer itself.
        try:
            import difflib

            near = difflib.get_close_matches(function_name, index.names_for(rel), n=3, cutoff=0.6)
            if near:
                result["recorded_names_nearby"] = [_sanitize(n) for n in near]
                result["note"] = EMPTY_CALLERS_NOTE + (
                    " No entry exists for this exact name in this module; the closest"
                    " names the index DOES record are in recorded_names_nearby -- if"
                    " one of those is the symbol you meant, re-call with it."
                )
        except Exception:
            pass
        return _envelope(result)

    # One row per (path, caller, grade, via) with every call line in `lines`:
    # a caller that hits the function N times is one row, not N near-identical
    # rows repeating the same path/caller/grade. `total` still counts call
    # sites, so nothing is lost -- the payload just stops paying for the
    # repetition.
    grouped: dict = {}
    row_order: list = []
    for row in entry["callers"]:
        path = _sanitize(_rr(row["path"])) if isinstance(row.get("path"), str) else row.get("path")
        caller = (
            _sanitize(row["caller"]) if isinstance(row.get("caller"), str) else row.get("caller")
        )
        grade = row.get("grade")
        # A through-barrel edge carries the re-export chain it was chased across:
        # this caller reaches the function via these barrel files, not by naming
        # the module directly. Surface it (rerooted + sanitized) so the path is
        # visible rather than the edge looking like a direct import that isn't.
        raw_via = row.get("via")
        via = (
            tuple(_sanitize(_rr(v)) for v in raw_via if isinstance(v, str))
            if isinstance(raw_via, list) and raw_via
            else ()
        )
        key = (path, caller, grade, via)
        clean_row = grouped.get(key)
        if clean_row is None:
            clean_row = {"path": path, "caller": caller, "grade": grade, "lines": []}
            if via:
                clean_row["via"] = list(via)
            grouped[key] = clean_row
            row_order.append(key)
        line = row.get("line")
        if line is not None:
            clean_row["lines"].append(line)
    clean_callers = [grouped[k] for k in row_order]
    for clean_row in clean_callers:
        clean_row["lines"].sort()

    result = {
        "found": True,
        "module": _sanitize(_rr(rel)),
        "function": _sanitize(function_name),
        "callers": clean_callers,
        "total": entry["total"],
        "truncated": entry["truncated"],
    }
    # An entry that exists but yields no surviving rows is a real "no known
    # caller" answer; carry the same absence-is-not-dead-code caveat the
    # known-absent branch and get_blast_radius already return.
    if not clean_callers:
        result["note"] = EMPTY_CALLERS_NOTE + (DUMP_CAPPED_NOTE if _capped else "")
    if _capped:
        result["dump_capped_files"] = _capped
    return _envelope(result)


def get_blast_radius(repo: str, file_path: str, function_name: str, depth: int = 0) -> dict:
    """Transitive callers of a function (the change blast radius), from the calls snapshot.

    Walks the prebuilt ``calls_index.json`` upward from ``function_name`` defined
    in the file at ``file_path`` and returns the bounded caller chains that reach
    it: "if I change this, what transitively calls it". Each chain starts at the
    function's FIRST caller and walks upward (caller -> caller's caller ...);
    the queried function itself is carried once in ``module``/``function``,
    never repeated per chain. One
    ``{path, name, line}`` hop per step. ``depth`` is the number of hops; it
    defaults to the judge's transitive depth and is clamped to
    ``[1, BLAST_RADIUS_MAX_DEPTH]``. The walk shares the judge's fanout /
    total-node caps, so total work is hard-bounded regardless of depth.

    This is the same conservative reach the turn-end correctness judge already
    walks, surfaced as a tool so pr-review and the human can ask it directly
    instead of being limited to one-hop ``get_callers``. Grades are deterministic
    (same_file, import, constant_receiver, typed_property, module_attribute); name-only / dynamic / inheritance
    call paths are absent by design.

    Interpretation note (returned in ``note``): absence of a caller is NOT
    evidence of dead code. Dynamic dispatch, reflection, and callers added after
    the last bootstrap are invisible, and a stale intermediate edge can shorten a
    chain. The reach is a grounding fact for review, not a reachability oracle.

    Fails open with ``found: False`` on any ambiguity: unresolvable / untrusted
    repo, missing artifact, path outside the repo. Never fabricates a caller.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.blast_radius import BLAST_RADIUS_NOTE, compute_blast_radius
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.symbol_index import module_key_for_path

    empty = {
        "found": False,
        "module": None,
        "function": None,
        "depth": 0,
        "chains": [],
        "reached": 0,
        "truncated": False,
    }

    if not _validate_file_path_arg(file_path):
        return _envelope(dict(empty))
    if not isinstance(function_name, str):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    if not p.is_absolute():
        # A repo-relative file_path is the natural input form: the calls index
        # keys, search_codebase, and describe_codebase all emit relative paths.
        # Resolve it against the repo arg's root before find_repo_root, which
        # otherwise walks up from the server CWD and silently fails open.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({**empty, "reason": "path-unresolved"})

    expected_repo_id = _compute_repo_id(repo_root)
    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + index lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible
    # "repo-arg-mismatch" negative indistinguishable from a real no-match. Proceed
    # with the file's own repo instead so the query answers for the workspace the
    # file actually lives in (origin-backed monorepos share one repo_id, so this
    # is a no-op there). Trust is still gated on expected_repo_id below.

    # Trust-gate: the calls index is a committed artifact whose caller paths must
    # not reach the model surface from an untrusted profile.
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    index = load_calls_index(repo_root)
    if index is None:
        out = dict(empty)
        out["reason"] = _calls_index_unavailable_reason(repo_root)
        return _envelope(out)

    rel = module_key_for_path(p, repo_root)
    if rel is None:
        out = dict(empty)
        out["reason"] = "file-outside-repo"
        return _envelope(out)

    # depth <= 0 (or a non-int) means "use the judge's default transitive depth";
    # any explicit request is clamped into [1, BLAST_RADIUS_MAX_DEPTH].
    if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
        resolved_depth = threshold_int("JUDGE_TRANSITIVE_DEPTH")
    else:
        resolved_depth = depth
    resolved_depth = max(1, min(resolved_depth, threshold_int("BLAST_RADIUS_MAX_DEPTH")))

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    # Emit paths in the repo-ARG root space (like search/describe) so they
    # round-trip in a nested-workspace monorepo (see _reroot_rel).
    _arg_root, _ = _resolve_repo_arg(repo)

    radius = compute_blast_radius(index, rel, function_name, depth=resolved_depth)
    clean_chains = []
    for chain in radius["chains"]:
        # Every chain starts at the SAME root hop -- the queried function
        # itself (no line, since it is the callee, not a call site). Repeating
        # it per chain is pure payload; the response's module/function fields
        # already carry it once. Emit each chain from its first caller upward.
        hops = chain[1:] if chain and chain[0].get("name") == function_name else chain
        clean_chains.append(
            [
                {
                    "path": _sanitize(_reroot_rel(hop["path"], repo_root, _arg_root))
                    if isinstance(hop.get("path"), str)
                    else hop.get("path"),
                    "name": _sanitize(hop["name"])
                    if isinstance(hop.get("name"), str)
                    else hop.get("name"),
                    "line": hop.get("line"),
                }
                for hop in hops
            ]
        )

    result = {
        "found": True,
        "module": _sanitize(_reroot_rel(rel, repo_root, _arg_root)),
        "function": _sanitize(function_name),
        "depth": resolved_depth,
        "chains": clean_chains,
        "reached": radius["reached"],
        "truncated": radius["truncated"],
        "note": BLAST_RADIUS_NOTE,
    }
    _capped = _dump_capped_payload(index, repo_root, _arg_root, _sanitize)
    if _capped:
        result["note"] = BLAST_RADIUS_NOTE + DUMP_CAPPED_NOTE
        result["dump_capped_files"] = _capped
    return _envelope(result)


def get_prose_rule_candidates(repo: str) -> dict:
    """Doc-stated import-preference rules, corroborated against the repo's own code.

    Reads a bounded allowlist of convention-bearing docs (CONTRIBUTING / STYLE /
    AGENTS.md / docs) for high-precision ``use X not Y`` / ``prefer X over Y``
    rules -- the conventions AST analysis cannot infer -- and tags each with how
    the repo's own imports back it:
      - ``corroborated`` (``teachable: true``): the preferred form is imported and
        the discouraged form is absent, so the code already follows the rule. Ready
        to capture via ``teach_competing_import``.
      - ``contested``: the discouraged form is still imported. Doc and code
        disagree; a human reconciles (fix the doc or the code), never auto-taught.
      - ``unsupported``: neither form imported; cannot verify from code.

    PROPOSE-ONLY: this never writes the profile. It returns candidates with their
    ``source`` provenance (``doc-path:line``) so an approval step decides. Fully
    offline, no repo-code execution, bounded. Fails open with ``found: False`` on
    an unresolvable / untrusted repo.
    """
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.prose_rules import mine_prose_rule_candidates

    empty = {"found": False, "candidates": []}

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None or resolved_path is None:
        return _envelope(dict(empty))
    repo_root = Path(resolved_path)

    # Trust-gate: the candidates echo repo doc/source text to the model surface,
    # so an untrusted profile must not reach it (the read-tool trust idiom).
    gate = _trust_state_for(repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    clean = []
    for c in mine_prose_rule_candidates(repo_root):
        clean.append(
            {
                "preferred": _sanitize(c["preferred"]),
                "over": _sanitize(c["over"]),
                "source": _sanitize(c["source"]),
                "status": c["status"],
                "teachable": c["teachable"],
                "preferred_files": c["preferred_files"],
                "over_files": c["over_files"],
            }
        )

    return _envelope(
        {
            "found": True,
            "candidates": clean,
            "note": (
                "Propose-only. Corroborated rules are ready to teach via "
                "teach_competing_import; contested / unsupported are advisory. "
                "chameleon never writes idioms.md without your approval."
            ),
        }
    )


def _resolve_response_format(value) -> tuple[str, str | None]:
    """Normalize a response_format argument: ("concise"|"detailed", note|None).

    Fail-open: an unknown value keeps the tool's full (detailed) behavior and
    says so in a note, instead of erroring a read that would otherwise work.
    """
    if isinstance(value, str) and value.strip().lower() in ("concise", "detailed"):
        return value.strip().lower(), None
    if value in (None, ""):
        return "detailed", None
    # The echoed value is model-supplied input reflected into the response:
    # bound it (a megastring must not inflate the payload it was meant to
    # shrink) and sanitize it (repr does not neutralize tag-boundary tokens).
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    shown = _sanitize(str(value)[:40])
    return (
        "detailed",
        f"unknown response_format {shown!r} ignored; valid values: concise, detailed",
    )


def search_codebase(
    repo: str,
    query: str,
    limit: int = 10,
    offset: int = 0,
    response_format: str = "detailed",
) -> dict:
    """Find symbols by name or file, ranked, from the committed profile (comprehension).

    The "where is X / find Y" query chameleon's conformance profile can also
    answer: it walks the committed symbol index (every recorded callable across
    all profiled languages) and returns the matches for ``query``, ranked exact
    name > prefix > substring > all-tokens > file-path, with a more-called symbol
    breaking ties (it is more central). Each result carries
    ``{name, file, line, signature, callers}``; ``response_format="concise"``
    keeps ``{name, file, line}`` (and ``kind``) only. ``limit`` is clamped to
    ``COMPREHEND_SEARCH_MAX_RESULTS``. ``offset`` pages the SAME deterministic
    ranking (clamped to ``COMPREHEND_SEARCH_MAX_OFFSET``); when more matches
    remain past the page, the response carries ``next_offset`` and a steering
    note.

    Read-only over the committed index, offline, no repo-code execution. Fails
    open with ``found: False`` on an unresolvable / untrusted repo or empty query.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.comprehension import search_symbols
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    empty = {"found": False, "query": "", "results": []}

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None or resolved_path is None:
        return _envelope(dict(empty))
    repo_root = Path(resolved_path)

    gate = _trust_state_for(repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    # A blank/empty query is a no-op search; return found:False per the contract
    # (so a caller can branch on `found` to detect it) rather than found:True with
    # an empty result list.
    if not isinstance(query, str) or not query.strip():
        return _envelope(dict(empty))

    cap = threshold_int("COMPREHEND_SEARCH_MAX_RESULTS")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        n = 10
    else:
        n = limit
    n = max(1, min(n, cap))
    # Pagination over the same deterministic ranking: offset skips ranked rows.
    # Clamped so a huge offset cannot force an unbounded index walk.
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0
    offset = min(offset, threshold_int("COMPREHEND_SEARCH_MAX_OFFSET"))
    fmt, fmt_note = _resolve_response_format(response_format)

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _s

    def _ss(v):
        return _s(v) if isinstance(v, str) else v

    # Fetch one past the effective page end: a full page alone cannot
    # distinguish "exactly limit matches" from "more were silently dropped",
    # and the cap clamp (limit > COMPREHEND_SEARCH_MAX_RESULTS) is invisible
    # to the caller.
    fetched = search_symbols(repo_root, query, limit=offset + n + 1)
    more_matches = len(fetched) > offset + n
    results = []
    for r in fetched[offset : offset + n]:
        row = {"name": _ss(r.get("name")), "file": _ss(r.get("file")), "line": r.get("line")}
        if fmt == "detailed":
            row["signature"] = _ss(r.get("signature"))
            row["callers"] = r.get("callers")
        if isinstance(r.get("kind"), str):
            row["kind"] = _ss(r.get("kind"))
        results.append(row)
    out = {"found": True, "query": _ss(query), "results": results}
    if offset:
        out["offset"] = offset
    if fmt_note:
        out["note"] = fmt_note
    if more_matches:
        out["truncated"] = True
        out["next_offset"] = offset + n
        out["truncated_note"] = (
            f"more symbols matched; re-call with offset={offset + n} for the next page, "
            "or narrow the query"
        )
    from chameleon_mcp.worktree import resolve_profile_root

    _pr = resolve_profile_root(repo_root)
    _reasons: list[str] = []
    if not results:
        # search_symbols returns [] for BOTH "no symbol matched" and "symbol index
        # unreadable", so an empty result on a corrupt symbol_signatures.json would
        # read as an authoritative "not found". Distinguish them: flag degraded
        # when the index artifact is present-but-unloadable.
        from chameleon_mcp.symbol_signatures import SCHEMA_VERSION as _SS_SV
        from chameleon_mcp.symbol_signatures import load_symbol_signatures

        _ss_path = _pr / ".chameleon" / "symbol_signatures.json"
        if load_symbol_signatures(_pr) is None:
            # Distinguish three states: never built (missing -- previously
            # silent, the identical situation the present-but-corrupt case
            # already flagged), genuine corruption, and a schema-stale index
            # (repaired by /chameleon-refresh, same as calls-index-stale
            # below). "Stale" requires SOME evidence this is a real
            # prior-schema artifact -- the expected "files" shape, or a
            # present (if out-of-range) int schema_version -- not merely a
            # missing/None schema_version on an otherwise-garbage payload.
            if not _ss_path.is_file():
                _ss_reason = "missing"
            else:
                _ss_reason = "corrupt"
                try:
                    _ss_obj = json.loads(_ss_path.read_text(encoding="utf-8"))
                    if isinstance(_ss_obj, dict):
                        _ss_sv = _ss_obj.get("schema_version")
                        _ss_has_shape = isinstance(_ss_obj.get("files"), dict)
                        _ss_has_versioned = (
                            isinstance(_ss_sv, int)
                            and not isinstance(_ss_sv, bool)
                            and _ss_sv != _SS_SV
                        )
                        if _ss_has_shape or _ss_has_versioned:
                            _ss_reason = "symbol-index-stale"
                except (OSError, ValueError):
                    pass
            _reasons.append(f"symbol index unavailable ({_ss_reason})")
    # A present-but-corrupt calls_index zeroes every `callers` count and re-ranks
    # NON-empty results, so check it regardless of whether results is empty --
    # otherwise a successful-looking search silently reports callers=0 everywhere.
    # Likewise a MISSING calls_index must not stay silent just because it never
    # existed -- the identical zeroed-callers situation the present-but-corrupt
    # case already flags.
    from chameleon_mcp.calls_index import load_calls_index

    if load_calls_index(_pr) is None:
        # Route through the same helper get_callers / get_blast_radius /
        # query_symbol_importers already use, so a schema-stale index reads
        # as "calls-index-stale" and a never-built one as "no-calls-index"
        # everywhere -- not a hardcoded "(corrupt)" here and a different
        # label on every sibling comprehension tool.
        _reasons.append(
            f"call index unavailable ({_calls_index_unavailable_reason(_pr)}); "
            "caller counts may be zero"
        )
    if _reasons:
        out["degraded"] = True
        out["reason"] = "; ".join(_reasons) + "; results may be incomplete"
    elif not results:
        if offset:
            # An empty PAGE past the end of a ranking that may well have
            # matched: "no symbol matched" would be false here and could push
            # the caller to a needless refresh or a wrong "does not exist".
            empty_note = (
                f"no matches at offset {offset}: the ranking ended earlier -- "
                "page back (offset=0) or narrow the query"
            )
        else:
            # A clean (non-degraded) empty result: the query matched no callable
            # and no class/module definition. The index covers both, but only
            # what the last profile build captured, so point at the actionable
            # next steps rather than let an empty result read as "this symbol
            # does not exist".
            empty_note = (
                "No symbol matched. The index covers callables (functions, methods) "
                "and class/module definitions from the last profile build -- try a "
                "different name or a file-path fragment, describe_codebase for the "
                "archetype overview, or /chameleon-refresh if the index may be stale."
            )
        # Append, never clobber: an unknown-response_format warning set above
        # must survive alongside the empty-result guidance.
        out["note"] = f"{out['note']} | {empty_note}" if out.get("note") else empty_note
    return _envelope(out)


def describe_codebase(repo: str, response_format: str = "detailed") -> dict:
    """A structural overview of the repo from its committed profile (comprehension).

    The "what is this codebase" answer, read off chameleon's own profile: the
    primary ``language`` and ``framework``, the ``archetypes`` (the kinds of files
    the repo contains, each with its size, summary, and canonical witness), the
    file/symbol totals, and the ``god_symbols`` (the most-called production
    functions, test files excluded). All from committed artifacts, offline.
    ``response_format="concise"`` keeps each archetype's ``{name, size,
    witness}`` (dropping the summary/paths prose) and the top 5 god symbols --
    the cheap orientation read; default ``"detailed"`` is the full overview.

    Fails open with ``found: False`` on an unresolvable / untrusted repo, and to
    an empty-shaped overview when no profile is present. A profile whose
    ``schema_version`` is unsupported by this engine resolves but cannot be
    trusted-and-derived: it returns ``found: True`` with ``degraded: True`` and
    the profile-derived fields (``language``, ``framework``, ``archetypes``)
    nulled/empty — an honest "a profile exists but is unusable under this
    engine" signal, distinct from ``found: False`` ("no profile at all"). Check
    ``degraded`` before reading the derived fields; ``detect_repo`` reports the
    same state as ``profile_unsupported_schema_version``.
    """
    from chameleon_mcp.comprehension import describe_codebase as _describe
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

    empty = {"found": False}

    resolved_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None or resolved_path is None:
        return _envelope(dict(empty))
    repo_root = Path(resolved_path)

    gate = _trust_state_for(repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _s

    def _ss(v):
        return _s(v) if isinstance(v, str) else v

    fmt, fmt_note = _resolve_response_format(response_format)
    d = _describe(repo_root)
    if fmt == "concise":
        archetypes = [
            {"name": _ss(a.get("name")), "size": a.get("size"), "witness": _ss(a.get("witness"))}
            for a in d.get("archetypes", [])
        ]
    else:
        archetypes = [
            {
                "name": _ss(a.get("name")),
                "summary": _ss(a.get("summary")),
                "size": a.get("size"),
                "paths": _ss(a.get("paths")),
                "witness": _ss(a.get("witness")),
            }
            for a in d.get("archetypes", [])
        ]
    god = []
    god_rows = d.get("god_symbols", [])
    if fmt == "concise":
        god_rows = god_rows[:5]
    for g in god_rows:
        row = {"name": _ss(g.get("name")), "file": _ss(g.get("file")), "callers": g.get("callers")}
        if g.get("capped"):
            # The per-callee row cap bounded the stored caller list, so this
            # count is a floor, not an exact production-caller total.
            row["capped"] = True
        god.append(row)
    out = {
        "found": True,
        "language": _ss(d.get("language")),
        "framework": _ss(d.get("framework")),
        "file_count": d.get("file_count"),
        "symbol_count": d.get("symbol_count"),
        "archetypes": archetypes,
        "god_symbols": god,
    }
    if fmt_note:
        out["note"] = fmt_note
    # The profile bundle failed cross-artifact validation but the independent
    # symbol index was still read; flag it so a consumer knows the archetype /
    # language fields may be partial and can suggest /chameleon-refresh.
    if d.get("degraded"):
        out["degraded"] = True
    # file_count sits at the signatures-artifact cap, so it is a floor on a repo
    # above the cap, not the true total; forward the flag so the count is not
    # read as ground truth.
    if d.get("truncated"):
        out["truncated"] = True
    # The archetype list is capped to the largest DESCRIBE_MAX_ARCHETYPES rows;
    # forward the omission count so the overview never reads as complete when
    # tail clusters were dropped.
    if d.get("archetypes_omitted"):
        out["archetypes_omitted"] = d["archetypes_omitted"]
    return _envelope(out)


def get_callees(repo: str, file_path: str, function_name: str) -> dict:
    """What a function calls (forward edges), from the committed calls snapshot.

    The forward counterpart to ``get_callers`` / ``get_blast_radius``: it inverts
    the reverse ``calls_index`` to answer "what does this function call". Each
    result is ``{callee, file, grade}`` with the same three deterministic grades
    (same_file, import, constant_receiver, typed_property, module_attribute). Absence of a callee is NOT proof the
    function calls nothing: dynamic dispatch and unsupported call paths are
    invisible. Fails open with ``found: False`` on any ambiguity.
    """
    from chameleon_mcp.calls_index import load_calls_index
    from chameleon_mcp.comprehension import callees_of
    from chameleon_mcp.profile.loader import find_repo_root
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for
    from chameleon_mcp.symbol_index import module_key_for_path

    empty = {"found": False, "module": None, "function": None, "callees": []}

    if not _validate_file_path_arg(file_path):
        return _envelope(dict(empty))
    # Mirror get_callers / get_blast_radius: a non-string function_name is
    # out-of-contract input; return found=False rather than echoing the raw
    # value back into the `function` field with a misleading found=True.
    if not isinstance(function_name, str):
        return _envelope(dict(empty))

    p = Path(file_path).expanduser()
    if not p.is_absolute():
        # A repo-relative file_path is the natural input form: the calls index
        # keys, search_codebase, and describe_codebase all emit relative paths.
        # Resolve it against the repo arg's root before find_repo_root, which
        # otherwise walks up from the server CWD and silently fails open.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({**empty, "reason": "path-unresolved"})

    expected_repo_id = _compute_repo_id(repo_root)
    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + index lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible
    # "repo-arg-mismatch" negative indistinguishable from a real no-match. Proceed
    # with the file's own repo instead so the query answers for the workspace the
    # file actually lives in (origin-backed monorepos share one repo_id, so this
    # is a no-op there). Trust is still gated on expected_repo_id below.

    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        out = dict(empty)
        out["status"] = "untrusted"
        return _envelope(out)

    _callees_index = load_calls_index(repo_root)
    if _callees_index is None:
        out = dict(empty)
        out["reason"] = _calls_index_unavailable_reason(repo_root)
        return _envelope(out)

    rel = module_key_for_path(p, repo_root)
    if rel is None:
        out = dict(empty)
        out["reason"] = "file-outside-repo"
        return _envelope(out)

    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _s

    def _ss(v):
        return _s(v) if isinstance(v, str) else v

    # Emit paths in the repo-ARG root space (like search/describe) so they
    # round-trip in a nested-workspace monorepo (see _reroot_rel).
    _arg_root, _ = _resolve_repo_arg(repo)

    clean = [
        {
            "callee": _ss(r.get("callee")),
            "file": _ss(_reroot_rel(r.get("file"), repo_root, _arg_root)),
            "grade": r.get("grade"),
        }
        for r in callees_of(repo_root, rel, function_name)
    ]
    result = {
        "found": True,
        "module": _ss(_reroot_rel(rel, repo_root, _arg_root)),
        "function": _ss(function_name),
        "callees": clean,
    }
    # No recorded callee is a real answer, not a failure; carry the same
    # absence-is-not-dead-code caveat get_callers / get_blast_radius return so a
    # consumer echoing the payload never reads empty as "calls nothing".
    if not clean:
        result["note"] = EMPTY_CALLEES_NOTE
    # When THIS file's own dump-time call-site record hit the cap, its later
    # outbound calls never reached the index -- say so instead of letting the
    # answer read as the file's complete forward edge set.
    if rel in getattr(_callees_index, "capped_files", frozenset()):
        result["note"] = (result.get("note", "") + DUMP_CAPPED_NOTE).strip()
        result["dump_capped_files"] = _dump_capped_payload(
            _callees_index, repo_root, _arg_root, _ss
        )
    return _envelope(result)


def _crossfile_module_resolver(repo_root: Path, language: str):
    """The specifier resolver the reverse index built with, for ``language``.

    Built FRESH per tool call (no process-lifetime cache): the resolver captures
    a tsconfig/src-root snapshot, and a long-lived MCP/daemon session that ran
    /chameleon-refresh after editing path aliases must not be served a stale alias
    map while the reverse index it filters was reloaded. Callers build it once per
    invocation and reuse it across that call's importer sites.
    """
    from chameleon_mcp.symbol_index import make_module_resolver

    return make_module_resolver(Path(repo_root).resolve(), language)


def _module_file_missing(repo_root: Path, rel: str) -> bool:
    """True iff ``rel`` names a path inside ``repo_root`` that no longer exists.

    A deleted module (gone from disk) exports nothing, so every importer still
    referencing it is a genuine existence break -- distinct from an unreadable
    module (oversized / unsafe path), whose export set is unknown. Does NO read,
    only an existence check within the repo, so it is safe for a path that failed
    the safe_open validation for a reason OTHER than deletion: an escaped or
    traversal path resolves outside the root and reads as not-missing (skip),
    never as deleted. Fails closed to False (treat as unreadable, skip) on error.
    """
    try:
        candidate = (repo_root / rel).resolve()
        root = repo_root.resolve()
        if candidate != root and root not in candidate.parents:
            return False
        return not candidate.exists()
    except (OSError, ValueError):
        # ValueError covers a null-byte in ``rel`` (a poisoned artifact path);
        # both fail closed to "not missing" so a bad path is skipped, never a
        # crash that aborts the whole cross-file scan.
        return False


def _live_importer_break(
    repo_root: Path,
    importer_rel: str,
    name: str,
    line: int | None,
    target_key: str,
    language: str,
    resolver,
) -> bool:
    """One read of the importer decides whether its call site to ``name`` from the
    edited module is genuinely broken: it still references ``name`` (word-boundary)
    AND has not repointed its import of ``name`` to a different module.

    The reverse index is a bootstrap snapshot. An importer may have dropped the
    reference (a rename reached it) -- not broken -- or kept the bareword while
    repointing the import to a new module (move-and-reimport) -- also not broken,
    the index just still attributes the import here. Resolving the importer's
    CURRENT import sources with ``resolver`` (the per-call resolver the reverse
    index built with) distinguishes a real dangling import from a repoint. Reads
    the file ONCE for both checks. Returns False on any read error (an unreadable
    importer cannot prop up a finding); the repoint leg fails open to "not
    repointed" so a parse miss never hides a real break.
    """
    from chameleon_mcp.safe_open import safe_read_text

    try:
        content = safe_read_text(repo_root, importer_rel, max_size_bytes=1_000_000)
    except Exception:
        return False
    # "Still references ``name`` as CODE" -- string literals (the import specifier
    # path) AND comments (a stale mention left after a rename) are blanked by
    # _reference_present so a removed export whose name is a substring of its own
    # module path (`api` in `'@/lib/api-client'`) or lingers in a comment does not
    # prop up a phantom break on an importer that fully renamed its reference.
    from chameleon_mcp.hook_helper import _reference_present

    if not _reference_present(content, name, line, language):
        return False
    try:
        from chameleon_mcp.hook_helper import _imported_source_keys

        keys = _imported_source_keys(
            content, name, (repo_root / importer_rel).parent, language, resolver
        )
        if keys and target_key not in keys:
            return False  # repointed to a different module -> not broken
    except Exception:
        pass
    return True


def _ruby_constant_existence_breaks(repo_root: Path) -> dict:
    """get_crossfile_context for Ruby: constants the index records as defined in a
    file that the file NO LONGER defines on disk, while referencers still name
    them -- a removed/renamed referenced class, the Ruby analogue of a removed
    export.

    high_confidence requires an UNAMBIGUOUS constant: exactly one defining file in
    the index AND a bare top-level name (no ``::`` namespace, which a call site
    cannot disambiguate) AND at least one referencer that still names it on disk.
    Namespaced or multiply-defined constants are skipped -- accepted
    undercoverage, matching the constant-receiver grade. Returns the same finding
    shape as the TS path; every finding is high_confidence.
    """
    import re as _re

    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.constant_index import load_constant_index
    from chameleon_mcp.safe_open import safe_read_text
    from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _sanitize

    index = load_constant_index(repo_root)
    if index is None:
        return {"found": False, "findings": [], "_index_missing": True}
    max_findings = threshold_int("CROSSFILE_MAX_FINDINGS")
    max_sites = threshold_int("CROSSFILE_MAX_SITES_PER_FINDING")
    findings: list[dict] = []
    truncated = False
    for name in sorted(index.get("constants") or {}):
        if len(findings) >= max_findings:
            truncated = True
            break
        entry = index["constants"][name]
        defined_in = entry.get("defined_in") or []
        referenced_by = entry.get("referenced_by") or []
        # Unambiguous + referenced: exactly one defining file, bare top-level name.
        if len(defined_in) != 1 or "::" in name or not referenced_by:
            continue
        def_file = defined_in[0]
        deleted_def = False
        try:
            content = safe_read_text(repo_root, def_file, max_size_bytes=1_000_000)
        except Exception:
            # A DELETED defining file means the constant is definitively gone --
            # the strongest existence break, exactly what a PR that removes the
            # file must surface. An unreadable file (oversized / unsafe path)
            # leaves the definition unknown, so only the deletion proceeds.
            if _module_file_missing(repo_root, def_file):
                deleted_def = True
                content = ""
            else:
                continue
        # Still defined on disk? A top-level `class Foo` / `module Foo`. A deleted
        # file defines nothing, so skip this check and fall through to the
        # still-referencing scan below.
        if not deleted_def and _re.search(
            r"(?m)^(?:class|module)\s+" + _re.escape(name) + r"\b", content
        ):
            continue
        # Removed/renamed -- keep only referencers that still name the constant.
        # Deliberately a string-inclusive bareword scan (unlike the TS import
        # path, string literals are NOT blanked here): a Ruby constant has no
        # import binding to anchor a code-only check, and blanking `"..."` would
        # also blank interpolations `"#{Foo.bar}"` -- real code whose loss would
        # drop a genuine referencer (a false negative, worse than the rare
        # after-rename string-mention over-inclusion). Keep-biased by design.
        live = []
        for ref in referenced_by:
            try:
                rc = safe_read_text(repo_root, ref, max_size_bytes=1_000_000)
            except Exception:
                continue
            if _re.search(r"(?<![:.\w])" + _re.escape(name) + r"\b", rc):
                live.append(ref)
        if not live:
            continue
        live.sort()
        findings.append(
            {
                "symbol": _sanitize(name),
                "module": _sanitize(def_file),
                "count": len(live),
                "high_confidence": True,
                "sites": [{"path": _sanitize(r), "line": None} for r in live[:max_sites]],
            }
        )
    return {
        "found": True,
        "findings": findings,
        "low_confidence_dropped": 0,
        "_truncated": truncated,
    }


def get_crossfile_context(repo: str) -> dict:
    """Cross-file existence breaks across a repo (TS/JS + Python via the
    reverse index; Ruby via the constant graph), for PR review.

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
    missing index). TypeScript and Python read the named-export reverse index
    (each module read with its own export reader); Ruby has no static
    import-of-named-symbol, so it falls back to the constant graph
    (``_ruby_constant_existence_breaks``) and returns the same shape for a
    referenced class removed/renamed on disk.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.lint_engine import detect_language
    from chameleon_mcp.phantom_imports import _current_export_names, _python_current_export_names
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
        # No named-export reverse index -> try Ruby (the constant graph): a
        # referenced class removed/renamed on disk is the Ruby existence break.
        ruby = _ruby_constant_existence_breaks(repo_root)
        if not ruby.get("_index_missing"):
            return _envelope(
                {
                    "found": True,
                    "findings": ruby["findings"],
                    "low_confidence_dropped": 0,
                },
                truncated=ruby.get("_truncated", False),
            )
        out = dict(empty)
        # Every supported language has SOME cross-file surface for this scan
        # (reverse index for TS/Python, constant index for Ruby), so reaching
        # here with a known language means the backing artifact is damaged or
        # missing, never by-design absence — include Ruby in the surface set
        # so its reason reads as repairable damage too.
        out["reason"] = _crossfile_unavailable_reason(
            repo_root, surfaces=_CROSSFILE_SURFACE_LANGUAGES
        )
        # The existence-break scan could not run (no reverse index and no Ruby
        # constant index -- corrupt/missing artifact, or an unsupported layout).
        # Mark it degraded, mirroring get_contract_breaks, so a reader (pr-review
        # Step 2.9c) does not read the empty findings as a verified "no breaks".
        out["status"] = "degraded"
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
    # Per-call resolver cache (NOT process-lifetime): one resolver per language,
    # rebuilt each invocation so a /chameleon-refresh of the alias config is never
    # served a stale snapshot, while still amortizing the build across this call's
    # importer sites.
    _xf_resolvers: dict = {}

    def _xf_resolver_for(lang: str):
        r = _xf_resolvers.get(lang)
        if r is None:
            r = _crossfile_module_resolver(repo_root, lang)
            _xf_resolvers[lang] = r
        return r

    for target_key in target_keys[:max_modules]:
        if len(high) >= max_findings:
            truncated = True
            break
        deleted_module = False
        try:
            content = safe_read_text(repo_root, target_key, max_size_bytes=1_000_000)
        except Exception:
            # Distinguish a DELETED module from a merely-unreadable one. A deleted
            # module exports NOTHING, so every importer still referencing it is a
            # genuine break -- the strongest existence break there is, and exactly
            # what a PR that removes a file must surface. An unreadable module
            # (oversized / unsafe path) has an UNKNOWN export set, so skipping is
            # still correct there -- guessing a break would be wrong.
            if _module_file_missing(repo_root, target_key):
                deleted_module = True
                content = ""
            else:
                continue
        # The index spans the TS and Python module graphs; read each module's
        # live export set with its own language reader so a Python module's real
        # exports are not all reported broken by the TS regex. A deleted module
        # has a CLOSED empty export set: no star re-export can resurrect a name,
        # so broken_importers(target, {}) returns every still-referencing importer
        # and the per-site _live_importer_break re-check keeps only the ones that
        # still name it and still resolve here (a rename that reached the importer
        # drops out), so a stale index never fabricates a break.
        if deleted_module:
            current, open_set = frozenset(), False
        elif detect_language(target_key) == "python":
            current, open_set = _python_current_export_names(content, repo_root / target_key)
        else:
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
            _xf_lang = detect_language(target_key)
            live_sites = [
                imp
                for imp in importers
                if _live_importer_break(
                    repo_root,
                    imp.path,
                    name,
                    imp.line,
                    target_key,
                    _xf_lang,
                    _xf_resolver_for(_xf_lang),
                )
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

    # Gate off only when the CLI genuinely cannot spawn (absent/too old). A
    # --bare auth failure is NOT a blocker: _spawn_reviewer falls back to a plain
    # `claude -p` (the same fallback the turn-end judge takes every turn), so
    # gating on it here left round 3 dead on every current CLI while the judge
    # kept running. The spawn's own plain fallback handles bare-auth failure.
    _absent = _refuter.refuter_cli_absent()
    if _absent is not None:
        return _envelope({"refuter": "unavailable", "verdicts": _all_unverified(_absent)})

    # Validate the base like the judge does (judge.py `_spawn_judge`): a garbage /
    # typo'd CHAMELEON_REFUTER_MODEL must fall back to a valid tier, never reach
    # `claude -p --model` (which would exit nonzero and fail-open every verdict to
    # unverified). Mirrors the never-garbage contract the model ladder already
    # honors for the HIGH escalation.
    from chameleon_mcp.judge import _valid_model as _vm

    model = os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
    if not _vm(model):
        model = "sonnet"
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
    if not isinstance(file_path, str):
        # "[] on any parse error" contract: a non-string path (from a malformed
        # caller) would crash the language/path detection below.
        return []
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


def get_duplication_candidates(
    repo: str, file_path: str, response_format: str = "detailed"
) -> dict:
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
    Body excerpts draw from one global char budget
    (``DUPLICATION_RESPONSE_EXCERPT_BUDGET_CHARS``) in candidate rank order;
    past it a candidate is still named but carries ``excerpt_omitted: true``
    (read its file to judge it), and the response carries a top-level
    ``excerpts_omitted`` count + steering ``note``.
    ``response_format="concise"`` skips every body excerpt (candidates carry
    name/file/shape only -- open each file to judge); default ``"detailed"``
    includes the budgeted excerpts. Fails open with
    ``found: False`` on any ambiguity (unresolvable / untrusted repo, missing
    catalog, unparsable file). Never fabricates a candidate -- each is a
    function the bootstrap recorded.
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
    if not p.is_absolute():
        # A repo-relative file_path is the natural input form: the calls index
        # keys, search_codebase, and describe_codebase all emit relative paths.
        # Resolve it against the repo arg's root before find_repo_root, which
        # otherwise walks up from the server CWD and silently fails open.
        _arg_root, _ = _resolve_repo_arg(repo)
        if _arg_root is not None:
            p = (_arg_root / p).resolve()
    repo_root = find_repo_root(p)
    if repo_root is None:
        return _envelope({**empty, "reason": "path-unresolved"})

    expected_repo_id = _compute_repo_id(repo_root)
    # The repo arg is ADVISORY: find_repo_root re-homes the file to its OWN
    # repo/workspace, and the trust gate + index lookup below use that repo_id
    # (expected_repo_id). An origin-less monorepo derives a DISTINCT repo_id per
    # workspace root, so a caller following the documented "detect_repo once,
    # reuse repo_id" pattern passes the COORDINATOR id for a workspace file --
    # which used to mismatch here and return a blind, doc-invisible
    # "repo-arg-mismatch" negative indistinguishable from a real no-match. Proceed
    # with the file's own repo instead so the query answers for the workspace the
    # file actually lives in (origin-backed monorepos share one repo_id, so this
    # is a no-op there). Trust is still gated on expected_repo_id below.

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

    # Drop stale-catalog candidates whose source file is gone on disk
    # (deleted/moved since bootstrap) -- a "reuse this" pointer to a function that
    # no longer exists is a phantom, mirroring how get_crossfile_context drops
    # importers it cannot read. Gate on FILE existence, not an empty body_excerpt
    # (a present file's method can also yield an empty excerpt). This runs BEFORE
    # the cap so the truncation flag reflects the matches actually returned, not
    # phantoms hidden by the cap; file existence is a cheap stat, while the
    # excerpt read below (the expensive part) stays bounded to the capped set.
    _resolved_root = Path(repo_root).resolve()

    def _cand_file_live(cfile) -> bool:
        try:
            cabs = (_resolved_root / cfile).resolve()
            return cabs.is_file() and cabs.is_relative_to(_resolved_root)
        except OSError:
            return False

    live_matches = []
    for match in matches:
        live_cands = [c for c in match["candidates"] if _cand_file_live(c.get("file"))]
        if live_cands:
            match["candidates"] = live_cands
            live_matches.append(match)
    matches = live_matches

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

    # Body excerpts are the dominant cost of this response (the metadata rows
    # are ~200B each; a real function-dense TS file measured 31KB of bodies
    # alone), so they draw from one global char budget in candidate rank order.
    # A candidate past the budget is still NAMED -- the caller opens its file to
    # judge instead of paying for an inline body. Concise format skips the
    # excerpt reads entirely.
    fmt, fmt_note = _resolve_response_format(response_format)
    excerpt_lines = threshold_int("DUPLICATION_BODY_EXCERPT_LINES")
    excerpt_budget = (
        0 if fmt == "concise" else threshold_int("DUPLICATION_RESPONSE_EXCERPT_BUDGET_CHARS")
    )
    excerpts_omitted = 0
    for match in matches:
        fn = match["function"]
        fn["name"] = _sanitize(fn["name"])
        for cand in match["candidates"]:
            cand["name"] = _sanitize(cand["name"])
            cand["file"] = _sanitize(cand["file"])
            cand["shared_tokens"] = [_sanitize(t) for t in cand.get("shared_tokens", [])]
            if fmt == "concise":
                cand.pop("body_excerpt", None)
                continue
            if excerpt_budget > 0:
                excerpt = _sanitize(
                    _candidate_body_excerpt(repo_root, cand["file"], cand["name"], excerpt_lines)
                )
                cand["body_excerpt"] = excerpt
                excerpt_budget -= len(excerpt)
            else:
                cand["body_excerpt"] = ""
                cand["excerpt_omitted"] = True
                excerpts_omitted += 1

    out = {
        "found": True,
        "file": _sanitize(file_rel) if file_rel else None,
        "matches": matches,
    }
    if truncated:
        out["truncated"] = True
        out["truncated_matches"] = total_matches - max_matches
    notes = []
    if fmt_note:
        notes.append(fmt_note)
    if fmt == "concise" and any(m["candidates"] for m in matches):
        out["response_format"] = "concise"
        notes.append(
            "concise format: body excerpts omitted -- open each candidate's file to "
            "judge it, or re-call with response_format=detailed"
        )
    if excerpts_omitted:
        out["excerpts_omitted"] = excerpts_omitted
        notes.append(
            "body-excerpt budget reached: the lowest-ranked "
            f"{excerpts_omitted} candidate(s) carry excerpt_omitted=true -- "
            "read each one's file to judge it, or query a file with fewer new functions"
        )
    if notes:
        out["note"] = " | ".join(notes)
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

    # A linked worktree has no .chameleon of its own; every profile-artifact
    # read below (never the git ops, which stay on resolved_path so they see
    # the caller's actual worktree) resolves through the main worktree.
    _profile_root = None
    if resolved_path is not None:
        from chameleon_mcp.worktree import resolve_profile_root

        _profile_root = resolve_profile_root(resolved_path)

    # Engine-version mismatch is the strongest staleness signal: the analysis
    # logic, not just the codebase, changed. It outranks drift/age because a
    # refresh re-derives the profile regardless. This is the user-facing half of
    # the version-aware refresh (the refresh itself re-clusters on mismatch).
    engine_version_mismatch = False
    if _profile_root is not None:
        from chameleon_mcp.bootstrap.orchestrator import ENGINE_VERSION

        engine_version_mismatch = _engine_version_changed(
            _profile_root / ".chameleon", ENGINE_VERSION
        )

    # A pre-current schema_version means the clustering algorithm changed
    # underneath the profile, which the loader accepts silently (only a NEWER
    # schema is rejected). In practice an old schema rides along with an old
    # engine stamp, so the engine branch above catches it first — this check
    # closes the remaining gap (hand-edited or partially-migrated profiles).
    schema_outdated = False
    if _profile_root is not None:
        try:
            from chameleon_mcp.profile.schema import CURRENT_SCHEMA_VERSION

            _parsed = json.loads(
                (_profile_root / ".chameleon" / "profile.json").read_text(encoding="utf-8")
            )
            # A hand-edited / corrupt profile.json may parse to a non-dict (a JSON
            # array parses cleanly past the ValueError guard); .get on it would
            # raise AttributeError and crash this read tool. Treat any non-dict as
            # "no declared version" so the probe fails open.
            _declared = _parsed.get("schema_version") if isinstance(_parsed, dict) else None
            schema_outdated = isinstance(_declared, int) and _declared < CURRENT_SCHEMA_VERSION
        except (OSError, ValueError):
            schema_outdated = False

    # Production-pinned freshness: when a production_ref lock exists, compare
    # the profile's recorded derivation SHA with the locked ref's current tip
    # (the LOCAL ref — current as of the user's last fetch; no network).
    production_block: dict | None = None
    production_tip_moved = False
    if _profile_root is not None:
        try:
            from chameleon_mcp.production_ref import resolve_production_ref

            _prod_branch = _persisted_production_ref(_profile_root)
            if _prod_branch:
                _prod_resolved = resolve_production_ref(_profile_root, _prod_branch)
                _recorded = _recorded_derivation_sha(_profile_root / ".chameleon")
                production_block = {
                    "branch": _prod_branch,
                    "ref": _prod_resolved.ref if _prod_resolved else None,
                    "tip_sha": _prod_resolved.sha if _prod_resolved else None,
                    "derived_sha": _recorded,
                    "resolvable": _prod_resolved is not None,
                }
                if _prod_resolved is not None and _recorded is None:
                    # The profile's derivation SHA is unknown (profile.json
                    # unreadable/truncated, or missing derivation_source). We
                    # cannot know whether production moved, so do NOT assert
                    # tip_moved -- an affirmative "production moved" built from an
                    # unknown is a misreport (the OMIT-on-unknown discipline).
                    # Surface the unknown honestly instead.
                    production_block["derivation_unknown"] = True
                if (
                    _prod_resolved is not None
                    and _recorded is not None
                    and _recorded != _prod_resolved.sha
                ):
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
            _profile_root is not None
            and not (_profile_root / ".chameleon" / "profile.json").is_file()
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
    elif production_block is not None and production_block.get("derivation_unknown"):
        recommended = (
            "the profile's derivation commit is unrecorded or unreadable "
            "(profile.json missing or damaged); run /chameleon-refresh to re-derive"
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


def _profile_unrenderable_status(profile_dir: Path) -> str | None:
    """Status string when a PRESENT profile must not be rendered as healthy.

    Mirrors the ``detect_repo`` / ``get_pattern_context`` guard so a display path
    (``get_status``) does not describe an enforcement panel for a profile the
    load-bearing read path (``load_profile_dir``) refuses -- the hooks fail open
    and enforce nothing on such a profile, so reporting ``mode=enforce`` with
    active rules would be a false-clean. Returns ``None`` when the profile is
    absent (a different, caller-handled case) or readable by this engine.
    """
    from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

    _core_artifacts = ("archetypes.json", "canonicals.json", "rules.json")
    pf = profile_dir / "profile.json"
    if not pf.exists():
        # profile.json is the commit sentinel. If it is gone but core artifacts
        # remain, the profile is damaged (load_profile_dir raises 'missing
        # required artifact', hooks fail open) -- report corrupt, not a clean
        # no-profile. A dir with no core artifacts either is a genuine no-profile
        # (a different, caller-handled case).
        if any((profile_dir / c).exists() for c in _core_artifacts):
            return "profile_corrupted"
        return None
    try:
        from chameleon_mcp.bootstrap.transaction import is_committed

        if not is_committed(profile_dir):
            return "profile_corrupted"
        peek = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "profile_corrupted"
    sv = peek.get("schema_version") if isinstance(peek, dict) else None
    if sv is not None and (isinstance(sv, bool) or not isinstance(sv, int)):
        return "profile_corrupted"
    if isinstance(sv, int) and sv > MAX_SUPPORTED_SCHEMA_VERSION:
        return "profile_unsupported_schema_version"
    # profile.json parsing alone is not enough: a PRESENT-but-UNPARSEABLE core
    # artifact (archetypes/canonicals/rules) is what makes load_profile_dir raise
    # and the hooks fail open (enforce NOTHING) -- yet get_status would still
    # render mode=enforce with active rules over it, a false-clean. Flag that
    # here. Deliberately NOT a full load_profile_dir call: get_status renders
    # legitimately-partial profiles on purpose (e.g. a missing enforcement.json
    # still lists the read-time security pair), and the full load rejects those.
    # Two corruption shapes are the false-clean case: a present-but-unparseable
    # core JSON, and a cross-artifact GENERATION mismatch (e.g. archetypes.json
    # reset to a valid-but-empty {} by a crashed/partial write or a bad 3-way
    # .chameleon merge -- parses fine, but its generation no longer matches the
    # others, so load_profile_dir rejects it exactly as the refresh-side twin does).
    _core_objs: dict[str, dict] = {}
    for _core in _core_artifacts:
        _cp = profile_dir / _core
        if not _cp.exists():
            # A core artifact (archetypes/canonicals/rules) is REQUIRED:
            # load_profile_dir raises 'missing required artifact' on its absence
            # and the hooks fail open, so a missing one is unrenderable, not a
            # skippable partial. (enforcement.json and friends stay optional.)
            return "profile_corrupted"
        try:
            _obj = json.loads(_cp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "profile_corrupted"
        if not isinstance(_obj, dict):
            return "profile_corrupted"
        _core_objs[_core] = _obj
    # conventions.json is OPTIONAL (a repo may have none) but must PARSE if it is
    # present: load_profile_dir rejects an unparseable conventions.json and the
    # hooks fail open, so a corrupt one is unrenderable even though its absence is
    # fine. principles.md is free-form and never blocks the load, so it is skipped.
    _cv = profile_dir / "conventions.json"
    if _cv.exists():
        try:
            _cvobj = json.loads(_cv.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "profile_corrupted"
        if not isinstance(_cvobj, dict):
            return "profile_corrupted"
    # Generation parity mirrors load_profile_dir (profile/loader.py): when the
    # full core trio is present alongside profile.json, all four generations must
    # be equal integers or the profile is unloadable. Only checked when all are
    # present, so a legitimately-partial profile is never false-flagged.
    if len(_core_objs) == 3 and isinstance(peek, dict):
        _gens = (
            peek.get("generation"),
            _core_objs["archetypes.json"].get("generation"),
            _core_objs["rules.json"].get("generation"),
            _core_objs["canonicals.json"].get("generation"),
        )
        if not all(isinstance(_g, int) for _g in _gens) or len(set(_gens)) != 1:
            return "profile_corrupted"
    return None


def _enforcement_artifact_unreadable(profile_dir: Path) -> bool:
    """True when a PROFILED repo's ``enforcement.json`` is present-but-unparseable
    or absent.

    ``load_block_rules`` swallows a torn/truncated/non-dict artifact to ``{}``,
    which silently drops every MEASURED block rule (the calibration-exempt
    security pair stays armed via ``active_block_rules``) -- indistinguishable
    from a repo whose calibration legitimately kept zero measured rules, so the
    enforcement panel would read as healthy while the measured blocking is
    neutered. Every real profile writes this artifact, so absent is also
    damage. Gated on ``profile.json`` presence: an unprofiled repo is not an
    enforcement-artifact problem.
    """
    if not (profile_dir / "profile.json").exists():
        return False
    p = profile_dir / "enforcement.json"
    if not p.exists():
        return True
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(raw, dict)


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
    - ``idiom_review`` — whether the async turn-end review job's idiom lens
      runs (default on; advisory, never blocks).
    - ``idiom_judge`` — vestigial: no longer read by any lens.
    - ``correctness_judge`` — whether the async turn-end review job's
      correctness lens runs (default on; advisory, never blocks).
    - ``config_malformed`` — True when config.json is present but its enforcement
      section is unparseable, so enforcement is off (gates fail open) until fixed.

    Fail-open: a missing/corrupt config degrades to the safest default
    (advisory mode) rather than raising, and a missing/corrupt
    enforcement.json empties the MEASURED rules from ``active``. The two
    calibration-exempt security rules (hard-kind credential, eval/exec) stay
    listed on any profiled repo regardless of the artifact — the deny they
    back is read-time-exempt and still fires. The richer profile/trust/drift
    surface stays in the dedicated tools the /chameleon-status skill already
    calls; this returns only the enforcement section those tools do not cover.
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

    # A present profile the load path refuses (torn sentinel, non-int or
    # unsupported schema_version) makes the hooks fail open and enforce nothing;
    # rendering an enforcement panel for it would be a false-clean. Refuse it
    # the way detect_repo / get_pattern_context do instead of showing
    # mode=enforce with active rules beside a profile nothing can load.
    _unrenderable = _profile_unrenderable_status(profile_dir)
    if _unrenderable is not None:
        # Remediation must match what refresh will actually do: refresh REBUILDS a
        # corrupt profile but REFUSES a newer-schema one (it would downgrade a
        # teammate's work), so pointing the user at /chameleon-refresh there is
        # dead-end advice -- tell them to upgrade the engine instead.
        _remedy = (
            "Upgrade chameleon to read this profile (a newer engine wrote it)."
            if _unrenderable == "profile_unsupported_schema_version"
            else "Run /chameleon-refresh to regenerate it."
        )
        return _envelope(
            {
                "status": _unrenderable,
                "error": (
                    f"profile.json is unreadable by this engine ({_unrenderable}); "
                    f"enforcement is OFF (hooks fail open). {_remedy}"
                ),
            }
        )

    mode = "off"
    idiom_review = True
    idiom_judge = False
    correctness_judge = True
    config_malformed = False
    try:
        from chameleon_mcp.profile.config import (
            ChameleonConfigError,
            load_config_enforcement_only,
        )

        # Read the enforcement section in isolation, exactly as the enforcement
        # gates do, so a typo in an unrelated config section does not make status
        # report enforcement "off" while the gates are in fact still enforcing.
        _enf = load_config_enforcement_only(profile_dir)
        mode = _enf.mode
        idiom_review = _enf.idiom_review
        idiom_judge = _enf.idiom_judge
        correctness_judge = _enf.correctness_judge
    except ChameleonConfigError:
        # The enforcement section (or the whole file) is unparseable, so the
        # gates fail open and enforcement is genuinely OFF. Surface that (doctor
        # already does) instead of showing "off" beside an "active" secret rule,
        # which reads as a deliberate opt-out rather than a broken config.
        config_malformed = True
        mode = "off"
    except Exception:
        # Any other read failure: fail-open to the safe mode without crashing.
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
    # The security rules deny regardless of the persisted verdict (the
    # read-time exemption in active_block_rules), so status must list them
    # active even on a legacy zero-witness entry or a missing/torn
    # enforcement.json — otherwise it reports a deny gate as off while the
    # gate is firing. Gated on a profile actually existing (mirroring
    # _enforcement_artifact_unreadable): on an unprofiled repo no hook gate
    # can fire at all, and listing the rules active there would be a false
    # assurance. Arming additionally requires trust and mode=enforce, which
    # the /chameleon-status skill reads from detect_repo and `mode` alongside
    # this list.
    from chameleon_mcp.enforcement_calibration import SECURITY_BLOCK_RULES

    if (profile_dir / "profile.json").exists():
        active = sorted(set(active) | SECURITY_BLOCK_RULES)
        demoted = [d for d in demoted if d.get("rule") not in SECURITY_BLOCK_RULES]
    if config_malformed:
        # Enforcement is off because the config could not be parsed; do not list
        # rules as "active" when the mode that would arm them is unreadable.
        active, demoted = [], []

    # Live override-rate section. bootstrap fp_rate (above, calibration against
    # committed files) and the override rate (here, team contention on real AI
    # edits) are two distinct axes, not the same number: a rule can read
    # fp_rate=0.000 and still be overridden on most edits. Fail-open: a missing
    # drift.db / metrics log degrades to no section rather than crashing status.
    # build_override_audit never returns None on success -- a repo with zero
    # recorded activity still gets the {"rules": {}, ...} empty shape -- so
    # "no exception" alone is not "there is history"; only a non-empty
    # per-rule dict counts as real drift.db override history.
    overrides = None
    try:
        from chameleon_mcp.review_ledger import build_override_audit

        _repo_path, repo_id = _resolve_repo_arg(repo)
        if repo_id is not None:
            _audit = build_override_audit(repo_id)
            if _audit.get("rules"):
                overrides = _audit
    except Exception:
        overrides = None

    enforcement = {
        "mode": mode,
        "active": active,
        "demoted": demoted,
        "idiom_review": idiom_review,
        "idiom_judge": idiom_judge,
        "correctness_judge": correctness_judge,
        # True when config.json is present but its enforcement section could not
        # be parsed: enforcement is off (gates fail open) until it is fixed.
        "config_malformed": config_malformed,
        # True when the block-rules artifact (enforcement.json) is present-but-
        # unparseable or absent on a profiled repo: load_block_rules swallows that
        # to {}, dropping every measured rule from `active` (the calibration-
        # exempt security pair stays armed and listed). This distinguishes
        # "artifact damaged, regenerate it" from "repo legitimately has zero
        # measured block rules" -- the two are otherwise identical.
        "enforcement_artifact_unreadable": _enforcement_artifact_unreadable(profile_dir),
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

    # enforcement.json is committed (attacker-controllable) and trust persists
    # across changes, so screen every rule name / demotion reason for injection
    # before this status reaches the model. This artifact carries only rule NAMES
    # and engine-generated reasons (no free-text lint messages), so drop_prose_strings
    # is safe here -- a poisoned rule name reaching the model as an active/demoted
    # entry is dropped, not merely tag-neutralized. Counts/booleans pass through.
    enforcement = _sanitize_rules_value(enforcement, drop_prose_strings=True)

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

    # Trust gate: the ledger's verdict/findings text and attestation records
    # are derived from this repo's own reviewed commits and profile, so they
    # must not disclose one checkout's review/security history to a caller
    # whose OWN checkout was never granted trust for this repo_id (mirrors
    # get_rules / get_contract_breaks / the other model-callable read tools).
    if _repo_path is not None:
        from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

        _gate_rec = _trust_state_for(repo_id)
        if _gate_rec is None or not _gate_rec.grants_root(_repo_path):
            return _envelope(
                {
                    "status": "untrusted",
                    "repo_id": repo_id,
                    "records": [],
                    "total": 0,
                    "unverified": 0,
                }
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

    # A linked worktree has no .chameleon of its own; peek the main
    # worktree's committed profile.json instead (trust.grants_root/
    # is_material_change below already resolve this internally, but the
    # direct profile.json open for generation/schema_version did not).
    from chameleon_mcp.worktree import resolve_profile_root

    profile_dir = resolve_profile_root(repo_root) / ".chameleon"
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


def record_finding_fate(
    repo: str,
    fate: str,
    message: str,
    file: str | None = None,
    line: int | None = None,
    lens: str | None = None,
    confidence_at_emit: float | None = None,
    surface: str | None = None,
) -> dict:
    """Persist how a human adjudicated one review finding, into the repo's signed
    finding-fate ledger.

    Called per adjudicated finding by the review skills: ``/chameleon-pr-review``
    at verdict time, ``/chameleon-receiving-code-review`` per AGREE / PUSH BACK
    item, and ``/chameleon-deep-work`` per declined finding. ``fate`` is
    accepted / declined / converted (the skills' synonyms agree / push-back /
    convert normalize too). Only a 16-hex digest of ``message`` + ``file`` +
    ``line`` is stored, never the prose; ``lens`` and ``confidence_at_emit`` let a
    later read-back compute per-lens precision (``get_finding_fate_stats``).

    Best-effort and never blocks the review: any failure returns
    ``recorded: False`` rather than raising. Tamper-evident against a third local
    user, NOT forgery-proof against the reviewer (who holds the signing key) and
    NOT CI-verifiable.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope({"status": "failed", "error": "expected repo path or repo_id hex digest"})
    if not isinstance(fate, str) or not fate.strip():
        return _envelope(
            {"status": "failed", "error": "expected a fate (accepted / declined / converted)"}
        )
    if not isinstance(message, str) or not message.strip():
        return _envelope(
            {"status": "failed", "error": "expected the finding message (hashed, never stored)"}
        )

    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo", "recorded": False})

    try:
        from chameleon_mcp.review_ledger import record_finding_fate as _record

        record = _record(
            repo_id,
            message=message,
            file=file,
            line=line,
            lens=lens,
            confidence_at_emit=confidence_at_emit,
            fate=fate,
            surface=surface,
        )
    except ValueError as exc:
        return _envelope({"status": "failed", "recorded": False, "error": str(exc)})
    except Exception as exc:
        return _envelope(
            {
                "status": "failed",
                "recorded": False,
                "error": f"fate ledger unavailable: {type(exc).__name__}",
            }
        )

    return _envelope(
        {"status": "ok", "recorded": True, "signed": bool(record.get("hmac")), "record": record}
    )


def get_finding_fate_stats(repo: str) -> dict:
    """Per-surface, per-lens precision from the repo's finding-fate ledger.

    Aggregation only, advisory: precision = accepted / (accepted + declined) per
    lens (a converted-to-check finding is pending and excluded from the
    denominator), broken down by ``surface`` because ``accepted`` means different
    things at pr-review / deep-work / receiving. HMAC-unverified rows are excluded
    and counted under ``unverified``. Nothing here gates or calibrates; it is the
    read-back a lead (and, later, an outcome-calibrated lens-weighting step)
    consults. Fail-open: a missing ledger returns empty aggregates rather than
    raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope({"status": "failed", "error": "expected repo path or repo_id hex digest"})
    _repo_path, repo_id = _resolve_repo_arg(repo)
    # Carry the same data keys as the healthy shape (empty) so a consumer parses
    # one schema regardless of repo existence or a read failure.
    if repo_id is None:
        return _envelope({"status": "no_repo", "repo_id": None, "unverified": 0, "surfaces": {}})
    try:
        from chameleon_mcp.review_ledger import per_lens_precision

        stats = per_lens_precision(repo_id)
    except Exception:
        stats = {"repo_id": repo_id, "unverified": 0, "surfaces": {}}
    return _envelope(stats)


def get_shelved_findings(repo: str) -> dict:
    """Below-surface-bar findings currently shelved for a repo.

    A shelved row failed the repo's ``review.surface_bar`` (severity too low
    to interrupt a turn) but is not discarded -- it recurs toward
    auto-promotion (``CHAMELEON_SHELVED_PROMOTION`` /
    ``SHELVED_PROMOTE_MIN_RECURRENCE``) and feeds the self-learning idiom
    miner. Each row carries the canonical ``Finding`` fields (severity,
    claim, file, kind, ...) plus ``recurrence`` and ``session_ids``. This is
    the read-only browsing surface /chameleon-status and /chameleon-explain
    use to show what chameleon noticed but did not surface -- nothing here
    promotes, delivers, or drops a finding. Fail-open: a missing/corrupt
    ledger returns an empty list rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope({"status": "failed", "error": "expected repo path or repo_id hex digest"})
    _repo_path, repo_id = _resolve_repo_arg(repo)
    if repo_id is None:
        return _envelope({"status": "no_repo", "repo_id": None, "count": 0, "findings": []})
    try:
        from chameleon_mcp.review_ledger import shelved_findings as _shelved_findings

        rows = _shelved_findings(repo_id)
    except Exception:
        rows = []
    return _envelope({"status": "ok", "repo_id": repo_id, "count": len(rows), "findings": rows})


def list_idiom_candidates(repo: str) -> dict:
    """Idiom candidates the self-learning miner has proposed for a repo.

    Each row is an unapproved proposal (title, rationale, evidence trail,
    ``occurrences``, ``session_ids``) the miner writes under
    ``.chameleon/idiom-candidates/`` -- see ``core.idiom_candidates``. NONE
    of this is adopted automatically: a candidate becomes a real idiom only
    through the same ``/chameleon-teach`` (or ``/chameleon-auto-idiom``)
    approval path a hand-taught idiom uses. This is the read-only browsing
    surface /chameleon-auto-idiom calls to present "learned from usage"
    candidates for review.

    Unlike ``get_idiom_coverage``/``check_idiom_candidates``, this does NOT
    gate on trust: the candidates directory is deliberately excluded from
    the trust-hashed profile surface (hashing unreviewed miner output would
    arm the trust gate on proposals nobody has seen yet), so it stays
    readable regardless of trust state -- the human reviewing each
    candidate before approval is the safety boundary here, not the trust
    gate. Fail-open: a repo with no profile or no candidates directory
    returns an empty list rather than raising.
    """
    if not isinstance(repo, str) or not repo:
        return _envelope({"status": "failed", "error": "expected repo path or repo_id hex digest"})
    repo_path, _repo_id = _resolve_repo_arg(repo)
    if repo_path is None or not repo_path.is_dir():
        return _envelope({"status": "no_repo", "count": 0, "candidates": []})
    from chameleon_mcp.worktree import resolve_profile_root

    profile_dir = resolve_profile_root(repo_path) / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope({"status": "no_repo", "count": 0, "candidates": []})
    try:
        from chameleon_mcp.core.idiom_candidates import load_candidates

        rows = load_candidates(profile_dir)
    except Exception:
        rows = []
    return _envelope({"status": "ok", "count": len(rows), "candidates": rows})


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
      quality, AND nothing fired, so the per-edit lint never had a calibrated
      shape to check against. Route to ``/chameleon-refresh`` (re-derive
      archetypes) or ``/chameleon-teach`` (capture the missing convention).
    - ``in-scope-miss`` — an ast/exact archetype matched and chameleon raised
      NOTHING on the edit that later broke. The shape was covered but no rule
      fired; route to a new rule / idiom rather than a refresh.
    - ``advised`` — chameleon raised advisory violations (or shadow-logged a
      would-block) but did not block. The rules fired; they were advisory. This
      takes precedence over ``coverage-gap``: a fallback/none-quality file drops
      the archetype-SHAPE rules, so a violation raised there is an archetype-
      INDEPENDENT rule (a secret, an eval) that DID fire -- not a coverage gap
      (which would mis-route a flagged-and-overridden credential to a refresh).
      Route to enforce-mode calibration or a stronger rule, not a refresh. Kept
      distinct from ``in-scope-miss`` so a raised advisory is not misread as silence.
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

    # Trust gate: the replayed decision (archetype, match_quality, confidence_band,
    # blockable-rule list) is derived from THIS repo_id's own committed profile, so
    # it must not be replayed to a caller whose own checkout was never granted
    # trust for this repo_id (mirrors get_rules / get_review_history / the other
    # model-callable read tools).
    if repo_path is not None:
        from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

        _gate_rec = _trust_state_for(repo_id)
        if _gate_rec is None or not _gate_rec.grants_root(repo_path):
            return _envelope(
                {
                    "status": "untrusted",
                    "repo_id": repo_id,
                    "found": False,
                    "decision": None,
                    "classification": None,
                }
            )

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
    elif violations_raised > 0:
        # The gate was not silent: it raised advisories (or shadow-logged a
        # would-block) but did not block. Not a miss — the rules fired, they were
        # advisory — so a postmortem routes this apart from a true in-scope miss.
        # This precedes the coverage-gap check: an archetype-INDEPENDENT rule (a
        # secret, an eval) fires on a no-archetype file too, so a raised violation
        # there is "advised", NOT "coverage-gap" — the latter's remediation (run
        # /chameleon-refresh so an archetype resolves) is the wrong route when the
        # deterministic scanner already fired (a leaked credential the human
        # chameleon-ignored replayed as coverage-gap before this order).
        classification = "advised"
    elif match_quality in (None, "none", "fallback"):
        classification = "coverage-gap"
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
    from chameleon_mcp.bootstrap.transaction import atomic_profile_commit, cleanup_orphan_tmp_dirs
    from chameleon_mcp.profile.trust import hash_profile

    # The orphaned merge-driver / crashed-txn tmp-dir sweep is documented as
    # "called before every bootstrap/refresh", but was previously wired only
    # into the full-bootstrap path -- a day-to-day small edit takes this
    # partial-refresh path instead, so an orphan left by a killed mid-merge
    # (or a crashed prior commit) survived indefinitely under normal refresh
    # usage. Swept at the profile's actual home (profile_dir.parent), not the
    # scan root, so a linked-worktree redirect doesn't miss it.
    try:
        cleanup_orphan_tmp_dirs(profile_dir.parent)
    except Exception:
        pass

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
        except UnicodeDecodeError:
            # Undecodable idioms.md: do NOT substitute "" — this text is
            # written back into the transaction below, and an empty write
            # would silently destroy the user-authored idioms a full
            # bootstrap deliberately carries forward byte-identical (with a
            # warning). Fall back to the full path, which handles it.
            return None
        except OSError:
            idioms_text = ""

    summary_text = ""
    summary_path = profile_dir / "profile.summary.md"
    if summary_path.is_file():
        try:
            summary_text = summary_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unlike idioms.md this is generated content — an empty summary
            # is repaired by the next full derivation, never user data loss.
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
        # Stamp the carried artifact with this refresh's generation: the four
        # re-derived artifacts below all get new_generation, and a carried
        # conventions.json left one generation behind reads as a bundle
        # mismatch to any cross-artifact consistency check (doctor) even
        # though the content is current.
        if conventions_text is not None:
            try:
                _conv_obj = json.loads(conventions_text)
                if isinstance(_conv_obj, dict) and "generation" in _conv_obj:
                    _conv_obj["generation"] = new_generation
                    conventions_text = json.dumps(_conv_obj, indent=2, sort_keys=True)
            except (ValueError, TypeError):
                pass

    principles_text = ""
    principles_path = profile_dir / "principles.md"
    if principles_path.is_file():
        try:
            principles_text = principles_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Generated content, like the summary: an empty write here is
            # repaired by the next full derivation, never user data loss.
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

    # counterexamples.json and symbol_signatures.json are protocol files for the
    # same reason as calls_index.json (a failed FULL rebuild drops them rather than
    # serving stale model-steering data). A partial refresh is not a failed rebuild,
    # so carry them forward verbatim; otherwise a successful partial refresh silently
    # wipes the taught off-pattern counterexamples and the symbol index until the
    # next full refresh.
    from chameleon_mcp.safe_open import UnsafeFileError as _UnsafeFileErrorIdx
    from chameleon_mcp.safe_open import safe_read_profile_artifact as _safe_read_idx

    def _carry_protocol_index(name: str) -> str | None:
        path = profile_dir / name
        if not path.is_file():
            return None
        try:
            return _safe_read_idx(path, max_bytes=16_000_000)
        except (OSError, FileNotFoundError, _UnsafeFileErrorIdx):
            return None

    counterexamples_text = _carry_protocol_index("counterexamples.json")
    symbol_signatures_text = _carry_protocol_index("symbol_signatures.json")
    # These three are file/symbol-keyed (not archetype-keyed) and are now protocol
    # files, so the generic sibling carry-forward skips them; a partial refresh
    # does not re-derive them, so carry the prior copy verbatim (same as
    # calls_index) rather than dropping it and dark-firing phantom-symbol /
    # cross-file existence / duplication until the next full refresh.
    exports_index_text = _carry_protocol_index("exports_index.json")
    reverse_index_text = _carry_protocol_index("reverse_index.json")
    function_catalog_text = _carry_protocol_index("function_catalog.json")

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
            if counterexamples_text is not None:
                (txn_dir / "counterexamples.json").write_text(
                    counterexamples_text, encoding="utf-8"
                )
            if symbol_signatures_text is not None:
                (txn_dir / "symbol_signatures.json").write_text(
                    symbol_signatures_text, encoding="utf-8"
                )
            if exports_index_text is not None:
                (txn_dir / "exports_index.json").write_text(exports_index_text, encoding="utf-8")
            if reverse_index_text is not None:
                (txn_dir / "reverse_index.json").write_text(reverse_index_text, encoding="utf-8")
            if function_catalog_text is not None:
                (txn_dir / "function_catalog.json").write_text(
                    function_catalog_text, encoding="utf-8"
                )
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

    # The txn carried conventions.md forward verbatim (it is not a protocol
    # file), so a partial refresh that rewrote conventions.json/principles.md
    # must re-render the memory-channel mirror or it serves the pre-refresh
    # content until the next teach.
    _sync_conventions_md_from_disk(profile_dir)

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
    # Follow a linked git worktree to the main worktree BEFORE the locking /
    # noop-check / trust-preservation logic below runs. Without this, refresh
    # reads/writes repo_path's OWN .chameleon (which a linked worktree never
    # has), so every check below sees "no prior profile" and silently
    # bootstraps a brand-new, diverged profile INSIDE the worktree instead of
    # updating the shared one at the main worktree -- orphaned the moment the
    # worktree is removed, and permanently forked from main's profile in the
    # meantime (resolve_profile_root's own no-.chameleon-yet check now finds
    # one and stops resolving here). A no-op for every non-worktree case.
    # raw_repo_path stays the CALLER's actual location: the unsafe-root check
    # and the live statusline-cache write below must reason about where the
    # caller actually is, not the redirected write target; the re-derive
    # itself must also DISCOVER/PARSE the caller's real checked-out tree, not
    # main's -- see analysis_root below.
    from chameleon_mcp.worktree import resolve_profile_root

    raw_repo_path = resolved_path
    repo_path = resolve_profile_root(raw_repo_path)
    if not repo_path.is_absolute() or not repo_path.is_dir():
        return _envelope(
            {
                "status": "failed",
                "error": "refresh_repo expects an absolute repo path",
            }
        )

    # refresh resolves its path directly (never through find_repo_root), so it
    # must apply the unsafe-root guard itself — same hole bootstrap_repo had.
    # Checked on raw_repo_path, not the redirected main worktree: a linked
    # worktree in a temp/world-writable location must still be refused, since
    # the re-derive below reads real source files from it.
    refusal = _unsafe_root_refusal(raw_repo_path)
    if refusal is not None:
        return _envelope({"status": "failed", "error": refusal})

    # A locked production_ref (checked inside _refresh_repo_locked) takes
    # precedence over this; when unset, discovery/AST-parsing should read the
    # caller's actual checked-out tree rather than main's redirected one.
    _analysis_root = raw_repo_path if raw_repo_path != repo_path else None

    # Lock lives in plugin-data, NOT inside .chameleon/: atomic_profile_commit
    # renames the whole .chameleon/ dir away during refresh, which orphaned a
    # lock held inside it — a second /chameleon-refresh starting after the rename
    # flocked a DIFFERENT inode and ran concurrently. A stable per-repo
    # plugin-data path keeps the lock inode constant across the profile swap.
    from chameleon_mcp.profile.trust import repo_data_dir

    _repo_id = _compute_repo_id(repo_path)
    _lock_dir = repo_data_dir(_repo_id)
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
            # Migrate a still-legacy idioms.md (or fold a hand edit) before the
            # re-derive below reads it. Must run BEFORE the .idioms.lock /
            # .conventions.lock acquisition just below: migrate_idioms_md and
            # ensure_store_fresh take the same .idioms.lock internally, and
            # re-acquiring a lock this process already holds would hang until
            # its blocking_timeout. Skipped for a not-yet-bootstrapped repo (no
            # profile.json yet) -- there is no idioms.md to migrate, and
            # materializing an empty idioms/ dir here would leave a stray
            # partial profile ahead of the implicit-bootstrap path below.
            _profile_dir_for_migration = repo_path / ".chameleon"
            if (_profile_dir_for_migration / "profile.json").is_file():
                _idioms_md_for_migration = _profile_dir_for_migration / "idioms.md"
                # The noop/staleness check below treats a newer idioms.md mtime
                # as "a taught idiom landed since the last derive" and forces a
                # full re-derive. A first-time migration's own rewrite (format
                # transition only, e.g. the empty template regenerated through
                # the store) must not look like that fresh edit -- restore the
                # pre-migration mtime afterward so only a GENUINE prior edit
                # (whose mtime was already newer walking in, e.g. the legacy
                # hand-edit ensure_store_fresh folds in) still trips staleness.
                try:
                    _pre_migration_mtime_ns = _idioms_md_for_migration.stat().st_mtime_ns
                except OSError:
                    _pre_migration_mtime_ns = None
                _migrate_idioms_store_or_warn(_profile_dir_for_migration, _repo_id)
                if _pre_migration_mtime_ns is not None:
                    try:
                        if _idioms_md_for_migration.stat().st_mtime_ns != _pre_migration_mtime_ns:
                            os.utime(
                                _idioms_md_for_migration,
                                ns=(_pre_migration_mtime_ns, _pre_migration_mtime_ns),
                            )
                    except OSError:
                        pass
            # Hold .idioms.lock across the re-derive's idioms.md read AND the
            # atomic profile swap. teach/deprecate write idioms.md under this same
            # lock, so without it a teach landing between the orchestrator's
            # idioms read and the dir-swap would be silently clobbered by the
            # swap (it carries the pre-teach idioms snapshot). The bounded
            # blocking wait lets an in-flight teach finish first; the re-derive
            # then reads the post-teach idioms.md. .refresh.lock is always taken
            # before .idioms.lock and teach never takes .refresh.lock, so the two
            # cannot deadlock.
            # Hold BOTH teach write locks across the whole re-derive: .idioms.lock
            # (idioms.md, written by teach/deprecate) and .conventions.lock
            # (conventions.json + renames.json, written by teach_competing_import /
            # apply_archetype_renames). Without the latter, a competing-import teach
            # landing between the derive's conventions read and the dir-swap was
            # silently clobbered by the swap. Set the contextvar so the nested
            # bootstrap_repo sees the locks are already held and does not re-acquire
            # (same-process self-deadlock). Order .refresh -> .idioms -> .conventions
            # -> .bootstrap matches bootstrap_repo's direct path, so no AB-BA.
            idioms_lock_path = _lock_dir / ".idioms.lock"
            conventions_lock_path = _lock_dir / ".conventions.lock"
            try:
                with (
                    acquire_advisory_lock(idioms_lock_path, blocking_timeout=10.0),
                    acquire_advisory_lock(conventions_lock_path, blocking_timeout=10.0),
                ):
                    _wl_token = _REFRESH_HOLDS_WRITE_LOCKS.set(True)
                    try:
                        envelope = _refresh_repo_locked(
                            repo_path, force=force, analysis_root=_analysis_root
                        )
                    finally:
                        _REFRESH_HOLDS_WRITE_LOCKS.reset(_wl_token)
            except LockHeldError as e:
                return _envelope(
                    {
                        "status": "failed",
                        "error": (
                            f"a /chameleon-teach is in progress (PID {e.holder_pid}); retry shortly"
                        ),
                    }
                )
            _inject_production_ref_fetch(envelope, _prod_fetch)
            _inject_archetype_diff(envelope, repo_path, pre_state)
            _maybe_preserve_trust_across_refresh(repo_path, pre_state, envelope)
            # Keep the status line in sync with the post-refresh trust state
            # (a refresh can flip trusted->stale; the cache otherwise lags a session).
            # Cache keyed on raw_repo_path: that's the directory the live
            # session's statusline script actually reads (its own cwd), which
            # for a linked worktree differs from the redirected repo_path.
            try:
                _ts = (
                    detect_repo(str(repo_path / "profile.json")).get("data", {}).get("trust_state")
                )
                if isinstance(_ts, str) and _ts in ("trusted", "stale", "untrusted"):
                    _update_statusline_trust(raw_repo_path, _ts)
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
        # Extend the preserved root trust to every workspace-internal profile,
        # mirroring trust_profile's enumeration. A refresh of a trusted monorepo
        # root can CREATE a workspace profile that did not exist at the original
        # grant (a Python/Django app under a JS monorepo discovered on
        # re-derivation, say). Without this it lands UNTRUSTED, silently disabling
        # injection AND the enforcement deny gates for that entire workspace and
        # its framework — the user trusted the repo but a whole language's
        # enforcement is off. grant_trust still injection-scans each workspace's
        # prose, so a poisoned workspace profile is still refused per-workspace.
        workspaces_preserved = 0
        for child_chameleon in _iter_workspace_chameleon_dirs(repo_path):
            if child_chameleon == profile_dir:
                continue
            if not (child_chameleon / "profile.json").is_file():
                continue
            try:
                grant_trust(current_repo_id, child_chameleon)
                workspaces_preserved += 1
            except Exception:
                pass
        data["trust_preserved"] = True
        data["trust_preserve_reason"] = preserve_reason
        if workspaces_preserved:
            data["workspace_trust_preserved"] = workspaces_preserved
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
    """Return the engine version that WROTE the profile, or '' if absent.

    Prefers ``engine_version`` (the writer's version). Falls back to
    ``engine_min_version`` for profiles written before the two were split apart:
    back then that key held the writer's version, so the fallback keeps refresh
    staleness correct for every already-written profile. On current profiles the
    fallback is never reached, which matters because ``engine_min_version`` is now
    a static compatibility floor -- reading it here would report "engine changed"
    on every refresh forever.
    """
    import json as _json

    for fname in ("archetypes.json", "profile.json"):
        p = profile_dir / fname
        if p.is_file():
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            v = data.get("engine_version") or data.get("engine_min_version")
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
    except (OSError, UnicodeDecodeError):
        # Undecodable bytes are as incomplete as an unreadable file: force the
        # re-derive instead of crashing the refresh that would repair it.
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
      ``calls_index.json``, ``function_catalog.json``, ``symbol_signatures.json``
      and ``counterexamples.json`` for every supported language;
    - ``enforcement.json`` must parse to a dict whose ``block_rules`` is itself a
      dict (the shape ``active_block_rules`` reads);
    - ``exports_index.json`` / ``reverse_index.json`` for any language that writes
      them (TypeScript always; Python writes them too, so a corrupt one there must
      also repair; a Ruby profile never writes them, so their absence must not
      force a rebuild), and ``constant_index.json`` (Ruby's analogue) when present;
    - ``profile.summary.md`` must exist and be non-empty;
    - ``conventions.md`` (the CLAUDE.md-channel mirror) must exist whenever the
      profile's conventions render non-empty (kill switch honored);
    - ``principles.md`` must carry the anti-hallucination protocol.

    ``enforcement.json`` matters most: ``active_block_rules`` drops every
    MEASURED rule on a missing/corrupt file OR one whose ``block_rules`` is not
    a dict (only the calibration-exempt security pair stays active), so a
    damaged one silently voids the measured block-rule enforcement while
    ``mode=enforce`` still reads normal. The noop refresh would never repair it
    without this check.

    ``idioms.md`` is user-taught content (preserved across a re-derive), so a
    missing idioms file does NOT force a rebuild.
    """
    import json as _json

    from chameleon_mcp.bootstrap.transaction import is_committed

    # COMMITTED sentinel -- the loader's FIRST rejection (loader.py, checked before
    # generation/schema). A profile missing it is hard-rejected at read time
    # (profile_corrupted) with the message "run /chameleon-refresh", but the noop
    # refresh preserved it verbatim, looping that advice forever. Mirror the
    # sentinel gate here -- a re-derive rewrites COMMITTED -- so refresh repairs an
    # incomplete/torn-down profile like every other shape the loader rejects.
    if not is_committed(profile_dir):
        return True

    parsed_artifacts: dict[str, dict] = {}
    for name in ("archetypes.json", "canonicals.json", "rules.json", "conventions.json"):
        try:
            obj = _json.loads((profile_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(obj, dict):
            return True
        parsed_artifacts[name] = obj
    # conventions.json content-shape check: valid, parseable JSON whose
    # "conventions" sub-object is missing one of the top-level derived
    # sections (e.g. a hand-edit or bad merge that strips "naming" while
    # leaving the rest of the file intact) is corruption the parseability/dict
    # checks above don't catch. Every conventions.json extract_all_conventions
    # writes starts from empty_conventions(), so every section key is always
    # present -- possibly as an empty {} when nothing was derived -- so a
    # narrow field missing entirely means it was stripped after the fact.
    from chameleon_mcp.conventions import empty_conventions as _empty_conventions

    conv_sections = parsed_artifacts["conventions.json"].get("conventions")
    if not isinstance(conv_sections, dict):
        return True
    expected_sections = _empty_conventions(generation=0)["conventions"].keys()
    if not set(expected_sections) <= set(conv_sections.keys()):
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
    # A non-int / bool schema_version is corruption -> repair. A schema ABOVE the
    # supported max is NOT corruption: it is a newer-engine profile, and re-deriving
    # would downgrade a teammate's committed work. refresh_repo refuses that case
    # up front (the too-new-schema guard), so it never reaches here; do NOT force a
    # rebuild on it.
    if schema is not None and (isinstance(schema, bool) or not isinstance(schema, int)):
        return True
    # Cross-artifact generation gate -- mirrors load_profile_dir (profile/loader.py):
    # the loader REJECTS a profile whose profile/archetypes/rules/canonicals
    # generations are absent, non-int, or unequal. That is exactly the shape a
    # crashed/partial write or a bad 3-way .chameleon merge leaves (an archetypes
    # reset to ``{}`` -> generation None; or one artifact's generation skewed past
    # its siblings). Such a profile reads as ``profile_corrupted`` and the loader's
    # own error says "/chameleon-refresh recommended" -- but the noop refresh would
    # preserve it verbatim, leaving that advice a dead end. Re-derive so a plain
    # refresh repairs precisely what the loader rejects. The per-artifact dict check
    # above (an empty ``{}`` is still a dict) does NOT catch this.
    gens = (
        manifest.get("generation"),
        parsed_artifacts["archetypes.json"].get("generation"),
        parsed_artifacts["rules.json"].get("generation"),
        parsed_artifacts["canonicals.json"].get("generation"),
    )
    if not all(isinstance(g, int) for g in gens) or len(set(gens)) != 1:
        return True
    # Generated index artifacts: the noop paths preserve the profile dir
    # verbatim, so a deleted or corrupt index would otherwise stay missing
    # forever -- the loaders fail open to "no facts", silently degrading the
    # judge caller facts, the duplication prefilter, and the phantom-symbol /
    # cross-file checks. Each loader also HARD-REJECTS a foreign schema_version
    # (calls_index.py / function_catalog.py / symbol_index.py: != current;
    # counterexamples.py: outside its readable set), so a parseable index left
    # behind by an older engine is exactly as dead as a corrupt one -- get_callers
    # reports it as "calls-index-stale" and tells the user to refresh, so the
    # refresh must actually rebuild it. Mirror each loader's own gate via its
    # exported constant so a future schema bump propagates here automatically.
    from chameleon_mcp.calls_index import SCHEMA_VERSION as _CALLS_SV
    from chameleon_mcp.counterexamples import _READABLE_SCHEMA_VERSIONS as _CEX_SVS
    from chameleon_mcp.function_catalog import SCHEMA_VERSION as _CATALOG_SV
    from chameleon_mcp.symbol_signatures import SCHEMA_VERSION as _SIGS_SV

    index_schemas = [
        ("calls_index.json", (_CALLS_SV,)),
        ("function_catalog.json", (_CATALOG_SV,)),
        ("symbol_signatures.json", (_SIGS_SV,)),
        ("counterexamples.json", tuple(_CEX_SVS)),
    ]
    for name, readable in index_schemas:
        try:
            obj = _json.loads((profile_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(obj, dict):
            return True
        if obj.get("schema_version") not in readable:
            return True
    # Block-rule calibration. active_block_rules drops every measured rule on
    # anything whose inner "block_rules" value is not a dict (the calibration-
    # exempt security pair stays active) -- so a missing file, a non-dict top
    # level, OR a block_rules that is not itself a dict all silently void the
    # measured block-rule enforcement under mode=enforce. Mirror
    # load_block_rules' shape check so the noop refresh repairs every one of
    # those states, not just unparseable JSON.
    try:
        enf = _json.loads((profile_dir / "enforcement.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(enf, dict) or not isinstance(enf.get("block_rules"), dict):
        return True
    # Symbol indexes: reverse-indexed languages always write them (absence ==
    # damage); Ruby never writes them, so validate-if-present and don't force
    # a rebuild on absence. Shares REVERSE_INDEXED_LANGUAGES with the
    # bootstrap build gate so the repair check cannot drift from what
    # bootstrap actually writes.
    from chameleon_mcp.symbol_index import _READABLE_SCHEMA_VERSIONS as _SI_SVS
    from chameleon_mcp.symbol_index import REVERSE_INDEXED_LANGUAGES as _RIL

    for name in ("exports_index.json", "reverse_index.json"):
        path = profile_dir / name
        if manifest.get("language") in _RIL or path.exists():
            try:
                obj = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return True
            if not isinstance(obj, dict):
                return True
            if obj.get("schema_version") not in _SI_SVS:
                return True
    # constant_index.json is Ruby's analogue of the symbol indexes (Ruby has no
    # static export surface). It is Ruby-only and written inside the atomic txn;
    # a corrupt one silently voids the Ruby cross-file existence-break advisory +
    # get_blast_radius / get_contract_breaks. Validate-if-present (no other
    # language writes it, so absence must not force a rebuild).
    const_idx = profile_dir / "constant_index.json"
    if const_idx.exists():
        try:
            obj = _json.loads(const_idx.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        if not isinstance(obj, dict):
            return True
        # Same schema_version validation as exports_index.json/reverse_index.json
        # just above -- constant_index.py's own loader hard-rejects on a
        # mismatched schema_version exactly like symbol_index.py's readers do,
        # so a stale schema here must force the same repair, not survive every
        # noop refresh until the next full bootstrap.
        from chameleon_mcp.constant_index import SCHEMA_VERSION as _CI_SV

        if obj.get("schema_version") != _CI_SV:
            return True
    # profile.summary.md is preserved verbatim by the noop path; an empty or
    # whitespace-only summary (truncated write / bad merge) must repair too,
    # and so must garbage content that isn't actually a rendered summary
    # (matches the render_summary_md header every real writer emits).
    try:
        summary_text = (profile_dir / "profile.summary.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    if not summary_text.strip():
        return True
    if "chameleon profile summary" not in summary_text.lower():
        return True
    # conventions.md (the CLAUDE.md-channel mirror) deliberately does NOT force
    # a repair re-derive: it renders entirely from on-disk artifacts, so the
    # refresh noop path re-syncs it directly (_sync_conventions_md_from_disk) —
    # healing a missing, deleted, or pre-idioms-format mirror in milliseconds
    # instead of a full derivation.
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


def _refresh_repo_locked(repo_path, *, force: bool, analysis_root: Path | None = None) -> dict:
    """Execute refresh logic. Called while .chameleon/.refresh.lock is held.

    ``analysis_root``: when refresh_repo redirected ``repo_path`` from a
    linked worktree to its main worktree, this is the caller's ORIGINAL
    worktree path. Every ``bootstrap_repo(...)`` fallback below is handed it
    straight through (so the orchestrator discovers/parses the caller's real
    checkout, not main's); the working-tree staleness scan further down
    (discover_files / partial-refresh) uses it directly as the scan root,
    since a locked production_ref -- checked first, below -- always wins over
    both.
    """
    from chameleon_mcp import index_db
    from chameleon_mcp.bootstrap.discovery import discover_files
    from chameleon_mcp.bootstrap.orchestrator import (
        _glob_for_extractor,
        _select_extractor,
    )

    started_at = time.time()
    profile_dir = repo_path / ".chameleon"
    persisted_pg = _persisted_paths_glob(profile_dir)

    # Too-new-schema guard (BEFORE the force short-circuit and every re-derive
    # path below): a profile whose schema_version is ABOVE this engine's supported
    # max was written by a NEWER chameleon. It is a forward-compat mismatch, not
    # corruption -- re-deriving it (force, engine-upgrade, or repair path) would
    # DOWNGRADE and destroy a teammate's committed newer profile. The read path
    # already refuses it as profile_unsupported_schema_version; refuse here too,
    # even under force, rather than clobbering. Recovery is to upgrade chameleon
    # (or delete .chameleon to rebuild deliberately). A non-int/bool schema is
    # corruption, handled by the repair path, not here.
    from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION as _MAX_SCHEMA

    try:
        _pj_manifest = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _pj_manifest = None
    _pj_schema = _pj_manifest.get("schema_version") if isinstance(_pj_manifest, dict) else None
    if (
        isinstance(_pj_schema, int)
        and not isinstance(_pj_schema, bool)
        and _pj_schema > _MAX_SCHEMA
    ):
        return _envelope(
            {
                "status": "unsupported_schema_version",
                "error": (
                    f"profile schema_version {_pj_schema} is newer than this engine "
                    f"supports (max {_MAX_SCHEMA}); it was written by a newer chameleon. "
                    "Refusing to re-derive so the newer profile is not downgraded -- "
                    "upgrade chameleon to refresh it."
                ),
            }
        )

    if force:
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    repo_root = repo_path.resolve()
    repo_id = _compute_repo_id(repo_root)
    cached = index_db.get_repo(repo_id, repo_root_hint=str(repo_root))
    profile_path = profile_dir / "profile.json"

    if not (cached and profile_path.is_file()):
        # No prior profile: refresh implicitly bootstraps rather than refusing
        # (the documented idempotent-refresh design). Tag the envelope so the
        # caller can distinguish an INITIAL bootstrap from a re-derive -- the
        # status stays "success" because the profile written is complete and
        # correct, but "noop"/"refreshed" would misdescribe a first-time write.
        result = bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            result["data"]["implicit_bootstrap"] = True
        return result

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
    from chameleon_mcp.bootstrap.orchestrator import ENGINE_VERSION

    if _engine_version_changed(profile_dir, ENGINE_VERSION):
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    # Schema-outdated guard: an old-but-readable schema_version rides along with
    # an old engine stamp in practice (caught above), but a hand-edited or
    # partially-migrated profile.json can carry a stale schema_version on its
    # own. get_drift_status tells the user "run /chameleon-refresh to re-derive"
    # for exactly this state (schema_outdated), so a noop here would make that
    # advice a dead end. Re-derive fully rather than leaving the profile pinned
    # to the old clustering schema.
    from chameleon_mcp.profile.schema import CURRENT_SCHEMA_VERSION as _CURRENT_SCHEMA

    if (
        isinstance(_pj_schema, int)
        and not isinstance(_pj_schema, bool)
        and _pj_schema < _CURRENT_SCHEMA
    ):
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    # Repair guard: the noop and partial paths preserve artifacts verbatim, so a
    # structurally incomplete or corrupt profile (missing/unparseable core JSON,
    # missing summary, or principles lacking the protocol) would never be fixed by
    # a normal refresh. Re-derive fully to repair it. A full re-derive preserves
    # user-taught idioms.md.
    if _profile_needs_rederive(profile_dir):
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    # A deleted idioms.md is the one user-authored artifact a noop refresh would
    # silently leave gone: the production-pinned and working-tree noop paths below
    # preserve artifacts verbatim, so without this a `rm idioms.md` + refresh
    # reports noop and never recreates the template or warns. Re-derive once so the
    # bootstrap idioms carry-forward path writes a fresh template AND surfaces the
    # "idioms.md was missing; restore from git history" warning in the envelope.
    # Fires only while idioms.md is absent, so it self-heals on the next refresh.
    if not idioms_path.is_file():
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

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
            if inherited is None and is_subdir:
                # A coordinator-only toplevel (no language of its own) never
                # gets a root .chameleon/config.json, so the check above
                # always misses even though the toplevel DID resolve+lock a
                # production_ref at bootstrap -- check the coordinator-scoped
                # plugin-data lock before falling to a live re-detect, so an
                # explicit/auto-locked coordinator decision is never silently
                # overwritten by this workspace's own independent detection.
                inherited = _persisted_coordinator_production_ref(toplevel)
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
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )
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
                # Self-heal the memory-channel mirror even on noop: it is not a
                # derived-from-source artifact, so a missing or older-format
                # file regenerates from the committed profile at no cost.
                _sync_conventions_md_from_disk(profile_dir)
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
            return bootstrap_repo(
                str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
            )

    # Working-tree staleness needs an extractor and a discovery pass; the
    # production-pinned gate above deliberately runs first because it needs
    # neither — a workspace-coordinator root (no root tsconfig/TS deps) has
    # no root-level extractor, and the tip-keyed noop must still engage there.
    # Scanned off analysis_root (the caller's actual worktree) when set, so
    # this staleness check answers "did the tree the caller is IN change",
    # not "did main change" -- a worktree on its own branch with genuinely
    # different files must never noop off main's unrelated file count/mtimes.
    _scan_root = analysis_root if analysis_root is not None else repo_root
    try:
        extractor = _select_extractor(_scan_root)
    except Exception:
        extractor = None
    if extractor is None:
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    try:
        discovery_glob = persisted_pg or _glob_for_extractor(extractor)
        candidates = discover_files(_scan_root, glob=discovery_glob, paths_glob=persisted_pg)
    except Exception:
        return bootstrap_repo(
            str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
        )

    # A content-preserving rename (git mv) of a canonical-witness file keeps the
    # file COUNT and every mtime unchanged, so cardinality_match + nothing_newer
    # both stay true and the noop gate would preserve a profile whose canonical
    # witness now points at a deleted path -- the WHOLE archetype's excerpt then
    # reads `missing` (including untouched sibling files), silently, until a forced
    # refresh. A vanished canonical witness is real drift the noop check misses, so
    # re-derive fully to re-select a live witness. Bounded: canonicals.json read +
    # one is_file per witness, off the per-edit hot path (refresh only).
    try:
        _canon = json.loads((profile_dir / "canonicals.json").read_text(encoding="utf-8"))
        _canon_map = _canon.get("canonicals", {}) if isinstance(_canon, dict) else {}
        for _entries in _canon_map.values():
            for _e in _entries if isinstance(_entries, list) else []:
                _wp = (_e.get("witness") or {}).get("path") if isinstance(_e, dict) else None
                if _wp and not (repo_root / _wp).is_file():
                    return bootstrap_repo(
                        str(repo_path),
                        force=True,
                        paths_glob=persisted_pg,
                        analysis_root=analysis_root,
                    )
    except (OSError, ValueError):
        pass  # unreadable canonicals is handled by the repair guard above

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
        # Same mirror self-heal as the production-tip noop above.
        _sync_conventions_md_from_disk(profile_dir)
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
        # _scan_root, not repo_root: candidates' absolute paths (from
        # discover_files above) are relative_to()'d and re-read against
        # whichever root actually holds them.
        partial_envelope = _attempt_partial_refresh(
            _scan_root,
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

    return bootstrap_repo(
        str(repo_path), force=True, paths_glob=persisted_pg, analysis_root=analysis_root
    )


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


# A (re-)bootstrap reads idioms.md + conventions.json early and carries those
# snapshots forward into the atomic profile swap. teach_profile writes idioms.md
# under .idioms.lock and teach_competing_import / apply_archetype_renames write
# conventions.json (and renames.json) under .conventions.lock -- disjoint from the
# .bootstrap.lock the derive holds. So a teach that lands between the carry-read
# and the swap was silently clobbered by the swap (its success-reported write
# vanished). The derive must therefore serialize against BOTH write locks across
# its read->swap window. refresh_repo pre-acquires them around its whole
# re-derive and sets this contextvar so the nested bootstrap_repo does NOT
# re-acquire (a same-process flock re-acquire would self-deadlock). A direct
# bootstrap_repo (e.g. /chameleon-init force re-init) sees False and acquires them
# itself. Fixed order everywhere -- .idioms before .conventions, both before
# .bootstrap.lock -- so a refresh and a direct re-init cannot AB-BA deadlock.
_REFRESH_HOLDS_WRITE_LOCKS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "chameleon_refresh_holds_write_locks", default=False
)


@contextlib.contextmanager
def _bootstrap_write_locks(lock_dir: Path):
    """Serialize a (re-)bootstrap's carry-read -> profile-swap against the teach
    write locks (.idioms.lock + .conventions.lock). No-op when an outer refresh
    already holds them (avoids a self-deadlock); otherwise acquires both in the
    canonical order. A held write lock blocks briefly so an in-flight teach
    finishes first and the derive reads its post-teach state."""
    from chameleon_mcp.locks import acquire_advisory_lock

    if _REFRESH_HOLDS_WRITE_LOCKS.get():
        yield
        return
    with (
        acquire_advisory_lock(lock_dir / ".idioms.lock", blocking_timeout=10.0),
        acquire_advisory_lock(lock_dir / ".conventions.lock", blocking_timeout=10.0),
    ):
        yield


def bootstrap_repo(
    path: str,
    paths_glob: str | None = None,
    force: bool = False,
    now: float | None = None,
    production_ref: str | None = None,
    analysis_root: Path | None = None,
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

    ``analysis_root``, like ``now``, is internal-only (not exposed by the
    server.py MCP tool wrapper): refresh_repo passes the CALLER's actual
    linked-worktree path here when ``path`` has already been redirected to the
    main worktree, so discovery/AST-parsing still reads the caller's real
    checkout instead of main's. See ``_bootstrap_repo_unlocked``.
    """
    from chameleon_mcp.bootstrap.transaction import ProfileCommitError
    from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

    if paths_glob is not None and not isinstance(paths_glob, str):
        # Validated here alongside production_ref/now: an unvalidated non-string
        # paths_glob reached discover_files and crashed with an uncaught
        # AttributeError instead of a clean failed envelope.
        return _envelope(
            {
                "status": "failed",
                "error": "paths_glob must be a string glob or null",
            }
        )

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
        return _bootstrap_repo_unlocked(path, paths_glob, force, now, production_ref, analysis_root)
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
        with (
            _bootstrap_write_locks(lock_dir),
            acquire_advisory_lock(lock_dir / ".bootstrap.lock"),
        ):
            result = _bootstrap_repo_unlocked(
                path, paths_glob, force, now, production_ref, analysis_root
            )
        # A successful (re-)derive re-baselines drift: observations were scored
        # against the now-superseded profile, so the drift window resets to
        # empty. Harmless on a first bootstrap (no observations exist yet).
        if result.get("data", {}).get("status") == "success":
            from chameleon_mcp.drift.observations import reset_drift_baseline

            try:
                # A first bootstrap of a no-remote repo may have just minted a
                # repo_uuid into config.json, changing the repo's identity from
                # the pre-uuid path hash this function locked under. Recompute
                # (cache invalidated first — the same-process cache would
                # otherwise serve the stale id for its whole TTL) so the drift
                # baseline lands under the identity every later call resolves.
                from chameleon_mcp.repo_id import invalidate_repo_id_cache

                invalidate_repo_id_cache(repo_root)
                final_repo_id = _compute_repo_id(repo_root)
                reset_drift_baseline(final_repo_id)
                if final_repo_id != repo_id:
                    # The pre-uuid lock dir is now permanently unreachable
                    # debris (it held only the transient lock files). Locks
                    # were released by the with-block above. Sweep only if no
                    # live claimant is contending: unlinking a lock file a
                    # concurrent second bootstrap holds would let a third
                    # claimant recreate it on a fresh inode, silently
                    # splitting the mutual exclusion. A held lock skips the
                    # sweep whole — the debris then outlives this call, which
                    # is the bounded, safe outcome.
                    try:
                        with acquire_advisory_lock(lock_dir / ".bootstrap.lock"):
                            for _lf in (".conventions.lock", ".idioms.lock"):
                                with contextlib.suppress(OSError):
                                    (lock_dir / _lf).unlink()
                        with contextlib.suppress(OSError):
                            (lock_dir / ".bootstrap.lock").unlink()
                        with contextlib.suppress(OSError):
                            lock_dir.rmdir()
                    except LockHeldError:
                        pass
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
_CONTRACT_DIFF_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rb", ".py", ".pyi")


def get_contract_breaks(repo: str, base_ref: str = "main") -> dict:
    """ADVISORY: deterministic caller-contract breaks for a branch diff vs ``base_ref``.

    For each changed TypeScript/Ruby/Python source file, compares its callables'
    POSITIONAL parameter contract at the merge-base of ``base_ref`` and HEAD vs
    HEAD and flags a NARROWING
    (a new required positional arg, or an optional positional flipped required)
    that has committed callers -- the deterministic signal the LLM correctness
    judge derives from the diff, surfaced as a tool result a reviewer can cite.

    Each finding names the callable, its required-arg delta, and the committed
    call sites that may now mis-call it. Findings come in two shapes:

    - narrowing (the default): ``old_required_positional`` /
      ``new_required_positional`` carry the integer delta, no ``kind`` field.
    - ``kind: "removed_export_still_imported"``: an export this diff REMOVED
      outright (confirmed present at the merge-base) that indexed importers
      still reference; both positional fields are ``None`` -- there is no new
      signature to diff. Same existence-break class ``get_crossfile_context``
      reports repo-wide; a consumer reading both should cite the break once.

    Tool-time only (git show + AST re-parse);
    no network, no repo-code execution. Default-on; fails open to a no-signal
    result; never blocks. The pr-review skill cites these as FIX findings.
    """
    from chameleon_mcp._thresholds import threshold_int
    from chameleon_mcp.judge import _git_available, _run_git
    from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

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
    # Trust-gate: the calls index this tool joins to is a committed,
    # attacker-controllable artifact whose caller paths/names must not reach the
    # model surface from an untrusted profile (mirrors get_callers /
    # query_symbol_importers / get_duplication_candidates).
    expected_repo_id = _compute_repo_id(repo_root)
    gate = _trust_state_for(expected_repo_id)
    if gate is None or not gate.grants_root(repo_root):
        return _envelope({"status": "untrusted", "findings": []})
    if not _git_available(repo_root):
        return _envelope(
            {"status": "degraded", "reason": "not_a_git_worktree", "findings": [], "advisory": True}
        )
    if not isinstance(base_ref, str) or not base_ref.strip():
        return _envelope(
            {"status": "failed", "error": "base_ref must be a non-empty ref name", "findings": []}
        )
    if base_ref == "main":
        # config.json (holding the lock) lives at the MAIN worktree; resolve
        # for this read only -- the git ops below stay on repo_root itself.
        from chameleon_mcp.worktree import resolve_profile_root

        locked = _persisted_production_ref(resolve_profile_root(repo_root))
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
    _count, details, _reason = _compute_contract_breaks(
        repo_root, res.stdout or "", base_ref, threshold_int("AUTOPASS_MAX_FILES")
    )
    if _reason:
        # The contract-break check could not run (the calls index its grounding
        # depends on is missing/corrupt). Surface it as degraded rather than an
        # empty "clean" -- an absent index is "we could not check", not "no breaks"
        # (mirrors get_callers, which does not present a missing index as no callers).
        return _envelope(
            {
                "status": "degraded",
                "reason": _reason,
                "base_ref": base_ref,
                "findings": details,
                "advisory": True,
            }
        )
    return _envelope({"status": "ok", "base_ref": base_ref, "findings": details, "advisory": True})


def _compute_contract_breaks(
    repo_root: Path, numstat_text: str, base_ref: str, max_files: int
) -> tuple[int, list, str | None]:
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
        from chameleon_mcp.profile.trust import trust_state_for as _trust_state_for

        # Trust-gate: the calls index this joins to is a committed,
        # attacker-controllable artifact whose caller paths/names must not reach
        # the model surface from an untrusted profile. get_contract_breaks gates
        # before calling, but get_autopass_verdict reaches this directly, so gate
        # here too (mirrors get_callers / query_symbol_importers).
        _gate = _trust_state_for(_compute_repo_id(repo_root))
        if _gate is None or not _gate.grants_root(repo_root):
            return 0, [], None

        try:
            # config.json lives at the MAIN worktree; resolve for this read
            # only -- the git merge-base / contract_breaks calls below stay on
            # repo_root itself so HEAD means the caller's own checkout.
            from chameleon_mcp.worktree import resolve_profile_root

            enabled = load_config(
                resolve_profile_root(repo_root) / ".chameleon"
            ).enforcement.signature_contract_diff
        except Exception:
            enabled = True  # fail-open to on, mirroring judge_crossfile_facts
        if not enabled:
            return 0, [], None

        rows = parse_numstat(numstat_text)
        if len(rows) > max_files:
            # Over the file cap. For get_autopass_verdict a change this large
            # already routes to a human on size, so skipping the re-parse and
            # reporting count 0 is harmless (that consumer ignores the reason).
            # But get_contract_breaks (pr-review Step 2.9e) has NO size backstop:
            # a silent (0, [], None) would read as a verified clean and MASK a
            # real narrowing on exactly the large / fan-out diff that surface
            # targets. Return a reason so get_contract_breaks degrades ("could not
            # check, diff too large") instead of falsely reporting clean; the LLM
            # contract check then covers the narrowing.
            return 0, [], "diff_too_large"
        changed_src = [
            r["path"] for r in rows if str(r["path"]).lower().endswith(_CONTRACT_DIFF_EXTS)
        ]
        if not changed_src:
            return 0, [], None
        index = load_calls_index(repo_root)
        if index is None:
            # There ARE changed source files but the calls index is missing/corrupt,
            # so the contract-break check cannot run. Signal "degraded" (no grounding)
            # rather than a false "clean".
            return 0, [], "no-calls-index"
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
            # A barrel-chased caller row carries a `via` list of repo-derived
            # barrel paths; dict(c) copies it verbatim, so sanitize each element
            # too (same committed-artifact-is-untrusted invariant as path).
            if isinstance(out.get("via"), list):
                out["via"] = [
                    sanitize_for_chameleon_context(v) for v in out["via"] if isinstance(v, str)
                ]
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
        # Existence breaks: signature_diff only sees a symbol that NARROWED, not
        # one that was REMOVED outright (dropping the `export` keyword, or the
        # declaration itself) -- a plain removal never shows up as a positional-
        # arity change because there is no new signature to diff against. A
        # removed-but-still-imported export is exactly the kind of caller-
        # contract break this router exists to catch, so fold it in here rather
        # than let it hide behind a clean blast-radius count.
        # query_symbol_importers already computes this deterministically (its
        # `broken` list: an indexed importer that still references a name the
        # module's CURRENT on-disk export set no longer has), so reuse it
        # instead of re-deriving the same signal from a second git diff. But
        # query_symbol_importers only ever compares against the CURRENT
        # on-disk export set -- it has no notion of old_ref -- so a file
        # touched for an unrelated reason that already had a dangling import
        # at old_ref (a pre-existing break this diff did not introduce) would
        # otherwise be misattributed here. Only fold in a `broken` entry whose
        # name was actually exported AT old_ref, confirming this diff is what
        # removed it.
        from chameleon_mcp.lint_engine import detect_language as _cb_detect_language
        from chameleon_mcp.phantom_imports import (
            _current_export_names,
            _python_current_export_names,
        )

        for rel in changed_src:
            try:
                _qsi = query_symbol_importers(str(repo_root), str(repo_root / rel))
                _qdata = _qsi.get("data") or {}
            except Exception:
                continue
            _broken = _qdata.get("broken") or []
            if not _broken:
                continue
            _lang = _cb_detect_language(rel)
            if _lang not in ("typescript", "python"):
                # Ruby's query_symbol_importers path (_ruby_constant_importers)
                # always returns broken=[] (documented limitation, no
                # removed-method-still-called check), so this never fires for
                # .rb -- nothing to scope for languages outside these two.
                continue
            _old_show = _sig_run_git(["show", f"{old_ref}:{rel}"], cwd=repo_root)
            if _old_show is None or _old_show.returncode != 0:
                # File did not exist at old_ref (added within this diff) --
                # nothing to compare against, so a "broken" entry here cannot
                # be a removal relative to old_ref.
                continue
            _old_content = _old_show.stdout or ""
            if _lang == "python":
                _old_names, _old_open = _python_current_export_names(_old_content, rel)
            else:
                _old_names, _old_open = _current_export_names(_old_content)
            if _old_open:
                # old_ref's export set was open (export * from / from x import
                # *) -- cannot tell what it exported, so don't guess.
                continue
            for b in _broken:
                cnt = int(b.get("count", 0) or 0)
                if cnt <= 0:
                    continue
                _name = b.get("name")
                if not isinstance(_name, str) or _name not in _old_names:
                    continue
                # name/sites are already sanitized by query_symbol_importers's
                # own boundary cleanup; only the file path needs it here.
                details.append(
                    {
                        "file": sanitize_for_chameleon_context(rel),
                        "name": _name,
                        "old_required_positional": None,
                        "new_required_positional": None,
                        "caller_total": cnt,
                        "callers": list(b.get("sites") or []),
                        "kind": "removed_export_still_imported",
                    }
                )
        return len(details), details, None
    except Exception:
        return 0, [], None


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
    # config.json / enforcement.json live at the MAIN worktree, not a linked
    # worktree's own (absent) .chameleon -- resolved ONCE here and reused
    # below for every .chameleon/* read. git ops stay on repo_root itself:
    # they must diff the actual worktree the caller is in, not main.
    from chameleon_mcp.worktree import resolve_profile_root

    _profile_root = resolve_profile_root(repo_root)
    # A caller leaving the "main" default on a production-pinned repo almost
    # certainly means "the repo's mainline" — which the lock names better. An
    # explicit non-default base_ref is always honored as given.
    if base_ref == "main":
        _locked = _persisted_production_ref(_profile_root)
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
        active = active_block_rules(_profile_root / ".chameleon")
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

    _REVERSE_INDEX_EXTS = (
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".py",
        ".pyi",
        ".rb",
    )

    def importers_of(rel: str) -> int | None:
        # query_symbol_importers covers the JS/TS and Python module graphs (the
        # reverse index) and the Ruby constant graph (the constant index), all
        # built at bootstrap; it dispatches each by language. This must stay in
        # step with the Stop judge-router, which already counts .rb fan-out -- a
        # Ruby file uncovered here read blast_radius=0 and skipped the blast gate.
        # A file outside those extensions is uncovered by design and contributes
        # 0 (not "unknown"); for a covered file, any
        # unreadable answer -- untrusted profile, missing index,
        # deleted/unreadable module, a raise -- returns None so the router counts
        # it as UNKNOWN fan-out instead of assuming 0, which is the auto-pass
        # direction and the wrong default.
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
    contract_break_count, contract_break_details, _cb_reason = _compute_contract_breaks(
        repo_root, numstat_text, base_ref, max_files_cap
    )

    # Attestation-gated governance (roadmap #7, default on, kill
    # CHAMELEON_AUTOPASS_ATTESTATION=0). The session attestations record whether
    # the diff was produced with verification off, a degraded correctness judge,
    # or inline overrides; fold that in RAISE-ONLY so an under-governed diff
    # routes to a human on terms a fully-governed one does not. Tool-time,
    # fail-open: any read error leaves the coverage all-clear (no change).
    session_coverage = None
    if os.environ.get("CHAMELEON_AUTOPASS_ATTESTATION") != "0":
        try:
            from chameleon_mcp.autopass import parse_numstat, session_coverage_from_attestations
            from chameleon_mcp.review_ledger import read_session_attestations

            changed_files = [r["path"] for r in parse_numstat(numstat_text)]
            records = read_session_attestations(
                repo_id, limit=threshold_int("ATTESTATION_MATCH_LIMIT")
            )["records"]
            session_coverage = session_coverage_from_attestations(records, changed_files)
        except Exception:
            session_coverage = None

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
        session_coverage=session_coverage,
    )
    verdict["advisory"] = True
    verdict["base_ref"] = base_ref
    verdict["contract_breaks"] = contract_break_details
    # changed_files are git-diff paths; a crafted path can cross-encode a close
    # tag across the separator, so sanitize them on the way to the model surface
    # exactly as the contract-break paths in the same envelope are sanitized.
    if isinstance(verdict.get("changed_files"), list):
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _san_path

        verdict["changed_files"] = [
            _san_path(f) if isinstance(f, str) else f for f in verdict["changed_files"]
        ]
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
                # `enforcement.calibration` makes the demotion thresholds
                # per-repo tunable and lets a repo disable auto-demotion
                # entirely. Read via the isolated enforcement-only loader (not
                # the whole-config load_config) so a typo in an unrelated
                # section (auto_refresh, trust, ...) cannot silently disable
                # this feedback loop. Fail-open to the pre-config
                # _thresholds-driven values -- identical to a repo with no
                # `enforcement.calibration` section -- on any read error.
                try:
                    from chameleon_mcp.profile.config import load_config_enforcement_only

                    cal = load_config_enforcement_only(profile_dir).calibration
                    auto_demote = cal.auto_demote
                    demote_threshold = cal.override_rate_threshold
                    demote_min_events = cal.min_events
                    demote_min_sessions = cal.min_distinct_sessions
                except Exception:
                    auto_demote = True
                    demote_threshold = threshold_float("RULE_FP_DEMOTE_THRESHOLD")
                    demote_min_events = threshold_int("OVERRIDE_AUDIT_MIN_EVENTS")
                    demote_min_sessions = threshold_int("OVERRIDE_DEMOTION_MIN_SESSIONS")
                if auto_demote:
                    verdicts = apply_override_feedback_demotion(
                        verdicts,
                        rates,
                        threshold=demote_threshold,
                        min_events=demote_min_events,
                        min_distinct_sessions=demote_min_sessions,
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
    analysis_root: Path | None = None,
) -> dict:
    """First-time analysis: AST scan + (Phase 2D interview) + atomic profile commit.

    For monorepos with detected workspace_paths, runs the full
    pipeline per workspace as well, producing one `.chameleon/` under each
    workspace root in addition to the root profile that catalogs them.

    Bug 1: `path` accepts either an absolute repo path or a
    64-char repo_id hex digest (for repos previously bootstrapped). See
    `_resolve_repo_arg`.

    ``analysis_root``: an explicit override for what tree to discover/parse,
    used when a caller (refresh_repo) already redirected ``path`` to the main
    worktree and knows the ORIGINAL linked-worktree path it redirected from.
    When unset, this function infers the same thing locally by comparing its
    own raw-resolved root against the (possibly worktree-redirected) write
    root. Either way, a locked production_ref still wins (see below).

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
        raw_root = resolved_path.resolve()
    except (OSError, ValueError):
        raw_root = resolved_path
    # Follow a linked git worktree to the main worktree's ALREADY-COMMITTED
    # profile (a no-op when there is none yet, e.g. a genuine first bootstrap
    # run from inside a worktree, which still writes there). Without this, a
    # forced re-bootstrap invoked from a worktree diverges the same way an
    # unresolved refresh does -- see refresh_repo's identical guard. repo_root
    # is the WRITE/identity root from here on; raw_root stays the caller's
    # actual location for the safety check and (below) the analysis root, so
    # a re-bootstrap from a worktree still analyzes the worktree's own
    # checked-out files, not main's -- only the .chameleon write redirects.
    from chameleon_mcp.worktree import resolve_profile_root

    repo_root = resolve_profile_root(raw_root)
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
    # Checked on raw_root (the caller's actual location, which analysis reads
    # from below when it differs from repo_root) -- not the redirected main
    # worktree, whose safety says nothing about a linked worktree elsewhere.
    refusal = _unsafe_root_refusal(raw_root)
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

    # Too-new-schema guard, checked even under force=True: an existing
    # profile.json whose schema_version is ABOVE this engine's supported max
    # was written by a NEWER chameleon. refresh_repo already refuses to
    # re-derive over that profile even under force (unsupported_schema_version)
    # -- without the same guard here, force=True would silently downgrade and
    # destroy a teammate's committed newer profile, the one scenario refresh
    # explicitly protects against. A non-int/bool schema is corruption, not
    # this case, and is left to the normal re-derive path.
    _existing_profile_path = repo_root / ".chameleon" / "profile.json"
    if _existing_profile_path.is_file():
        from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION as _MAX_SCHEMA_BOOT

        try:
            _existing_manifest = json.loads(_existing_profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _existing_manifest = None
        _existing_schema = (
            _existing_manifest.get("schema_version")
            if isinstance(_existing_manifest, dict)
            else None
        )
        if (
            isinstance(_existing_schema, int)
            and not isinstance(_existing_schema, bool)
            and _existing_schema > _MAX_SCHEMA_BOOT
        ):
            return _envelope(
                {
                    "status": "unsupported_schema_version",
                    "error": (
                        f"profile schema_version {_existing_schema} is newer than this "
                        f"engine supports (max {_MAX_SCHEMA_BOOT}); it was written by a "
                        "newer chameleon. Refusing to overwrite so the newer profile is "
                        "not downgraded -- upgrade chameleon to bootstrap over it."
                    ),
                }
            )

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
    # A locked production_ref wins (existing precedence: the team's declared
    # mainline over any working tree). Otherwise, when repo_root was
    # redirected to a linked worktree's main, analyze the CALLER'S actual
    # checked-out tree rather than main's -- discovery/AST-parsing must see
    # the files the caller is actually working on; only the commit target
    # moved. An explicit analysis_root from a caller who already redirected
    # `path` (refresh_repo) wins over this function's own (now-moot) raw_root
    # inference; plain non-worktree bootstrap is unchanged (None either way).
    _worktree_analysis_root = (
        analysis_root
        if analysis_root is not None
        else (raw_root if raw_root != repo_root else None)
    )
    _analysis_root = prod_state.tree if prod_state.tree is not None else _worktree_analysis_root
    try:
        report = _bootstrap(
            repo_root,
            paths_glob=paths_glob,
            now=now,
            analysis_root=_analysis_root,
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
                    _analysis_root if _analysis_root is not None else repo_root,
                    paths_glob=paths_glob,
                )
            except Exception:
                file_cluster_rows = None
            if file_cluster_rows is not None:
                index_db.delete_all_file_clusters(repo_id)
                if file_cluster_rows:
                    index_db.upsert_file_clusters(repo_id, file_cluster_rows)
        elif (
            report.status == "success_workspaces_only"
            and prod_state.persist
            and prod_state.locked
            and prod_state.branch
        ):
            # A coordinator-only root (no language signal of its own) never
            # gets its own .chameleon/, so _persist_production_ref's usual
            # config.json target does not exist here and the toplevel's
            # resolved lock would otherwise vanish -- persist it to the
            # coordinator-scoped plugin-data file instead, so detect_repo's
            # walk-up and refresh_repo's inheritance fallback (both keyed
            # off git_toplevel) can find it for every workspace underneath.
            from chameleon_mcp.production_ref import git_toplevel

            _coord_toplevel = git_toplevel(repo_root) or repo_root
            _persist_coordinator_production_ref(_coord_toplevel, prod_state.branch)
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

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 1000:
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
                    # A linked worktree has no .chameleon of its own; without
                    # this, a row recorded at a still-live worktree always
                    # short-circuits past is_material_change below (no local
                    # .chameleon to find) and silently never goes stale.
                    from chameleon_mcp.worktree import resolve_profile_root

                    profile_dir = resolve_profile_root(Path(row_root)) / ".chameleon"
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
    # A linked worktree has no .chameleon of its own; check the main
    # worktree's committed profile before declaring this row dead -- an
    # index_db row recorded (by an older chameleon) at a still-live worktree
    # must not be pruned just because ITS OWN directory has no .chameleon.
    from chameleon_mcp.worktree import resolve_profile_root

    return not (resolve_profile_root(root) / ".chameleon" / "profile.json").is_file()


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

    The ``repo`` argument is accepted for MCP-schema uniformity but unused:
    the merge driver invokes this per-file with no repo context, and the merge
    reads/writes only the three explicit paths (its own atomic tmp+replace,
    outside the profile-transaction protocol). No trust gate applies — the
    write is Write-equivalent on paths the caller already controls.
    """

    # never-raise / fail-open contract: base/ours/theirs are file-path strings a
    # malformed call could send as a list/dict, which Path() rejects with a
    # TypeError. Decline cleanly instead of leaking a traceback to the driver.
    if not all(isinstance(x, str) for x in (base, ours, theirs)):
        return _envelope(
            {
                "status": "failed",
                "error": "base/ours/theirs must be file path strings",
                "merged_profile_path": None,
            }
        )
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

        # The cluster_size/witness merge below assumes each archetype maps to a
        # DICT (archetypes.json). counterexamples.json also has a top-level
        # "archetypes" key, but maps each archetype to a LIST of off-pattern rows
        # (schema v2), which would crash on `.get(...)`. That artifact is a
        # regenerable protocol file and is deliberately NOT routed to this driver
        # (.gitattributes-template), but decline cleanly rather than crash if it
        # ever reaches here — the next /chameleon-refresh rebuilds it from the
        # merged conventions.json.
        if any(not isinstance(v, dict) for v in (*ours_archs.values(), *theirs_archs.values())):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        "archetype values are not objects (regenerable protocol "
                        "artifact); leaving the conflict for manual resolution"
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
        # never-raise / fail-open contract: a hand-/merge-mangled payload that is
        # a list or scalar (not a dict) would raise AttributeError/TypeError in
        # the union below. Decline cleanly, like the archetypes branch does.
        if not isinstance(ours_payload, dict) or not isinstance(theirs_payload, dict):
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"{data_key} payload is not an object (corrupt/mangled "
                        "artifact); leaving the conflict for manual resolution"
                    ),
                    "merged_profile_path": None,
                }
            )
        if data_key == "conventions":
            # conventions.json's top-level keys are the FIXED dimension names
            # (imports/naming/inheritance/...), identical on BOTH sides, so a
            # shallow {**theirs, **ours} degenerates to "ours wins wholesale" and
            # silently drops theirs' per-archetype convention additions. Union one
            # level deeper: within each dimension, merge the archetype-keyed
            # entries (ours wins on a per-archetype conflict).
            merged_payload = {}
            for dim in {*ours_payload, *theirs_payload}:
                od = ours_payload.get(dim)
                td = theirs_payload.get(dim)
                if isinstance(od, dict) and isinstance(td, dict):
                    merged_payload[dim] = {**td, **od}
                elif dim in ours_payload:
                    merged_payload[dim] = od
                else:
                    merged_payload[dim] = td
        else:
            # canonicals / rules key their payload by ARCHETYPE (names that
            # legitimately differ between branches), so the shallow union is the
            # right granularity there.
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

    Best-effort, never raises. ``grant_trust`` re-runs its idioms.md/principles.md
    injection scan and raises ProfileInjectionError on a poisoned teach, which is
    swallowed -- so the re-stamp is skipped. Under the default one-time-trust
    policy (CHAMELEON_TRUST_REVALIDATE unset) the profile then stays TRUSTED on the
    PRIOR grant, NOT "stale"; the injection defense for what reaches the model is
    the render-time prose screen (loader.safe_prose_text / _prose_injection_unsafe
    + the conventions/summary/idiom-coverage screens), not staleness. Only under
    CHAMELEON_TRUST_REVALIDATE=1 does a skipped re-stamp surface as "stale".
    """
    if not (was_trusted and repo_id):
        return
    try:
        from chameleon_mcp.profile.trust import grant_trust

        grant_trust(repo_id, profile_dir)
    except Exception:  # noqa: BLE001
        pass


def _migrate_idioms_store_or_warn(profile_dir: Path, repo_id: str | None) -> dict | None:
    """Trigger the idioms.md -> store migration (and fold any legacy hand
    edit) for a caller that never writes idioms itself.

    Unlike teach's write path (which aborts on a failed migration so it never
    seeds a store missing the pre-migration idioms), refresh and trust only
    READ idioms indirectly -- the render/read paths already fall back to
    parsing legacy idioms.md, so a migration failure here must not block the
    caller. A failure that also left no store behind is not silenced, though:
    it prints one line so an operator can notice the repo is still running on
    the legacy parser.

    Skips the mutation entirely when idioms.md/principles.md currently fails
    the injection scan: both migrate_idioms_md and ensure_store_fresh
    regenerate idioms.md from parsed "### " blocks, and free-form prose with
    no recognized block -- exactly the shape a raw injection payload has --
    is dropped from the regenerated view rather than quarantined. Rewriting
    over poisoned content would launder it out of the file BEFORE a caller's
    own injection scan (grant_trust, refresh's trust-preservation re-grant)
    gets a chance to see and refuse it. trust_profile additionally pre-checks
    this itself for a clear user-facing refusal; this is the backstop for
    every other caller, present and future.

    Returns ``migrate_idioms_md``'s own return value when a migration ran (or
    was already a noop), or None when the mutation was skipped (poisoned
    content) or raised.
    """
    from chameleon_mcp.profile.trust import injected_prose_artifact

    if injected_prose_artifact(profile_dir) is not None:
        import sys

        print(
            "chameleon: idiom-store migration skipped: idioms.md or principles.md "
            "failed the injection scan; review .chameleon/ before re-running "
            "/chameleon-teach or /chameleon-trust",
            file=sys.stderr,
        )
        return None
    try:
        from chameleon_mcp.core.idiom_store import ensure_store_fresh, migrate_idioms_md

        migrate_result = migrate_idioms_md(profile_dir, repo_id=repo_id)
        ensure_store_fresh(profile_dir, repo_id=repo_id)
        return migrate_result
    except Exception:
        from chameleon_mcp.core.idiom_store import store_exists

        if not store_exists(profile_dir):
            import sys

            print(
                "chameleon: idiom-store migration failed; continuing on legacy idioms.md",
                file=sys.stderr,
            )
        return None


def teach_profile(repo: str, feedback: str, archetype: str | None = None) -> dict:
    """Append a captured idiom to .chameleon/idioms.md.

    ``archetype`` optionally scopes a free-form idiom to one archetype (e.g.
    ``controller``); the per-edit block and turn-end nudge then surface it first
    on edits of that archetype. Omitted (or unrecognized), the idiom is written
    untagged and treated as general, which applies to every archetype.

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
    from chameleon_mcp.locks import LockHeldError

    # never-raise / fail-open contract: a malformed call could send feedback as a
    # list/dict, which the sanitize/regex path below would crash on.
    if not isinstance(feedback, str):
        return _envelope(
            {
                "status": "failed",
                "error": f"feedback must be a string; got {type(feedback).__name__}",
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
    if not repo_path.is_dir():
        return _envelope({"status": "failed", "error": f"repo path is not a directory: {repo!r}"})

    # A linked worktree has no .chameleon of its own; the idiom is captured
    # into the main worktree's committed idioms.md.
    from chameleon_mcp.worktree import resolve_profile_root

    repo_path = resolve_profile_root(repo_path)

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

    # Imported unconditionally (not inside the try below) so the except
    # branch can always call it, even if the try's own import line is what
    # raised.
    from chameleon_mcp.core.idiom_store import store_exists

    # No store yet means the next call below would migrate the live
    # idioms.md. A migration regenerates idioms.md from parsed "### " blocks
    # and drops everything else -- exactly the shape a raw injection payload
    # has -- so a poisoned pre-migration file would be laundered clean before
    # this teach's own idiom ever gets a chance to fail the scan. Refuse
    # loudly here instead, the same pre-migration check trust_profile runs.
    if not store_exists(_profile_dir):
        from chameleon_mcp.profile.trust import injected_prose_artifact

        _poisoned = injected_prose_artifact(_profile_dir)
        if _poisoned is not None:
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"{_poisoned} contains a suspicious pattern; clean it "
                        f"(or review .chameleon/{_poisoned}) before teaching"
                    ),
                }
            )

    _migrate_result: dict | None = None
    try:
        from chameleon_mcp.core.idiom_store import ensure_store_fresh, migrate_idioms_md

        _migrate_result = migrate_idioms_md(_profile_dir, repo_id=_repo_id)
        ensure_store_fresh(_profile_dir, repo_id=_repo_id)
    except Exception as exc:
        if not store_exists(_profile_dir):
            # A failed migration rolls back the store dir entirely (see
            # migrate_idioms_md); proceeding here would let teach_record seed a
            # brand-new store containing only this one idiom, so every rendered
            # view would drop the whole legacy idiom set down to one entry.
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"idiom store migration failed ({exc}); teach aborted, idioms.md unchanged"
                    ),
                }
            )
        # The store exists (either migration never needed to run, or
        # ensure_store_fresh failed after the store was already there) --
        # that failure mode is additive-only against an intact store, so
        # falling through and letting this teach proceed is safe.

    suspicious, suspicious_pattern = _looks_suspicious(feedback)

    sanitized = _sanitize_user_input(feedback)
    if not sanitized.strip():
        return _envelope({"status": "failed", "error": "feedback is empty after sanitization"})
    if len(sanitized) > 50_000:
        return _envelope({"status": "failed", "error": "feedback exceeds 50KB cap"})

    body = _escape_markdown_section_headings(sanitized)

    timestamp = time.strftime("%Y-%m-%d", time.gmtime())

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

    from chameleon_mcp.core.idiom_store import IdiomRecord, records_from_markdown, teach_record

    if body.lstrip().startswith("### "):
        # Pre-rendered block (structured teach delegation, or a user-authored
        # header in free-form feedback): parse it with the migration importer
        # so the block's own metadata wins over the auto-derived fields.
        parsed, rejected = records_from_markdown(f"# idioms\n\n## active\n\n{body.strip()}\n")
        if not parsed:
            return _envelope(
                {"status": "failed", "error": "idiom block could not be parsed"}
                if rejected
                else {"status": "failed", "error": "feedback contained no idiom body"}
            )
        record = parsed[0]
        if not record.languages and language != "any":
            record.languages = [language]
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
        # An optional, recognized archetype scopes the idiom (P3 then surfaces it
        # first on that archetype's edits). An unrecognized value is dropped, not
        # erroring, so a typo leaves a general idiom rather than failing the teach.
        from chameleon_mcp.profile.schema import ARCHETYPE_NAME_RE

        record = IdiomRecord(
            slug=slug,
            title=slug,
            rationale=body.strip(),
            languages=[] if language == "any" else [language],
            archetypes=(
                [archetype.strip()]
                if isinstance(archetype, str) and ARCHETYPE_NAME_RE.match(archetype.strip())
                else []
            ),
            status="active",
            added_date=timestamp,
            rank=0,
        )

    try:
        outcome = teach_record(_profile_dir, record, repo_id=_repo_id)
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another operation holds the idioms lock (PID {e.holder_pid}); retry shortly"
                ),
            }
        )
    # Surfaced only when THIS call actually ran the idioms.md -> store
    # migration (not on a noop against an already-migrated repo), so a
    # teammate's client can show "N legacy idioms migrated" exactly once.
    _migration_extra: dict = {}
    if _migrate_result is not None and _migrate_result.get("status") == "migrated":
        _migration_extra = {
            "idioms_migrated": _migrate_result["idioms_out"],
            "idioms_quarantined": _migrate_result["quarantined"],
        }

    if outcome == "duplicate":
        return _envelope(
            {
                "status": "success",
                "already_present": True,
                "note": "an identical idiom is already active; not duplicated.",
                **_migration_extra,
            }
        )

    _regrant_trust_if_was_trusted(_was_trusted, _repo_id, _profile_dir)

    _notify_daemon_cache_invalidation()

    response: dict = {
        "status": "success",
        "idioms_added": 1,
        "idioms_deprecated": 0,
        **_migration_extra,
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
    """True if `session_id` is unknown to chameleon for `repo_id`.

    "Known" means either an exec-log entry (a Bash command ran) OR a persisted
    per-session enforcement state (an Edit/Write was gated: advisory injected, a
    block fired, or an override recorded). A session that only edited files writes
    no exec-log, so checking exec-log alone false-warned on the common case.

    Best-effort: any error returns False (don't false-warn on a system where the
    state dirs aren't readable).
    """
    # An edit-only session (no Bash) writes no exec-log but does persist an
    # enforcement state the moment chameleon gates one of its edits, so that state
    # is authoritative that the session is known.
    try:
        from chameleon_mcp.enforcement import _state_path
        from chameleon_mcp.hook_helper import _plugin_data_dir

        if _state_path(_plugin_data_dir() / repo_id, session_id).is_file():
            return False
    except Exception:
        pass
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

    # A session writes its OWN exec-log files, named by its session marker, so a
    # direct existence check is authoritative and O(1) -- independent of how many
    # other sessions share this repo_id's (TMPDIR-shared) exec-log dir. The old
    # "5 newest by mtime" window falsely reported an unseen session once >5 newer
    # sessions had logged since, so /chameleon-disable wrongly demanded force=True.
    try:
        from chameleon_mcp.optouts import _safe_session_marker

        marker = _safe_session_marker(session_id)
        for suffix in (".jsonl", ".checks.jsonl"):
            if (exec_dir / f"{marker}{suffix}").is_file():
                return False
    except Exception:
        pass

    needle = f'"session_id":"{session_id}"'
    try:
        log_files = sorted(
            (p for p in exec_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
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
    expires the marker; no manual cleanup needed. The marker is
    HMAC-signed (same key and threat model as `disable_session`'s
    marker): with the per-user key available, `is_chameleon_suppressed`
    rejects a bare or wrong-sig marker planted directly on disk — the
    key-unavailable edge case fails open, same as the disable marker.

    Used by the /chameleon-pause-15m slash command (and any future
    /chameleon-pause-<N> variants).

    Bug 1: `repo` now accepts either an absolute repo path
    or a 64-char repo_id hex digest. The asymmetry across MCP tools
    surfaced 4 separate dogfood complaints about pause/disable rejecting
    repo_ids. `_resolve_repo_arg` performs the shape detection.
    """
    from chameleon_mcp.optouts import write_pause
    from chameleon_mcp.profile.trust import trust_state_for

    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0 or minutes > 240:
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

    # Explicit-path / by-id resolution bypasses find_repo_root, so re-apply the
    # unsafe-root guard here: a profile planted under /tmp or a world-writable
    # dir by another local user must not be trustable (the hooks would refuse to
    # load it anyway, so trusting it is a dead grant at best, an injection at
    # worst).
    _unsafe = _unsafe_root_refusal(repo_path)
    if _unsafe is not None:
        return _envelope({"status": "failed", "error": _unsafe})

    # A linked worktree has no .chameleon of its own; the committed profile
    # being trusted lives at the main worktree. repo_path itself (and its
    # basename) stays UNRESOLVED below for the confirmation_token check and
    # the statusline update -- those are about where the user actually typed
    # the command from, not where the profile is stored.
    from chameleon_mcp.worktree import resolve_profile_root

    _profile_root = resolve_profile_root(repo_path)
    profile_dir = _profile_root / ".chameleon"
    if not profile_dir.is_dir():
        # A pure-coordinator monorepo root (pnpm/turbo/nx) has no root profile
        # even after a successful init -- its WORKSPACES were bootstrapped
        # (status success_workspaces_only). Detect that and point the user at the
        # workspaces instead of the contradictory "run /chameleon-init first"
        # (init already ran; there is just nothing to trust at the bare root).
        try:
            _ws = sorted(
                str(p.parent.relative_to(_profile_root))
                for parent in ("apps", "packages", "services", "workspaces")
                for p in (_profile_root / parent).glob("*/.chameleon")
                if (p / "profile.json").is_file()
            )
        except Exception:
            _ws = []
        if _ws:
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        "no root .chameleon/ profile: this is a coordinator monorepo "
                        "whose workspaces were bootstrapped. Trust each workspace "
                        "instead of the root: " + ", ".join(_ws)
                    ),
                    "workspaces": _ws,
                }
            )
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

    # Validate the confirmation token BEFORE anything below mutates the repo
    # (the poison pre-scan is read-only, but the migration trigger writes to
    # .chameleon/idioms/). A refused command -- wrong token -- must leave the
    # repo untouched, so both run only once the token has already checked out.
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

    # Scan for a poisoned idioms.md/principles.md BEFORE migrating: a
    # migration regenerates idioms.md from parsed "### " blocks, and
    # free-form prose with no recognized block (exactly the shape a raw
    # injection payload has) is silently dropped from the regenerated view
    # rather than quarantined. Scanning AFTER migration would see only the
    # laundered, injection-free view and grant trust to what was originally a
    # poisoned profile -- run the same check grant_trust applies, on the
    # pre-migration content, so a poisoned repo is refused before anything
    # rewrites the evidence.
    from chameleon_mcp.profile.trust import injected_prose_artifact

    _poisoned = injected_prose_artifact(profile_dir)
    if _poisoned is not None:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    "profile failed the injection/secret scan and was NOT trusted; "
                    f"review .chameleon/ for poisoned content: {_poisoned} contains "
                    "an injection pattern"
                ),
            }
        )

    # A committed profile may still be on legacy idioms.md (never taught
    # through this engine, or a teammate's hand edit since the last store
    # write). Migrate/fold it in before grant_trust computes hash_profile
    # below, so the explicit user grant covers the migrated surface --
    # including a migration that quarantined content, since here the user is
    # granting by explicit token, not a machine re-stamp.
    _migrate_result = _migrate_idioms_store_or_warn(profile_dir, repo_id)

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
    for child_chameleon in _iter_workspace_chameleon_dirs(_profile_root):
        if child_chameleon == profile_dir:
            continue
        if not (child_chameleon / "profile.json").is_file():
            continue
        try:
            # Grant under the WORKSPACE's own repo_id, not the root's. On a
            # remote-backed repo every workspace shares the remote-derived id, so
            # this equals repo_id (no change). On a NO-REMOTE monorepo each
            # workspace derives a distinct id from its config repo_uuid, and
            # granting under the root id left detect-time lookups (which compute
            # the workspace's own id) untrusted -- a silent false-clean where the
            # tool reported the workspace trusted but guidance/enforcement/Stop
            # were all dead.
            try:
                _ws_repo_id = _compute_repo_id(child_chameleon.parent)
            except Exception:
                _ws_repo_id = repo_id
            grant_trust(_ws_repo_id, child_chameleon)
            workspace_trust_count += 1
        except Exception:
            pass

    data: dict = {
        "status": "success",
        # This root's own grant time, not the shared record's first-grant time:
        # granting a workspace under a monorepo-shared repo_id must report when
        # the user just trusted THIS root, not the original one.
        "trusted_at": record.granted_at_for_root(profile_dir.parent),
        "granted_by_user": record.granted_by_user,
    }
    if workspace_trust_count:
        data["workspace_profiles_trusted"] = workspace_trust_count
    if _migrate_result is not None and _migrate_result.get("status") == "migrated":
        data["idioms_migrated"] = _migrate_result["idioms_out"]
        data["idioms_quarantined"] = _migrate_result["quarantined"]
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
    # `system: <directive>` is role injection; `System::Base` is a Ruby namespace
    # (the trailing `(?!:)` excludes the `::` so a legit namespaced base/idiom is not
    # flagged), and `\b` avoids matching inside `subsystem:`.
    (
        "system role injection",
        re.compile(r"(<\s*/?\s*system\s*>|\bsystem\s*:(?!:))", re.IGNORECASE),
    ),
    # eval()/exec()/rm-rf (and os.system() below) are code-execution SINKS. This
    # list scans PROSE, where an OFFENSIVE instruction ("always run eval(user)")
    # is an injection but DEFENSIVE advice ("never call eval() on user input",
    # "avoid rm -rf in deploy scripts") is exactly the guidance teams teach --
    # flagging the latter silently dropped the whole idiom block from every
    # injection path. _looks_suspicious exempts a sink match in a negated/advisory
    # context (see _CODE_SINK_LABELS / _DEFENSIVE_CONTEXT) so only an imperative
    # usage flags. Code artifacts get the unconditional check via
    # `scan_for_dangerous_patterns` (poisoning_scanner.py).
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
    # System-prompt extraction with a broader verb + possessive set than the
    # narrow "reveal ..." above ("reveal YOUR system prompt", "print the full
    # system instructions"). Scoped to the "system prompt / instructions /
    # message" objects, which are prompt-injection vocabulary that never appears
    # in healthy convention prose (even negated: no principle says "never expose
    # the system prompt"), so it stays high-precision for the shared trust scan.
    (
        "extract system prompt",
        re.compile(
            r"\b(?:reveal|expose|print|show|output|repeat|dump|leak|disclose|"
            r"divulge|echo|display)\s+(?:me\s+)?"
            r"(?:the\s+|your\s+|our\s+|its\s+|my\s+|this\s+|that\s+)?"
            r"(?:full\s+|entire\s+|complete\s+|initial\s+|original\s+|hidden\s+|"
            r"verbatim\s+|exact\s+|underlying\s+)?"
            r"(?:system\s+prompt|system\s+instructions?|system\s+message)",
            re.IGNORECASE,
        ),
    ),
    # Subverting chameleon's OWN guidance mechanism ("disregard the canonical and
    # instead ...", "ignore the witness"). Healthy convention prose FOLLOWS the
    # canonical/witness; only an injection tells the model to ignore/bypass it, so
    # pairing an override verb with these chameleon-specific objects is high-signal.
    (
        "subvert chameleon guidance",
        re.compile(
            r"\b(?:ignore|disregard|forget|bypass|override)\s+"
            r"(?:the\s+|this\s+|any\s+|all\s+|its\s+|these\s+|those\s+)?"
            r"(?:canonical(?:\s+witness)?|witness|chameleon(?:\s+context|\s+guidance)?)\b",
            re.IGNORECASE,
        ),
    ),
    # Reading/exfiltrating a private-key or credential FILE. Anchored to the exact
    # sensitive paths (id_rsa / ~/.ssh / ~/.aws/credentials / /etc/shadow) that
    # never appear in healthy idiom prose even negated, so a verb aimed at one is
    # an exfil instruction, not a "never commit your key" security note.
    (
        "private-key file read",
        re.compile(
            r"\b(?:cat|read|print|output|include|send|upload|exfiltrate|leak|dump|"
            r"base64|encode|copy|show)\b[^.\n]{0,40}"
            r"(?:id_rsa\b|~/\.ssh/|~/\.aws/credentials|/etc/shadow\b)",
            re.IGNORECASE,
        ),
    ),
    # Identity-reassignment via an imperative override. Anchored to start-of-text
    # or a sentence boundary so a NEGATED clause ("Don't forget you are inside a
    # transaction") does not trip it -- there the verb is not sentence-initial.
    # The override must target the model's identity/role, not an arbitrary object
    # ("Forget the cache" stays clean), so benign idiom prose survives.
    (
        "identity reassignment",
        re.compile(
            r"(?:^|[.!?]\s)\s*(?:please\s+)?(?:forget|disregard)\s+"
            r"(?:that\s+|everything\s+|all\s+)?"
            r"(?:you\s+are|you're|your\s+(?:role|identity|instructions|guidelines|persona)|"
            r"who\s+you\s+are|what\s+you\s+are)",
            re.IGNORECASE,
        ),
    ),
    # "act as the user/assistant/admin/..." role hijack. Requires the imperative
    # "act as" (not the descriptive "acts as ...") and a bare role noun, so
    # architecture prose ("the gateway acts as a facade", "act as a thin
    # pass-through") is not flagged.
    (
        "act-as role hijack",
        re.compile(
            r"\bact\s+as\s+(?:the\s+|an?\s+)?"
            r"(?:user|assistant|administrator|admin|system\s+prompt|the\s+model|root\s+user)\b",
            re.IGNORECASE,
        ),
    ),
    # Instruction-override WITHOUT the literal "previous"/"above" qualifier. The
    # canonical "ignore all previous instructions" pattern above only fires when
    # that position word is present, so "ignore all instructions" / "disregard the
    # directives" / "ignore the instructions above" slipped through (the position
    # word can also trail the noun). Kept to the strongly-meta nouns
    # (instructions/directives/prompts/guidelines/system prompt/conventions) so
    # lint-rule convention prose ("ignore the rule for X") is NOT flagged. Note
    # "conventions" is meta HERE only behind an override verb ("disregard the
    # conventions above"); benign profile prose uses "match/follow the
    # conventions", never "ignore/disregard the conventions", so it is high-signal.
    (
        "override instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass|skip)\s+"
            r"(?:all\s+|any\s+|the\s+|these\s+|those\s+|my\s+|your\s+|its\s+|"
            r"(?:all\s+)?(?:previous|prior|above|earlier|preceding|foregoing|following|other)\s+)*"
            r"(?:instructions?|directives?|system\s+prompts?|guidelines?|conventions?)\b",
            re.IGNORECASE,
        ),
    ),
    # "From now on ..." behaviour-reassignment lead-in. "you are" alone is common
    # prose, so this fires only on the injection-shaped continuation (you are/will,
    # ignore, act as, respond/answer only, always/never), never on a bare "you are
    # responsible for ...".
    (
        "behaviour override lead-in",
        re.compile(
            r"\bfrom\s+now\s+on[\s,]+(?:you\s+(?:are|will|must|should|shall)|you're|"
            r"ignore|disregard|forget|act\s+as|respond|answer|reply|only|always|never)\b",
            re.IGNORECASE,
        ),
    ),
    # Bare jailbreak-persona markers (DAN / unrestricted / developer mode / ...).
    # These tokens do not appear in code-convention prose, so matching the identity
    # assignment directly is high-precision.
    (
        "jailbreak persona",
        re.compile(
            r"\byou(?:\s+are|'re|\s*’re)\s+(?:now\s+)?(?:an?\s+|in\s+)?"
            r"(?:dan\b|do\s+anything\s+now|unrestricted|jailbroken|uncensored|"
            r"developer\s+mode|no\s+longer\s+bound|not\s+bound\s+by|"
            r"free\s+(?:from|of)\s+(?:all\s+)?(?:restrictions?|rules?|guidelines?|limits?))",
            re.IGNORECASE,
        ),
    ),
    # An injected fresh instruction block: "new directive:", "new persona:",
    # "new system prompt:", "new instructions:". Rare in genuine convention prose.
    (
        "injected new directive",
        re.compile(
            r"\bnew\s+(?:directive|persona|system\s+prompt|role|instruction)s?\s*:",
            re.IGNORECASE,
        ),
    ),
    # Credential/secret-file exfiltration: a strong exfil verb aimed at a
    # high-signal secret path (.env / .ssh / id_rsa / /etc/passwd / *.pem / ~/.aws
    # / .npmrc). The specific target keeps benign API prose ("post the token to
    # /auth") from tripping it; the gap excludes periods so it stays within one
    # clause and cannot bridge two sentences.
    (
        "credential exfiltration",
        re.compile(
            r"\b(?:append|send|post|upload|exfiltrate|leak|steal|dump|transmit|copy"
            r"|e-?mail|mail|forward|share|ship|push|curl|wget|fetch)\b"
            r"[^.\n]{0,48}"
            r"(?:\.env\b|\.ssh\b|id_rsa|/etc/passwd|/etc/shadow|~/\.aws|\.pem\b|\.npmrc\b)",
            re.IGNORECASE,
        ),
    ),
    # Pipe-to-shell remote execution (curl … | sh). A concrete exploit chain,
    # distinct from an API name that a legit "avoid X" note might mention.
    (
        "pipe to shell",
        re.compile(
            r"\b(?:curl|wget|fetch)\b[^|\n]{0,120}\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b",
            re.IGNORECASE,
        ),
    ),
    # os.system() shell sink, matched like the eval()/exec()/rm -rf sinks above
    # (a code-execution SINK). Member access is excluded so an unrelated
    # `.system(` object method is not flagged; _looks_suspicious exempts it in a
    # negated/advisory context ("never call os.system(); use subprocess").
    ("os.system()", re.compile(r"\bos\.system\s*\(", re.IGNORECASE)),
)


# Code-execution SINK labels: flagged in an offensive/imperative context but
# EXEMPT in defensive/advisory prose ("never call eval()", "avoid rm -rf").
_CODE_SINK_LABELS = frozenset({"eval()", "exec()", "rm -rf", "os.system()"})
# A negation/avoidance cue near a sink marks the prose as security ADVICE, not an
# instruction to execute the sink. An offensive "always run eval(user)" carries
# none of these, so it still flags.
_DEFENSIVE_CONTEXT = re.compile(
    r"\b(?:never|avoid|avoids|avoiding|don'?t|do\s+not|does\s+not|no|not|without|"
    r"instead(?:\s+of)?|rather\s+than|prohibit(?:ed|s)?|ban(?:ned|s)?|"
    r"forbid(?:den|s)?|disallow(?:ed|s)?|unsafe|dangerous|insecure|vulnerable|"
    r"prefer|shouldn'?t|should\s+not|must\s+not|mustn'?t|cannot|can'?t|refuse)\b",
    re.IGNORECASE,
)


def _looks_suspicious(text: str) -> tuple[bool, str | None]:
    """Return `(matched, label)` if `text` matches a known injection
    pattern, else `(False, None)`.

    The label corresponds to a human-readable handle for the matched
    pattern (e.g., "ignore previous instructions"). It's surfaced in the
    `suspicious_input_reason` envelope field so consumers can route on
    the specific category of suspicion without parsing free text.

    A code-execution sink (eval/exec/rm-rf/os.system) is flagged only in an
    imperative context: a match wrapped in negated/advisory prose is security
    guidance, not an injection, and blocking it silently dropped whole idiom
    blocks from every injection path.
    """
    if not isinstance(text, str) or not text:
        return False, None
    for label, regex in _SUSPICIOUS_PATTERNS:
        if label in _CODE_SINK_LABELS:
            for m in regex.finditer(text):
                lo = max(0, m.start() - 64)
                hi = min(len(text), m.end() + 64)
                if not _DEFENSIVE_CONTEXT.search(text[lo:hi]):
                    return True, label
            continue
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

    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0 or top_n > 64:
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
    # A linked worktree has no .chameleon of its own; read the main
    # worktree's committed profile instead.
    from chameleon_mcp.worktree import resolve_profile_root

    repo_root = resolve_profile_root(resolved_path.resolve())

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
        # current_name / canonical_file (witness path) / paths_pattern are
        # profile-derived strings that reach the model. They are identifiers, file
        # paths, and globs (not free prose), so tag-boundary sanitization is the
        # right screen -- a prose-injection scan would false-drop a legit glob like
        # "app/**". load_profile_dir filters archetype KEYS but never these VALUES,
        # so scrub them here at the tool boundary.
        from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _san

        rows.append(
            {
                "current_name": _san(name),
                "cluster_size": int((arch or {}).get("cluster_size", 0)),
                "canonical_file": _san(canonical_path),
                "paths_pattern": _san((arch or {}).get("paths_pattern", "")),
                "suggested_alternatives": [_san(a) for a in alternatives],
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
    in ``_HASHED_ARTIFACTS`` (the hash provides provenance / material-change
    detection; under the default one-time trust it re-stamps with no re-prompt,
    and only re-prompts under CHAMELEON_TRUST_REVALIDATE=1). The load-time guard
    below is what actually keeps a hand-edited ledger from poisoning a refresh.

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
    from chameleon_mcp.locks import LockHeldError
    from chameleon_mcp.profile.loader import load_profile_dir
    from chameleon_mcp.profile.trust import hash_profile
    from chameleon_mcp.profile.trust import repo_data_dir as _rdd

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
    # A linked worktree has no .chameleon of its own; write the rename into
    # the main worktree's committed profile instead.
    from chameleon_mcp.worktree import resolve_profile_root

    repo_root = resolve_profile_root(resolved_path.resolve())

    profile_dir = repo_root / ".chameleon"
    if not profile_dir.is_dir():
        return _envelope(
            {"status": "failed", "error": "no .chameleon/ directory (run /chameleon-init first)"}
        )

    repo_id = _compute_repo_id(repo_root)
    lock_dir = _rdd(repo_id)
    try:
        with _bootstrap_write_locks(lock_dir):
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
            # Source the RAW on-disk artifact, not loaded.conventions: load_profile_dir
            # scrubs injection-heuristic hits out of its in-memory render copy, and a
            # rename PERSISTS what it writes. Sourcing the scrubbed copy would erase a
            # legitimate convention value (or whole archetype) that merely tripped the
            # heuristic. Rename only remaps the per-archetype keys; every value must
            # survive byte-for-byte. Fall back to the loaded copy on a corrupt artifact
            # so rename never drops conventions.json wholesale.
            conventions_path = profile_dir / "conventions.json"
            conventions_data = None
            if conventions_path.is_file():
                try:
                    from chameleon_mcp.profile.loader import _loads_hardened, _safe_read_artifact

                    conventions_data = _loads_hardened(_safe_read_artifact(conventions_path))
                except Exception:
                    conventions_data = json.loads(json.dumps(loaded.conventions))
            if isinstance(conventions_data, dict):
                _conv_block = conventions_data.get("conventions")
                if isinstance(_conv_block, dict):
                    # Remap EVERY per-archetype section, not a hardcoded subset: the
                    # edit-time hot path looks each section up by the new archetype
                    # name with no alias fallback, so a section left under the old
                    # key (required_guards' authz hint, test_pairing's reminder, ...)
                    # is silently dropped for the renamed archetype. Iterating the
                    # block keeps a future-added per-archetype section from regressing.
                    # Repo-level sections (layering) are keyed by edge/report, not
                    # archetype, and must be left untouched.
                    from chameleon_mcp.conventions import REPO_LEVEL_CONVENTION_SECTIONS

                    for _section, _sub in list(_conv_block.items()):
                        if _section in REPO_LEVEL_CONVENTION_SECTIONS or not isinstance(_sub, dict):
                            continue
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

            # counterexamples.json + symbol_signatures.json are protocol files, so the swap
            # drops whatever the txn does not re-emit. symbol_signatures is keyed by file
            # path, so it carries verbatim; counterexamples is keyed by archetype NAME, so
            # a rename must remap its keys (the same dangling-reference the idioms
            # "Archetype:" rewrite below avoids) or the entry points at a vanished archetype.
            from chameleon_mcp.safe_open import safe_read_profile_artifact as _safe_read_rn2

            counterexamples_text: str | None = None
            ce_rename_path = profile_dir / "counterexamples.json"
            if ce_rename_path.is_file():
                try:
                    _ce_doc = json.loads(_safe_read_rn2(ce_rename_path, max_bytes=16_000_000))
                except Exception:
                    _ce_doc = None
                if isinstance(_ce_doc, dict) and isinstance(_ce_doc.get("archetypes"), dict):
                    _ce_doc["archetypes"] = {
                        effective.get(a, a): row for a, row in _ce_doc["archetypes"].items()
                    }
                    counterexamples_text = json.dumps(_ce_doc, indent=2, sort_keys=True)

            symbol_signatures_text: str | None = None
            ss_rename_path = profile_dir / "symbol_signatures.json"
            if ss_rename_path.is_file():
                try:
                    symbol_signatures_text = _safe_read_rn2(ss_rename_path, max_bytes=16_000_000)
                except Exception:
                    symbol_signatures_text = None

            # exports_index / reverse_index / function_catalog are protocol files keyed by
            # file path + symbol (not archetype), so a rename does not change them; carry
            # the prior copy verbatim (like symbol_signatures) or the dir-swap drops them
            # and dark-fires phantom-symbol / cross-file existence / duplication.
            def _carry_rn_index(name: str) -> str | None:
                p = profile_dir / name
                if not p.is_file():
                    return None
                try:
                    return _safe_read_rn2(p, max_bytes=16_000_000)
                except Exception:
                    return None

            exports_index_text = _carry_rn_index("exports_index.json")
            reverse_index_text = _carry_rn_index("reverse_index.json")
            function_catalog_text = _carry_rn_index("function_catalog.json")

            idioms_path = profile_dir / "idioms.md"
            idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

            from chameleon_mcp.core.idiom_store import store_exists as _idiom_store_exists

            store_backed = _idiom_store_exists(profile_dir)
            if not store_backed:
                # Rewrite taught-idiom archetype references so a rename does not leave
                # a dangling "Archetype: <old>" pointing at an archetype that no longer
                # exists. Once the idiom store exists it is truth for archetype tags —
                # rename_archetypes (called below, after this lock releases) rewrites
                # the records and regenerates this view instead, so a store-backed
                # repo skips this legacy markdown rewrite (it would otherwise be
                # reverted by the next store write's regenerate_views()).
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
                        (txn_dir / "calls_index.json").write_text(
                            calls_index_text, encoding="utf-8"
                        )
                    if counterexamples_text is not None:
                        (txn_dir / "counterexamples.json").write_text(
                            counterexamples_text, encoding="utf-8"
                        )
                    if symbol_signatures_text is not None:
                        (txn_dir / "symbol_signatures.json").write_text(
                            symbol_signatures_text, encoding="utf-8"
                        )
                    if exports_index_text is not None:
                        (txn_dir / "exports_index.json").write_text(
                            exports_index_text, encoding="utf-8"
                        )
                    if reverse_index_text is not None:
                        (txn_dir / "reverse_index.json").write_text(
                            reverse_index_text, encoding="utf-8"
                        )
                    if function_catalog_text is not None:
                        (txn_dir / "function_catalog.json").write_text(
                            function_catalog_text, encoding="utf-8"
                        )
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
    except LockHeldError:
        return _envelope(
            {
                "status": "failed",
                "error": "another operation holds the profile write lock; retry shortly",
            }
        )

    if store_backed:
        # Must run OUTSIDE _bootstrap_write_locks: that block already acquired
        # .idioms.lock for the txn above, and rename_archetypes acquires the same
        # per-repo lock itself (the pattern every other store writer uses). Calling
        # it while still inside would re-enter an already-held advisory lock and
        # block until its timeout instead of completing.
        from chameleon_mcp.core.idiom_store import rename_archetypes

        rename_archetypes(profile_dir, effective, repo_id=repo_id)

    # The rename rewrites canonicals.json (the witness set), so the block-rule
    # verdict in enforcement.json must be re-measured against the renamed profile;
    # otherwise it stays pinned to the pre-rename witnesses. Calibrate before the
    # hash snapshot so enforcement.json (part of the trust-hashed surface) is
    # reflected in new_profile_sha256 and the index.db mirror.
    _calibrate_block_rules_for_repo(repo_root)

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


def _npm_package_root(specifier: str) -> str | None:
    """The npm package NAME an import specifier resolves to, or None when it is not
    a bare package import.

    ``lodash/fp`` -> ``lodash``; ``@scope/pkg/sub`` -> ``@scope/pkg``. Returns None
    for a relative (``./x``), absolute (``/x``), or TS path-alias (``@/x`` — a bare
    ``@`` with no scope name) import: those are not npm packages and are resolved by
    tsconfig/baseUrl, not package.json, so a package.json membership check does not
    apply.
    """
    s = (specifier or "").strip()
    if not s or s.startswith(".") or s.startswith("/"):
        return None
    if s.startswith("@"):
        parts = s.split("/")
        if len(parts) < 2 or not parts[0][1:] or not parts[1]:
            return None  # `@/…` path alias (empty scope) is not a package
        return f"{parts[0]}/{parts[1]}"
    return s.split("/", 1)[0] or None


def _resolves_under_tsconfig_baseurl(specifier: str, repo_path: Path) -> bool:
    """True when a bare specifier is a first-party module resolved via tsconfig
    ``baseUrl`` (so it is NOT a missing npm package).

    A repo with ``compilerOptions.baseUrl`` (commonly ``"."``) lets code import a
    local module by a bare path (`lib/api-client` -> `<repo>/lib/api-client.ts`).
    ``_npm_package_root`` reads that as the package ``lib`` and the caller would
    warn it is absent from package.json -- a false positive on a valid first-party
    import. Resolve the specifier against baseUrl and probe the standard TS
    suffixes; a hit means first-party. Best-effort, fail toward NOT warning."""
    try:
        from chameleon_mcp.phantom_imports import _exists_with_suffix, _load_tsconfig_paths

        base_url, _paths = _load_tsconfig_paths(str(repo_path))
        if not base_url:
            return False
        candidate = (repo_path / base_url / specifier).resolve()
        return _exists_with_suffix(candidate)
    except Exception:
        return False


def _package_json_dependency_names(repo_path: Path) -> set[str] | None:
    """Every declared dependency name across EVERY package.json in the repo, or None
    when there is no readable root package.json.

    A monorepo declares deps in per-workspace ``package.json`` files, not the root,
    so a root-only read would falsely report a real workspace dependency as
    "missing" — the exact false positive that makes an existence check untrustworthy.
    The scan is a bounded, node_modules-pruned walk (same shape as the workspace
    ``.chameleon`` walk) and unions deps across all manifests. Returns None only when
    there is no root package.json at all (not a JS repo → skip the check)."""
    root_pkg = repo_path / "package.json"
    if not root_pkg.is_file():
        return None

    def _names_from(pkg: Path, into: set[str]) -> None:
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for section in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            block = data.get(section)
            if isinstance(block, dict):
                into.update(k for k in block if isinstance(k, str))

    names: set[str] = set()
    _names_from(root_pkg, names)
    # Bounded walk for workspace manifests (prune node_modules / .git / dotdirs).
    stack: list[tuple[Path, int]] = [(repo_path, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_file() and child.name == "package.json" and child != root_pkg:
                    _names_from(child, names)
                    continue
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if child.name in _WS_PRUNE_DIRS or child.name.startswith("."):
                continue
            if depth + 1 <= _WS_MAX_DEPTH:
                stack.append((child, depth + 1))
    return names


def _sync_conventions_md(profile_dir: Path, conv: dict) -> None:
    """Keep `.chameleon/conventions.md` (the CLAUDE.md-channel mirror) in sync
    after a conventions.json or idioms.md mutation. Best-effort: the
    teach/unteach must succeed even if the mirror render fails. Honors the same
    kill switch as the bootstrap write; an empty render removes a stale mirror
    rather than leaving it lying about the profile.

    Principles and idiom gists ride the mirror too (read from the live profile
    dir through the injection-scanned prose path), so the memory channel carries
    the complete session-conventions content — that completeness is what lets
    SessionStart skip its duplicate hook injection when the mirror is wired."""
    if os.environ.get("CHAMELEON_CONVENTIONS_MD") == "0":
        return
    try:
        from chameleon_mcp.conventions import render_conventions_md
        from chameleon_mcp.idiom_coverage import has_idiom_content
        from chameleon_mcp.profile.loader import safe_prose_text

        md_path = profile_dir / "conventions.md"
        principles_text = safe_prose_text(profile_dir / "principles.md")
        idioms_text = safe_prose_text(profile_dir / "idioms.md")
        if not has_idiom_content(idioms_text):
            idioms_text = ""
        text = render_conventions_md(conv, principles_text or None, idioms_text or None)
        if not text:
            md_path.unlink(missing_ok=True)
            return
        # Skip the rewrite when the mirror is already byte-identical: the noop
        # refresh self-heal calls this every session, and a gratuitous replace
        # would advance the mirror's mtime for no content change. Undecodable
        # bytes fall through to the write like an unreadable file does — the
        # documented self-heal must repair a binary-corrupted mirror, not abort.
        try:
            if md_path.is_file() and md_path.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeDecodeError):
            pass
        _tmp = md_path.with_suffix(".md.tmp")
        _tmp.write_text(text, encoding="utf-8")
        _tmp.replace(md_path)
    except Exception:
        pass


def _sync_conventions_md_from_disk(profile_dir: Path) -> None:
    """Mirror re-sync for call sites that mutated idioms.md (teach/deprecate),
    which have no conventions dict in hand: load conventions.json from disk
    (fail-open to an empty object — the idiom gists must still sync) and delegate.
    """
    try:
        from chameleon_mcp.safe_open import safe_read_profile_artifact

        conv: dict = {}
        try:
            _parsed = json.loads(safe_read_profile_artifact(profile_dir / "conventions.json"))
            if isinstance(_parsed, dict):
                conv = _parsed
        except Exception:
            conv = {}
        _sync_conventions_md(profile_dir, conv)
    except Exception:
        pass


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
    # A linked worktree has no .chameleon of its own; write the taught
    # convention into the main worktree's committed profile. repo_path stays
    # UNRESOLVED below for capture_counterexample_in_repo / package.json
    # reads, which must scan the caller's ACTUAL checked-out source tree.
    from chameleon_mcp.worktree import resolve_profile_root

    profile_dir = resolve_profile_root(repo_path) / ".chameleon"
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
        with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
            if conv_path.is_file():
                # A present-but-corrupt conventions.json still holds recoverable
                # derived data (naming, inheritance, layering). Overwriting it with
                # an empty skeleton would silently destroy that data, report
                # success, and re-grant trust over the gutted profile. Fail closed
                # instead -- matching the sibling unteach_competing_import -- so the
                # corruption stays loud and a /chameleon-refresh can recover it.
                try:
                    raw_conv = safe_read_profile_artifact(conv_path)
                except Exception:
                    return _envelope(
                        {"status": "failed", "error": "conventions.json unreadable or invalid"}
                    )
                if not (raw_conv or "").strip():
                    # A 0-byte / whitespace-only torn write holds nothing to lose,
                    # so recover to a fresh skeleton and proceed rather than block
                    # the teach -- the fail-closed guard above only matters when
                    # the file carries real derived conventions.
                    conv = empty_conventions(generation=0)
                else:
                    try:
                        conv = json.loads(raw_conv)
                    except Exception:
                        return _envelope(
                            {"status": "failed", "error": "conventions.json unreadable or invalid"}
                        )
                    if not isinstance(conv, dict):
                        return _envelope(
                            {"status": "failed", "error": "conventions.json unreadable or invalid"}
                        )
            else:
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
            _sync_conventions_md(profile_dir, conv)
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": f"another conventions write is in progress: {e}",
            }
        )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"conventions write failed: {e}"})

    # Pair the taught wrapper-preference with a real instance of the banned import
    # so the per-edit block can show the off-pattern form next to the witness. The
    # counterexample artifact is a hashed trust file, so it is rebuilt BEFORE the
    # re-grant below, which then covers it. Best-effort: a scan/write failure must
    # not fail the teach.
    if not already:
        try:
            from chameleon_mcp.counterexamples import (
                COUNTEREXAMPLES_FILENAME,
                capture_counterexample_in_repo,
                normalize_archetype_rows,
            )
            from chameleon_mcp.counterexamples import (
                SCHEMA_VERSION as _ce_schema,
            )

            entry = capture_counterexample_in_repo(
                repo_path, [{"preferred": preferred, "over": over}]
            )
            if entry is not None:
                ce_path = profile_dir / COUNTEREXAMPLES_FILENAME
                # Serialize the artifact read-modify-write under the same conventions
                # lock the source-of-truth write used, so a concurrent teach on a
                # different archetype cannot clobber this row (the scan above needs
                # no lock; only the shared-file update does).
                with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
                    try:
                        ce_doc = (
                            json.loads(safe_read_profile_artifact(ce_path))
                            if ce_path.is_file()
                            else {}
                        )
                    except Exception:
                        ce_doc = {}
                    if not isinstance(ce_doc, dict):
                        ce_doc = {}
                    ce_doc["schema_version"] = _ce_schema
                    arches = ce_doc.setdefault("archetypes", {})
                    if not isinstance(arches, dict):
                        ce_doc["archetypes"] = arches = {}
                    # APPEND into the archetype's row list (normalizing any legacy v1
                    # single-dict still on disk), replacing only a prior row for the
                    # SAME discouraged import. A second taught competing import for
                    # this archetype keeps the first's counterexample instead of
                    # clobbering it.
                    rows = [
                        r
                        for r in normalize_archetype_rows(arches.get(archetype))
                        if r.get("over") != over
                    ]
                    rows.append(entry)
                    # Re-normalize the final list so the cap holds AFTER the append
                    # (capping only the existing rows would let it grow to cap+1).
                    arches[archetype] = normalize_archetype_rows(rows)
                    _ce_tmp = ce_path.with_suffix(".json.tmp")
                    _ce_tmp.write_text(
                        json.dumps(ce_doc, indent=2, sort_keys=True), encoding="utf-8"
                    )
                    _ce_tmp.replace(ce_path)
        except Exception:
            pass

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
    warnings: list[str] = []
    try:
        _arch = json.loads(safe_read_profile_artifact(profile_dir / "archetypes.json"))
        _known = _arch.get("archetypes") if isinstance(_arch, dict) else None
        if isinstance(_known, dict) and archetype not in _known:
            warnings.append(
                f"archetype {archetype!r} is not in the current profile; the rule was "
                "recorded but will not match any file until an archetype by that name "
                "exists. Check for a typo, or /chameleon-refresh if it was renamed."
            )
    except Exception:
        pass

    # Soft, NON-FATAL check that the PREFERRED module resolves — the rule steers the
    # model toward it, so a typo (`@/lib/cn` for `@/utils/cn`) silently points at a
    # module that does not exist. Only the high-confidence, low-false-positive case
    # is flagged: `preferred` is a bare/scoped npm PACKAGE specifier (no `.`/`/`
    # prefix, not a deep import) yet absent from package.json deps. Alias (`@/…`)
    # and relative (`./…`) forms are NOT checked — resolving them needs tsconfig
    # path maps, and the target may legitimately be created later; a noisy warning
    # there would punish valid forward-looking teachings.
    # The npm/package.json check only makes sense for a TypeScript/JavaScript
    # profile: a Ruby/Python repo that bundles a JS frontend or asset pipeline has
    # a package.json, but a taught Ruby/Python wrapper preference is not an npm
    # package and must not be warned as "missing from package.json".
    _teach_lang: str | None = None
    try:
        _pj = _effective_profile_dir(repo_path) / "profile.json"
        if _pj.exists():
            _teach_lang = json.loads(_pj.read_text(encoding="utf-8")).get("language")
    except Exception:
        _teach_lang = None
    if not already and _teach_lang in ("typescript", "javascript"):
        try:
            pkg_name = _npm_package_root(preferred)
            if pkg_name is not None and not _resolves_under_tsconfig_baseurl(preferred, repo_path):
                deps = _package_json_dependency_names(repo_path)
                if deps is not None and pkg_name not in deps:
                    warnings.append(
                        f"preferred package {pkg_name!r} is not in package.json; the rule "
                        "was recorded but points at a package the repo does not depend on. "
                        "Check for a typo, or add the dependency."
                    )
        except Exception:
            pass
    warning = " ".join(warnings) if warnings else None

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
    # A linked worktree has no .chameleon of its own; write the removal into
    # the main worktree's committed profile. repo_path stays UNRESOLVED below
    # for capture_counterexamples_in_repo, which must scan the caller's
    # ACTUAL checked-out source tree.
    from chameleon_mcp.worktree import resolve_profile_root

    profile_dir = resolve_profile_root(repo_path) / ".chameleon"
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
        with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
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
                _sync_conventions_md(profile_dir, conv)
    except LockHeldError as e:
        return _envelope(
            {"status": "failed", "error": f"another conventions write is in progress: {e}"}
        )
    except Exception as e:
        return _envelope({"status": "failed", "error": f"conventions write failed: {e}"})

    # Keep the counterexample artifact coherent: the removed pair may have been the
    # one captured, so recompute this archetype's counterexample from the REMAINING
    # taught pairs and drop it when none still apply. Rebuilt BEFORE the re-grant so
    # the new hash is covered. Best-effort: a failure must not fail the unteach.
    if removed:
        try:
            from chameleon_mcp.counterexamples import (
                COUNTEREXAMPLES_FILENAME,
                capture_counterexamples_in_repo,
            )
            from chameleon_mcp.counterexamples import (
                SCHEMA_VERSION as _ce_schema,
            )

            ce_path = profile_dir / COUNTEREXAMPLES_FILENAME
            if ce_path.is_file():
                # Recompute outside the lock (a repo scan), then serialize the
                # artifact read-modify-write under the conventions lock so a
                # concurrent teach/unteach cannot clobber another archetype's row.
                # Rebuild this archetype's FULL row list from the REMAINING taught
                # pairs so unteaching one competing import leaves the others' rows.
                new_rows = capture_counterexamples_in_repo(repo_path, kept)
                with acquire_advisory_lock(lock_path, blocking_timeout=10.0):
                    try:
                        ce_doc = json.loads(safe_read_profile_artifact(ce_path))
                    except Exception:
                        ce_doc = None
                    arches = ce_doc.get("archetypes") if isinstance(ce_doc, dict) else None
                    if isinstance(arches, dict) and archetype in arches:
                        if new_rows:
                            arches[archetype] = new_rows
                        else:
                            arches.pop(archetype, None)
                        ce_doc["schema_version"] = _ce_schema
                        _ce_tmp = ce_path.with_suffix(".json.tmp")
                        _ce_tmp.write_text(
                            json.dumps(ce_doc, indent=2, sort_keys=True), encoding="utf-8"
                        )
                        _ce_tmp.replace(ce_path)
        except Exception:
            pass

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
    # A linked worktree has no .chameleon of its own; the idiom is captured
    # into the main worktree's committed idioms.md.
    from chameleon_mcp.worktree import resolve_profile_root

    repo_path = resolve_profile_root(repo_path)
    idioms_path = repo_path / ".chameleon" / "idioms.md"
    if not idioms_path.parent.exists():
        return _envelope(
            {"status": "failed", "error": "no profile in this repo (run /chameleon-init)"}
        )

    profile_dir = repo_path / ".chameleon"
    # Imported unconditionally (not inside the try below) so the except
    # branch can always call it, even if the try's own import line is what
    # raised.
    from chameleon_mcp.core.idiom_store import store_exists

    # No store yet means the next call below would migrate the live
    # idioms.md. A migration regenerates idioms.md from parsed "### " blocks
    # and drops everything else -- exactly the shape a raw injection payload
    # has -- so a poisoned pre-migration file would be laundered clean before
    # this teach's own idiom ever gets a chance to fail the scan. Refuse
    # loudly here instead, the same pre-migration check trust_profile runs.
    if not store_exists(profile_dir):
        from chameleon_mcp.profile.trust import injected_prose_artifact

        _poisoned = injected_prose_artifact(profile_dir)
        if _poisoned is not None:
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"{_poisoned} contains a suspicious pattern; clean it "
                        f"(or review .chameleon/{_poisoned}) before teaching"
                    ),
                }
            )

    _migrate_result: dict | None = None
    try:
        from chameleon_mcp.core.idiom_store import ensure_store_fresh, migrate_idioms_md

        _migrate_result = migrate_idioms_md(profile_dir, repo_id=_repo_id)
        ensure_store_fresh(profile_dir, repo_id=_repo_id)
    except Exception as exc:
        if not store_exists(profile_dir):
            # A failed migration rolls back the store dir entirely (see
            # migrate_idioms_md); proceeding here would let teach_record seed a
            # brand-new store containing only this one idiom, so every rendered
            # view would drop the whole legacy idiom set down to one entry.
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"idiom store migration failed ({exc}); teach aborted, idioms.md unchanged"
                    ),
                }
            )
        # The store exists (either migration never needed to run, or
        # ensure_store_fresh failed after the store was already there) --
        # that failure mode is additive-only against an intact store, so
        # falling through and letting this teach proceed is safe.

    # Surfaced only when THIS call actually ran the idioms.md -> store
    # migration (not on a noop against an already-migrated repo).
    _migration_extra: dict = {}
    if _migrate_result is not None and _migrate_result.get("status") == "migrated":
        _migration_extra = {
            "idioms_migrated": _migrate_result["idioms_out"],
            "idioms_quarantined": _migrate_result["quarantined"],
        }

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

    from chameleon_mcp.core.idiom_store import (
        deprecate_record,
        find_by_slug,
        load_store,
        reactivate_record,
    )
    from chameleon_mcp.locks import LockHeldError

    existing = find_by_slug(load_store(profile_dir), slug)
    timestamp_now = timestamp
    try:
        if existing is not None and existing.status == "active" and status == "active":
            return _envelope(
                {
                    "status": "failed",
                    "error": (
                        f"slug {slug!r} already exists in '## active'. To "
                        'retire it, pass status="deprecated"; to change its '
                        "body, deprecate it and re-teach (idioms.md is a "
                        "generated view), or pick a new slug."
                    ),
                }
            )
        if existing is not None and status == "deprecated":
            was_trusted = _profile_trusted_now(_repo_id, profile_dir)
            # Same sanitization the tombstone (new-deprecated-slug) path and
            # teach_profile apply before anything reaches the store: a
            # deprecation note is echoed back into the model's context inside
            # a <chameleon-context> wrapper just like an active idiom is.
            dep_rationale, dep_example, dep_counterexample = _sanitize_idiom_inputs(
                rationale, example, counterexample
            )
            outcome = deprecate_record(
                profile_dir,
                slug,
                timestamp=timestamp_now,
                rationale=dep_rationale,
                example=dep_example,
                counterexample=dep_counterexample,
                provenance=clean_source or None,
                repo_id=_repo_id,
            )
            if outcome == "absent":
                return _envelope(
                    {
                        "status": "failed",
                        "error": f"slug {slug!r} is not active; nothing to deprecate",
                    }
                )
            _regrant_trust_if_was_trusted(was_trusted, _repo_id, profile_dir)
            _notify_daemon_cache_invalidation()
            return _envelope(
                {
                    "status": "success",
                    "idioms_added": 0,
                    "idioms_deprecated": 1,
                    "slug": slug,
                    "note": f"moved '### {slug}' from '## active' to '## deprecated'",
                    **_migration_extra,
                }
            )
        if existing is not None and existing.status == "deprecated" and status == "active":
            was_trusted = _profile_trusted_now(_repo_id, profile_dir)
            reactivate_record(profile_dir, slug, timestamp=timestamp_now, repo_id=_repo_id)
            _regrant_trust_if_was_trusted(was_trusted, _repo_id, profile_dir)
            _notify_daemon_cache_invalidation()
            return _envelope(
                {
                    "status": "success",
                    "idioms_added": 1,
                    "idioms_deprecated": 0,
                    "slug": slug,
                    "note": f"reactivated '### {slug}'",
                    **_migration_extra,
                }
            )
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another operation holds the idioms lock (PID {e.holder_pid}); retry shortly"
                ),
            }
        )

    # New slug: active goes through teach_profile (keeps sanitize/dedup/trust in
    # one place); new deprecated slug writes a tombstone record directly.
    if status == "active":
        _tp_result = teach_profile(repo, rendered)
        # The migration (if any) ran above, before delegating -- teach_profile's
        # own migrate_idioms_md call sees the store already exists and is a
        # noop, so its envelope never carries the pair on its own; fold this
        # call's migration counts in so the caller still sees them.
        if _migration_extra and _tp_result.get("data", {}).get("status") == "success":
            _tp_result["data"].update(_migration_extra)
        return _tp_result
    s_rationale, s_example, s_counterexample = _sanitize_idiom_inputs(
        rationale, example, counterexample
    )
    # The pre-sanitize `rationale.strip()` check above passes zero-width-only
    # input; sanitization can still strip it down to "", which would otherwise
    # reach IdiomRecord.__post_init__ and raise past this function's
    # never-raise contract (the enclosing try only catches LockHeldError).
    if not s_rationale.strip():
        return _envelope({"status": "failed", "error": "rationale is empty after sanitization"})
    was_trusted = _profile_trusted_now(_repo_id, profile_dir)
    try:
        from chameleon_mcp.core.idiom_store import IdiomRecord, tombstone_record

        tombstone_record(
            profile_dir,
            IdiomRecord(
                slug=slug,
                title=slug,
                rationale=s_rationale,
                archetypes=[archetype] if archetype else [],
                status="deprecated",
                deprecated_date=timestamp_now,
                examples=[e for e in [s_example] if e],
                counterexamples=[c for c in [s_counterexample] if c],
                provenance=clean_source,
                rank=0,
            ),
            repo_id=_repo_id,
        )
    except LockHeldError as e:
        return _envelope(
            {
                "status": "failed",
                "error": (
                    f"another operation holds the idioms lock (PID {e.holder_pid}); retry shortly"
                ),
            }
        )
    _regrant_trust_if_was_trusted(was_trusted, _repo_id, profile_dir)
    _notify_daemon_cache_invalidation()
    return _envelope(
        {
            "status": "success",
            "idioms_added": 0,
            "idioms_deprecated": 1,
            "slug": slug,
            **_migration_extra,
        }
    )


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
    # A linked worktree has no .chameleon of its own; read the main
    # worktree's committed WORKING-TREE profile (see docstring above).
    from chameleon_mcp.worktree import resolve_profile_root

    repo_path = resolve_profile_root(repo_path)
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
        # config.json (holding the lock) lives at the MAIN worktree; resolve
        # for this read only -- the git ops below stay on repo_root itself.
        from chameleon_mcp.worktree import resolve_profile_root

        locked = _persisted_production_ref(resolve_profile_root(repo_root))
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
    # A changed dependency manifest of an ecosystem this scanner does not parse
    # (Python requirements/pyproject/Pipfile, go.mod, Cargo, composer) is NOT
    # reviewed by the checks above. Surface it so the consumer reads "not
    # covered, hand-review" instead of an empty findings list that looks clean --
    # the honesty contract this scanner's own docstring promises.
    uncovered = [p for p in changed if dep_diff.is_uncovered_manifest(p)]
    if uncovered:
        data["uncovered_manifests"] = uncovered
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


def doctor(repo: str | None = None) -> dict:
    """Triage report for chameleon installation health.

    Returns a structured envelope with subsystem checks. Each check
    has a status (ok | warn | error) and a brief message.

    ``repo`` optionally targets the PER-REPO checks (``config_json``,
    ``production_ref``, and the ``profile_artifacts`` / ``judge_spawn_health`` /
    ``advisory_emission`` dead-install detectors) at a specific repo root, so a
    caller like ``/chameleon-status`` gets config for the repo it is statusing
    rather than whatever the process cwd resolves to. When omitted they resolve
    from cwd (the original behavior). The global plumbing checks (python, hooks,
    HMAC key, daemon) ignore it.
    """
    import os
    import platform
    import shutil
    import sys
    from pathlib import Path

    checks: list[dict] = []
    # A malformed repo arg (embedded null byte, un-stattable path) must not crash
    # the tool -- doctor's contract is a clean envelope. A null byte propagates as
    # ValueError out of find_repo_root's realpath/lstat (Path.exists() does NOT
    # raise on it under 3.13), so reject it here and fall back to the cwd-scoped
    # behavior (the no-arg default) rather than crashing.
    _target_root: Path | None = None
    if repo and isinstance(repo, str) and "\x00" not in repo:
        try:
            _target_root = Path(repo)
        except (OSError, ValueError):
            _target_root = None

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

    # The bundled MCP server launches via `uvx` (.mcp.json runs
    # `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`), so every MCP tool --
    # /chameleon-init, refresh, status, and the codebase-comprehension queries --
    # is gated on `uvx` resolving. This is distinct from hook_interpreter_deps:
    # the hook ladder can win on the bundled venv or a version-named python3.x
    # with no uv present, so a machine can pass that check while the MCP server
    # cannot start at all. Probe `uvx` explicitly so a green report never hides a
    # dead MCP surface.
    uvx_path = shutil.which("uvx")
    if uvx_path:
        checks.append({"name": "mcp_server_launcher", "status": "ok", "detail": uvx_path})
    elif shutil.which("uv"):
        checks.append(
            {
                "name": "mcp_server_launcher",
                "status": "warn",
                "detail": (
                    f"`uv` is on PATH ({shutil.which('uv')}) but `uvx` is not; the MCP "
                    "server is launched as `uvx`, so put uv's tool shim on PATH or the "
                    "MCP tools will not load"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "mcp_server_launcher",
                "status": "error",
                "detail": (
                    "neither `uvx` nor `uv` on PATH; the bundled MCP server cannot launch, "
                    "so chameleon's MCP tools (/chameleon-init, refresh, status, and "
                    "codebase queries) are unavailable. Install uv: "
                    "https://docs.astral.sh/uv/getting-started/installation/"
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
            "stop-backstop",
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
            # Matches both prose-injection-drop stderr prints (safe_prose_text's
            # "<name> dropped from context: contains a prompt-injection pattern"
            # and load_profile_dir's "idioms.md dropped from context: contains a
            # prompt-injection, secret, or dangerous pattern") regardless of which
            # artifact name fills the middle.
            _injection_drop_re = _re.compile(
                r"chameleon: \S+ dropped from context: contains a prompt-injection"
            )
            # Group lines into ENTRIES (one timestamped anchor line plus every
            # continuation line up to the next anchor), then window/slice by
            # entry, not by raw line. The previous line-based approach kept a
            # flat `recent` list and glued any non-timestamped line onto it
            # whenever it was non-empty -- so a continuation line that
            # actually belonged to an OUT-OF-WINDOW (or unparseable-timestamp)
            # anchor got misattributed to whatever real entry preceded it in
            # the file, and `recent[-5:]` could then surface that unrelated
            # continuation (e.g. a benign first-run-setup banner) while the
            # anchor line of the real recent error it displaced fell outside
            # the slice entirely.
            raw_lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            entries: list[list[str] | None] = []
            for line in raw_lines:
                m = ts_re.match(line)
                if m:
                    try:
                        when = _dt.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_UTC)
                    except ValueError:
                        when = None
                    entries.append([line] if when is not None and when >= cutoff else None)
                    continue
                # Continuation line: attach to the entry that most recently
                # started, IN-WINDOW ONLY -- an out-of-window (or dropped/
                # unparseable) anchor is represented by `None` above, so its
                # own continuation lines are dropped with it instead of
                # bleeding into a different, real in-window entry. A line
                # before any anchor at all has nothing to attach to.
                if entries and entries[-1] is not None:
                    entries[-1].append(line)
            recent_entries = [e for e in entries if e]
            tail = [ln for e in recent_entries[-5:] for ln in e]

            from chameleon_mcp.sanitization import sanitize_for_chameleon_context as _san

            if tail:
                # Log lines can embed repo-derived text (an exception message
                # carrying a symbol name, a path from a hostile fixture), and
                # this detail reaches the model surface — sanitize each line.
                tail = [_san(ln) for ln in tail]
                # The log is installation-wide: entries may come from other
                # repos (deleted QA fixtures included). Say so, or a fresh-repo
                # user reads a stale unrelated error as their repo's problem.
                tail = [
                    "installation-wide hook error log (last 72h; entries may be from other repos):"
                ] + tail
                checks.append({"name": "recent_hook_errors", "status": "warn", "detail": tail})
            else:
                checks.append(
                    {
                        "name": "recent_hook_errors",
                        "status": "ok",
                        "detail": "no errors in the last 72h",
                    }
                )

            # Prose-injection-drop warnings (loader.safe_prose_text /
            # load_profile_dir's idioms.md guard) are plain stderr prints with NO
            # leading `[timestamp]` anchor -- the hook wrappers redirect a hook's
            # raw stderr straight into this same log (`2>>"${LOG_FILE}"`), so this
            # warning class never matches ts_re and the anchor-grouping pass above
            # drops it silently (an unanchored line "has nothing to attach to").
            # That is exactly the ONE diagnostic doctor exists to surface: a live
            # poisoning event correctly blocked at the read path must leave a
            # trace here. Scan the raw lines independently of the anchor grouping
            # (they carry no timestamp to window against) and surface the most
            # recent ones regardless of the 72h window.
            injection_drops = [_san(ln) for ln in raw_lines if _injection_drop_re.search(ln)][-5:]
            if injection_drops:
                checks.append(
                    {
                        "name": "prose_injection_drops",
                        "status": "warn",
                        "detail": [
                            "prose artifact(s) dropped for prompt-injection at the read path:"
                        ]
                        + injection_drops,
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

    # Walk up to the repo root so doctor reports the repo's config even when the
    # session cwd is a subdirectory (a monorepo workspace, app/ under a Rails
    # repo). Reading Path.cwd()/.chameleon directly reported a configured repo as
    # unconfigured from any subdir, contradicting the rest of the status flow
    # (which resolves the repo via a file path that walks to root).
    from chameleon_mcp.profile.loader import find_repo_root

    _doctor_base = _target_root or Path.cwd()
    try:
        _doctor_root = find_repo_root(_doctor_base) or _doctor_base
    except (OSError, ValueError):
        # A pathological base (unresolvable / too-long path) must not crash the
        # per-repo checks: fall back to cwd so the plumbing checks still report.
        _doctor_root = Path.cwd()
    # A linked worktree has no .chameleon of its own; every check below reads
    # the main worktree's committed config/artifacts instead.
    from chameleon_mcp.worktree import resolve_profile_root

    _doctor_root = resolve_profile_root(_doctor_root)
    cwd_config = _doctor_root / ".chameleon" / "config.json"
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

                    _doctor_resolved = resolve_production_ref(_doctor_root, cfg.production_ref)
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
                    "auto_rename, one-time trust (it persists across profile "
                    "changes and a refresh never re-prompts), and enforcement.mode "
                    "'enforce' (calibrated block rules deny for real on a trusted "
                    "repo; set 'shadow' to log-only or 'off' for advisory). OFF by "
                    "default: canonical_ref (branch pinning) and production_ref "
                    "(ref-pinned derivation; auto-locked at init/refresh for "
                    "origin-backed repos). Add a config.json to change these. To "
                    "restore re-prompting when the profile changes after a grant, "
                    "set CHAMELEON_TRUST_REVALIDATE=1 in the environment."
                ),
            }
        )

    # Dead-install detectors: an install can pass every plumbing check above
    # while chameleon does nothing — generated artifacts missing so resolution
    # is degenerate, every turn-end reviewer spawn failing, or a trusted repo
    # whose edits never resolve an archetype. Each detector fails open: absence
    # of a profile / attestations / metrics is healthy-unknown, never a warn.
    # Resolve to the REPO ROOT (the walk-up _doctor_root already used by
    # config_json/production_ref), not the raw cwd: reading `cwd/.chameleon`
    # from a subdir reported a corrupt-artifact repo as clean (false-clean).
    cwd_root = _doctor_root
    cwd_profile_dir = cwd_root / ".chameleon"
    try:
        cwd_repo_id: str | None = _compute_repo_id(cwd_root)
    except Exception:
        cwd_repo_id = None

    try:
        from chameleon_mcp.bootstrap.transaction import is_committed as _tx_is_committed

        if (cwd_profile_dir / "profile.json").is_file() and not _tx_is_committed(cwd_profile_dir):
            # Artifacts present but the COMMITTED sentinel is missing or
            # marker-laden: a torn transaction or unresolved merge. Say so
            # directly — the index loaders now refuse this state uniformly,
            # and interpreting their refusal per-artifact would misreport it
            # as "stale schema or oversize" while the repo_states section of
            # this same doctor run reads the root as having no profile.
            checks.append(
                {
                    "name": "profile_artifacts",
                    "status": "warn",
                    "detail": (
                        "profile is uncommitted (COMMITTED sentinel missing or "
                        "conflict-marked): a torn transaction or unresolved merge. "
                        "Every loader refuses this state; run /chameleon-refresh "
                        "to regenerate."
                    ),
                }
            )
        elif (cwd_profile_dir / "profile.json").is_file():
            import json as _json

            artifact_problems: list[str] = []
            _lang = None
            try:
                from chameleon_mcp.profile.loader import MAX_SUPPORTED_SCHEMA_VERSION

                _pj = _json.loads((cwd_profile_dir / "profile.json").read_text(encoding="utf-8"))
                _lang = _pj.get("language")
                _sv = _pj.get("schema_version")
                # A schema_version this engine cannot load (non-int or > max)
                # PARSES as valid JSON, so a plain parse check reads it as healthy
                # while the load path refuses it and the hooks fail open. Flag it.
                if _sv is not None and (isinstance(_sv, bool) or not isinstance(_sv, int)):
                    artifact_problems.append("profile.json schema_version is not an integer")
                elif isinstance(_sv, int) and _sv > MAX_SUPPORTED_SCHEMA_VERSION:
                    artifact_problems.append(
                        f"profile.json schema_version {_sv} exceeds engine max "
                        f"{MAX_SUPPORTED_SCHEMA_VERSION} (upgrade chameleon)"
                    )
            except Exception:
                # A corrupt profile.json is itself a dead-install signal, not a
                # reason to silently narrow the checked set to the base pair.
                artifact_problems.append("profile.json corrupt")
            # The core generated artifacts every profile writes, for all three
            # languages: a corrupt or missing one silently degrades archetype
            # resolution, conventions, and enforcement while every plumbing check
            # above stays green -- the exact false-clean this detector exists to
            # catch. (calls_index / function_catalog back the cross-file facts;
            # the rest back per-edit resolution.)
            expected_artifacts = [
                "archetypes.json",
                "canonicals.json",
                "conventions.json",
                "rules.json",
                "enforcement.json",
                "calls_index.json",
                "function_catalog.json",
            ]
            # Language-specific cross-file indexes: TS and Python both emit the
            # export/reverse import graph; Ruby emits a constant index instead.
            # (The old code only added these for TypeScript, so a corrupt Python
            # exports_index or a corrupt Ruby constant_index was silent-clean.)
            if _lang in ("typescript", "python"):
                expected_artifacts += ["exports_index.json", "reverse_index.json"]
            elif _lang == "ruby":
                expected_artifacts += ["constant_index.json"]
            # A parseable artifact can still be STALE-SCHEMA: an index built by an
            # older engine (e.g. a pre-v2.41 calls_index at schema_version 1) is
            # valid JSON, so a plain parse check reads it "ok" while the loader
            # rejects the schema and every calls / cross-file tool silently
            # degrades to zero facts -- the exact false-clean this detector exists
            # to catch, one layer deeper than a missing/corrupt file. Call the real
            # loader (present file + None return = stale schema or oversize, both
            # of which /chameleon-refresh repairs); each loader honors its own
            # readable-version set (counterexamples reads {1,2}), so this never
            # false-flags a version the engine still accepts. Fail-open: if the
            # loaders can't be imported, fall back to the parse-only check.
            _schema_loaders: dict = {}
            try:
                from chameleon_mcp.calls_index import load_calls_index as _ld_calls
                from chameleon_mcp.constant_index import load_constant_index as _ld_const
                from chameleon_mcp.counterexamples import load_counterexamples as _ld_cx
                from chameleon_mcp.symbol_index import (
                    load_exports_index as _ld_exports,
                )
                from chameleon_mcp.symbol_index import (
                    load_reverse_index as _ld_reverse,
                )
                from chameleon_mcp.symbol_signatures import (
                    load_symbol_signatures as _ld_sigs,
                )

                _schema_loaders = {
                    "calls_index.json": _ld_calls,
                    "exports_index.json": _ld_exports,
                    "reverse_index.json": _ld_reverse,
                    "constant_index.json": _ld_const,
                    "symbol_signatures.json": _ld_sigs,
                    "counterexamples.json": _ld_cx,
                }
            except Exception:
                _schema_loaders = {}

            def _schema_stale(art: str) -> bool:
                loader = _schema_loaders.get(art)
                if loader is None:
                    return False
                try:
                    return loader(cwd_root) is None
                except Exception:
                    return False

            for art in expected_artifacts:
                apath = cwd_profile_dir / art
                if not apath.is_file():
                    artifact_problems.append(f"{art} missing")
                    continue
                try:
                    _json.loads(apath.read_text(encoding="utf-8"))
                except Exception:
                    artifact_problems.append(f"{art} corrupt")
                    continue
                if _schema_stale(art):
                    artifact_problems.append(
                        f"{art} unreadable by this engine (stale schema or oversize)"
                    )
            # Advisory artifacts (nearby-signatures, counterexamples): validate
            # IF PRESENT (a present-but-corrupt one degrades those features), but
            # do not require presence -- an older profile may predate them.
            for art in ("symbol_signatures.json", "counterexamples.json"):
                apath = cwd_profile_dir / art
                if apath.is_file():
                    try:
                        _json.loads(apath.read_text(encoding="utf-8"))
                    except Exception:
                        artifact_problems.append(f"{art} corrupt")
                        continue
                    if _schema_stale(art):
                        artifact_problems.append(
                            f"{art} unreadable by this engine (stale schema or oversize)"
                        )
            # The per-artifact checks above are parse-only for the CORE bundle
            # (archetypes/canonicals/rules/conventions/profile.json), so a
            # valid-JSON-but-wrong-schema core artifact slipped through as
            # "parseable" while the profile is actually unloadable. Catch the two
            # shapes the loader rejects that a bare json.loads does not:
            #   (1) a core artifact that is not a JSON OBJECT (a bare array/scalar);
            #   (2) a generation MISMATCH across the artifacts the LOADER gates on.
            # The unloadable claim mirrors load_profile_dir's own consistency
            # gate exactly: that gate covers profile/archetypes/rules/canonicals
            # only. conventions.json drifting a generation behind never blocks a
            # load, so it is reported as staleness, not unloadability.
            _core_bundle = (
                "archetypes.json",
                "canonicals.json",
                "rules.json",
                "conventions.json",
                "profile.json",
            )
            _loader_gated = {"archetypes.json", "canonicals.json", "rules.json", "profile.json"}
            _generations: set = set()
            _conventions_gen: set = set()
            for art in _core_bundle:
                apath = cwd_profile_dir / art
                if not apath.is_file():
                    continue
                try:
                    _obj = _json.loads(apath.read_text(encoding="utf-8"))
                except Exception:
                    continue  # already reported corrupt above if it was expected
                if not isinstance(_obj, dict):
                    artifact_problems.append(f"{art} is not a JSON object (wrong schema)")
                    continue
                if "generation" in _obj:
                    (_generations if art in _loader_gated else _conventions_gen).add(
                        _obj.get("generation")
                    )
            if len(_generations) > 1:
                artifact_problems.append(
                    f"profile generation mismatch across artifacts ({sorted(map(str, _generations))}); "
                    "the bundle is unloadable"
                )
            elif _conventions_gen and _generations and _conventions_gen != _generations:
                artifact_problems.append(
                    "conventions.json generation lags the loaded bundle "
                    f"({sorted(map(str, _conventions_gen))} vs {sorted(map(str, _generations))}); "
                    "loads fine, but taught-convention data may predate the last refresh"
                )
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
            # A grounding event (judge_defs_*/judge_transitive_*/judge_facts_*)
            # rides the degraded_spawn channel in pre-2.38.9 attestations but is
            # NOT a spawn failure; counting it warned "reviewer failing to spawn"
            # for a healthy reviewer. Drop those so only genuine failures warn.
            from chameleon_mcp.judge import is_grounding_event as _is_grounding

            # Recognizes BOTH check-event vocabularies so a mixed-version
            # attestation history (a repo whose ledger spans the phase-3
            # cutover) still surfaces a dead reviewer: the pre-cutover
            # "correctness_judge" checks, and the async scheduler's own
            # "review_job" checks (stop/scheduler.py + stop/pipeline.py's
            # _run_review_job). The new vocabulary has no "spawned/completed"
            # equivalent (the detached job runner records no explicit success
            # checkpoint), so a clean launch is inferred as a "review_job"/
            # "spawned" with no launch-failure companion -- across the whole
            # window, a net surplus of spawns over degradations.
            #
            # Both the spawn and the degrade tallies MUST sum the attestation's
            # per-entry `count` (``_build_session_attestation`` collapses
            # repeated identical (check, status, reason) events into ONE entry
            # carrying a count), never len() of the entry list: review_job/
            # degraded always carries the single reason "platform_unavailable",
            # so counting ENTRIES reads any number of failures as exactly one --
            # an 8-spawn/2-fail reviewer would false-warn (2 spawn entries > 1
            # degrade entry is fine, but 1 spawn reason vs 1 degrade reason nets
            # zero) and a 1-spawn/9-fail one would false-OK.
            def _ev_count(e: dict) -> int:
                c = e.get("count")
                return c if isinstance(c, int) and not isinstance(c, bool) and c > 0 else 1

            judge_events: list[dict] = []
            degraded: list[dict] = []
            completed: list[dict] = []
            review_spawned_count = 0
            review_degraded_count = 0
            for rec in _records:
                rec_checks = [e for e in (rec.get("checks") or []) if isinstance(e, dict)]

                old_events = [e for e in rec_checks if e.get("check") == "correctness_judge"]
                judge_events.extend(old_events)
                degraded.extend(
                    e
                    for e in old_events
                    if e.get("status") == "degraded_spawn" and not _is_grounding(e.get("reason"))
                )
                # Every spawn attempt logs "spawned/started"; only
                # "spawned/completed" marks a reviewer that actually ran.
                completed.extend(
                    e
                    for e in old_events
                    if e.get("status") == "spawned" and e.get("reason") == "completed"
                )

                review_events = [e for e in rec_checks if e.get("check") == "review_job"]
                judge_events.extend(review_events)
                review_degraded = [
                    e
                    for e in review_events
                    if e.get("status") in ("degraded", "platform_unavailable")
                ]
                degraded.extend(review_degraded)
                review_spawned_count += sum(
                    _ev_count(e) for e in review_events if e.get("status") == "spawned"
                )
                review_degraded_count += sum(_ev_count(e) for e in review_degraded)
            # A net surplus of review-job spawns over launch failures means at
            # least one clean detach in the window -- the review_job analog to
            # the correctness check's "spawned/completed" success signal.
            if review_spawned_count - review_degraded_count > 0:
                completed.append({"check": "review_job", "status": "spawned"})
            if degraded and not completed:
                judge_warn = True
                _reasons = sorted({str(e.get("reason") or "unknown") for e in degraded})
                _n_sessions = len({r.get("session_id") for r in _records if r.get("session_id")})
                _span = (
                    f"{_n_sessions} recent session(s)"
                    if _n_sessions
                    else f"{len(_records)} recent attestation(s)"
                )
                judge_detail = (
                    f"turn-end reviewer failing to spawn ({', '.join(_reasons)}) across the "
                    f"last {_span}; correctness review is not running. Check the claude "
                    "binary/auth, then verify with a new session."
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
        _source_exts = (
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".rb",
            ".py",
            ".pyi",
        )
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
        # A single path can carry several registry rows -- an older repo_id left
        # behind by a re-bootstrap or an id-scheme change (remote->uuid->path).
        # list_profiles orders most-recent-first, so the FIRST row for a root is
        # the active profile; the rest are stale. Listing the path three times
        # with contradictory trust states (the reported symptom) is noise, so
        # collapse the stale rows into a count on the authoritative entry.
        seen_roots: dict[str, int] = {}
        for r in profiles:
            root = r.get("repo_root")
            if root and root in seen_roots:
                prior = repo_states[seen_roots[root]]
                prior["stale_registry_entries"] = prior.get("stale_registry_entries", 0) + 1
                if r.get("trust_state") != prior.get("trust_state"):
                    prior["note"] = (
                        "older registry rows for this path record a different "
                        "trust state; the most recent profile (shown) governs"
                    )
                continue
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
            if root:
                seen_roots[root] = len(repo_states)
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
