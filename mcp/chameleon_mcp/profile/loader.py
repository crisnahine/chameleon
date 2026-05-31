"""Profile loader — reads committed `.chameleon/` directory contents.

Per docs/architecture.md "SQLite schemas" → "Cross-file referential integrity":
applies the double-fstat loader pattern with generation counter verification.

Refuses to load if `COMMITTED` sentinel is missing (atomic-commit guard).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp import __version__ as ENGINE_VERSION
from chameleon_mcp.bootstrap.transaction import is_committed
from chameleon_mcp.profile.schema import (
    ARCHETYPE_NAME_RE,
    SchemaError,
    _check_depth,
    _no_duplicate_keys,
)
from chameleon_mcp.safe_open import (
    UnsafeFileError,
    safe_read_profile_artifact,
)

MAX_SUPPORTED_SCHEMA_VERSION = 8

_REPO_ROOT_CACHE: dict[str, Path | None] = {}


def clear_repo_root_cache() -> None:
    """Drop all cached directory -> repo_root mappings.

    Call on bootstrap_repo entry so a newly created .chameleon/ directory
    is picked up by subsequent find_repo_root calls.
    """
    _REPO_ROOT_CACHE.clear()


_PROFILE_CACHE: dict[str, tuple[str, LoadedProfile]] = {}


def clear_profile_cache() -> None:
    """Drop all cached LoadedProfile entries and repo root mappings.

    Call after bootstrap, refresh, or teach — any operation that mutates
    the on-disk profile artifacts. Also clears the repo root cache since
    bootstrap creates a new .chameleon/ directory that changes root resolution.
    """
    _PROFILE_CACHE.clear()
    _REPO_ROOT_CACHE.clear()


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a "X.Y.Z" version string. Trailing junk is dropped."""
    parts: list[int] = []
    for chunk in str(v).split("."):
        try:
            parts.append(int("".join(c for c in chunk if c.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


class ProfileLoadError(Exception):
    """Raised when a profile fails to load (missing sentinel, schema, generation)."""


def _safe_read_artifact(path: Path) -> str:
    """Read a chameleon profile artifact via the shared safe helper.

    Thin wrapper that translates UnsafeFileError into ProfileLoadError so
    existing callers (load_profile_dir, etc.) keep seeing a single
    exception type. The actual atomic open + size cap + symlink refusal
    now lives in chameleon_mcp.safe_open.safe_read_profile_artifact so all
    four hashed-artifact consumers share one implementation.
    """
    try:
        return safe_read_profile_artifact(path)
    except FileNotFoundError:
        raise
    except UnsafeFileError as exc:
        raise ProfileLoadError(str(exc)) from exc


def _loads_hardened(content: str) -> dict:
    """Parse a committed JSON artifact, rejecting duplicate keys and deep nesting.

    A committed profile is trust-gated, but the trust hash covers the bytes,
    not their semantics: a teammate could push a profile.json with duplicate
    keys (last-wins ambiguity) or pathological nesting. These checks bound
    both before the artifact reaches model context.
    """
    try:
        obj = json.loads(content, object_pairs_hook=_no_duplicate_keys)
        _check_depth(obj)
    except (SchemaError, json.JSONDecodeError) as exc:
        raise ProfileLoadError(f"profile artifact rejected: {exc}") from exc
    if not isinstance(obj, dict):
        # Valid JSON but not an object (e.g. a bare list/number) — downstream
        # code does dict ops, so reject cleanly instead of AttributeError later.
        raise ProfileLoadError("profile artifact must be a JSON object")
    return obj


@dataclass
class LoadedProfile:
    """In-memory representation of a committed `.chameleon/` profile."""

    profile: dict
    archetypes: dict
    canonicals: dict
    rules: dict
    conventions: dict
    idioms_text: str
    generation: int
    profile_dir: Path
    mtime_token: str = ""
    """Concatenated mtime fingerprint of all 4 JSON artifacts (for cache invalidation)."""

    archetype_names: list[str] = field(default_factory=list)


def _is_unsafe_repo_root(root: Path) -> str | None:
    """Return a human-readable refusal reason if `root` is not a safe
    chameleon repo root, else None.

    Refuses: /tmp, $TMPDIR (and subdirs of either), and world-writable
    directories. The home-ancestor guard lives separately in tools.py
    detect_repo.
    """
    if os.environ.get("CHAMELEON_ALLOW_TMP_REPO") == "1":
        return None
    try:
        resolved = root.resolve(strict=False)
    except OSError:
        return None
    forbidden_anchors = []
    try:
        forbidden_anchors.append(Path("/tmp").resolve(strict=False))
    except OSError:
        pass
    tmp_env = os.environ.get("TMPDIR")
    if tmp_env:
        try:
            forbidden_anchors.append(Path(tmp_env).resolve(strict=False))
        except OSError:
            pass
    try:
        forbidden_anchors.append(Path(tempfile.gettempdir()).resolve(strict=False))
    except OSError:
        pass
    for anchor in forbidden_anchors:
        try:
            if resolved == anchor or anchor in resolved.parents:
                return f"refusing repo_root inside temp dir {anchor}"
        except OSError:
            continue
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    if st.st_mode & 0o002:
        return f"refusing world-writable repo_root (mode={oct(st.st_mode)})"
    return None


REPO_ROOT_MARKERS: tuple[str, ...] = (
    ".chameleon",
    ".git",
    "package.json",
    "tsconfig.json",
    "Gemfile",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
)


def find_repo_root(file_path: Path) -> Path | None:
    """Walk up from file_path looking for a repo-root marker.

    Two-pass strategy (BUG-NEW-002, v0.5.7-redo):

    Pass 1 - if the immediate first-marker ancestor's marker is
    ``.chameleon``, return it. (Fast path: behaves like pre-fix code
    when the closest marker is already a chameleon profile, which is
    the overwhelming-common case for files inside a single-root repo.)

    Pass 2 - the first-marker ancestor's marker is NOT ``.chameleon``
    (e.g. workspace ``package.json``). Continue walking up looking for
    an ancestor that DOES have ``.chameleon``. If found within
    32 levels, return THAT one. Otherwise fall back to the first-marker
    ancestor from pass 1 (pre-fix behavior).

    Why two-pass: pre-v0.5.7 the walk stopped at the first marker, so
    monorepos with ``.chameleon`` at the root and ``package.json`` at
    each workspace returned the workspace as repo_root and masked the
    root profile. The straight ``.chameleon`` priority within a level
    couldn't fix that because the marker existed at a DIFFERENT level.

    Why not always prefer ``.chameleon``: tests, especially run_all_orders.py
    test-isolation harness, can leak stray ``.chameleon`` dirs in tmp
    paths between tests via the shared filesystem. Walking up looking
    for ``.chameleon`` past a closer real marker introduces order-
    dependent test failures. The two-pass approach is defensive: if a
    closer language marker is present we trust it as the lower bound,
    and we only override when a closer-or-equal-priority chameleon
    profile genuinely exists upstream.

    Returns the marker directory, or None if no marker is found within
    32 parent directories.
    """
    current = file_path.expanduser()
    if current.is_file():
        current = current.parent
    try:
        current = current.resolve()
    except OSError:
        return None

    cache_key = str(current)
    if cache_key in _REPO_ROOT_CACHE:
        return _REPO_ROOT_CACHE[cache_key]

    result = _find_repo_root_uncached(current)
    _REPO_ROOT_CACHE[cache_key] = result
    return result


def _find_repo_root_uncached(current: Path) -> Path | None:
    """Filesystem walk implementation for find_repo_root (uncached)."""
    first_marker_ancestor: Path | None = None
    first_marker_name: str | None = None
    walker = current
    for _ in range(32):
        for marker in REPO_ROOT_MARKERS:
            if (walker / marker).exists():
                first_marker_ancestor = walker
                first_marker_name = marker
                break
        if first_marker_ancestor is not None:
            break
        parent = walker.parent
        if parent == walker:
            break
        walker = parent

    if first_marker_ancestor is None:
        return None

    if first_marker_name == ".chameleon":
        reason = _is_unsafe_repo_root(first_marker_ancestor)
        if reason is not None:
            return None
        return first_marker_ancestor

    walker = first_marker_ancestor.parent
    if walker == first_marker_ancestor:
        reason = _is_unsafe_repo_root(first_marker_ancestor)
        if reason is not None:
            return None
        return first_marker_ancestor
    for _ in range(32):
        if (walker / ".chameleon").exists():
            reason = _is_unsafe_repo_root(walker)
            if reason is not None:
                return None
            return walker
        parent = walker.parent
        if parent == walker:
            break
        walker = parent
    reason = _is_unsafe_repo_root(first_marker_ancestor)
    if reason is not None:
        return None
    return first_marker_ancestor


def _compute_mtime_token(
    artifact_paths: list[Path],
    idioms_path: Path,
    conventions_path: Path | None = None,
) -> str:
    """Build an mtime fingerprint from the 4 JSON artifacts + idioms.md
    (+ optional conventions.json).

    Returns a dash-joined string of st_mtime_ns values. idioms.md and
    conventions.json each contribute "0" when absent so the token shape is
    stable.

    Raises FileNotFoundError / OSError if any of the 4 required JSON artifacts
    is missing (idioms.md / conventions.json absence is not an error).
    """
    parts = [str(p.stat().st_mtime_ns) for p in artifact_paths]
    for optional in (idioms_path, conventions_path):
        if optional is None:
            continue
        try:
            parts.append(str(optional.stat().st_mtime_ns))
        except FileNotFoundError:
            parts.append("0")
    return "-".join(parts)


def load_profile_dir(profile_dir: Path) -> LoadedProfile:
    """Load and validate all artifacts from a `.chameleon/` directory.

    Implements the double-fstat pattern: capture mtime tuple before reads,
    verify after reads to detect mid-load mutation.

    Results are cached process-globally keyed on (profile_dir, mtime_token).
    The mtime_token covers all 4 JSON artifacts + idioms.md so that any
    on-disk change (including /chameleon-teach which only touches idioms.md)
    triggers a full re-read.

    Raises:
        ProfileLoadError: missing sentinel, malformed JSON, generation
                          mismatch across files, or mid-load mutation.
    """
    cache_key = str(profile_dir)

    try:
        quick_artifact_paths = [
            profile_dir / "profile.json",
            profile_dir / "archetypes.json",
            profile_dir / "rules.json",
            profile_dir / "canonicals.json",
        ]
        quick_idioms = profile_dir / "idioms.md"
        quick_conventions = profile_dir / "conventions.json"
        quick_token = _compute_mtime_token(quick_artifact_paths, quick_idioms, quick_conventions)
        cached = _PROFILE_CACHE.get(cache_key)
        if cached is not None and cached[0] == quick_token:
            return cached[1]
    except (FileNotFoundError, OSError):
        pass

    if not is_committed(profile_dir):
        raise ProfileLoadError(
            f"profile at {profile_dir} is missing COMMITTED sentinel "
            "(incomplete or corrupted; run /chameleon-refresh)"
        )

    artifact_paths = [
        profile_dir / "profile.json",
        profile_dir / "archetypes.json",
        profile_dir / "rules.json",
        profile_dir / "canonicals.json",
    ]
    for p in artifact_paths:
        if not p.is_file():
            raise ProfileLoadError(f"missing required artifact: {p}")

    conventions_path = profile_dir / "conventions.json"

    def _opt_mtime(pp: Path) -> int | None:
        try:
            return pp.stat().st_mtime_ns
        except OSError:
            return None

    mtimes_before = tuple(p.stat().st_mtime_ns for p in artifact_paths)
    conv_mtime_before = _opt_mtime(conventions_path)

    profile = _loads_hardened(_safe_read_artifact(artifact_paths[0]))
    archetypes = _loads_hardened(_safe_read_artifact(artifact_paths[1]))
    rules = _loads_hardened(_safe_read_artifact(artifact_paths[2]))
    canonicals = _loads_hardened(_safe_read_artifact(artifact_paths[3]))

    try:
        conventions = _loads_hardened(_safe_read_artifact(conventions_path))
    except FileNotFoundError:
        conventions = {}

    idioms_path = profile_dir / "idioms.md"
    try:
        idioms_text = _safe_read_artifact(idioms_path)
    except FileNotFoundError:
        idioms_text = ""

    mtimes_after = tuple(p.stat().st_mtime_ns for p in artifact_paths)
    conv_mtime_after = _opt_mtime(conventions_path)
    # conventions.json is in the cache token, so it must also be in the
    # mid-load mutation guard, or a conventions rewrite (e.g. teach_competing_
    # import) between read and token-compute could cache stale content.
    if mtimes_before != mtimes_after or conv_mtime_before != conv_mtime_after:
        raise ProfileLoadError("profile changed during load (mid-load mutation detected); retry")

    gens = (
        profile.get("generation"),
        archetypes.get("generation"),
        rules.get("generation"),
        canonicals.get("generation"),
    )
    if not all(isinstance(g, int) for g in gens) or len(set(gens)) != 1:
        raise ProfileLoadError(
            f"profile generation mismatch across artifacts: {gens}; /chameleon-refresh recommended"
        )

    declared_min = profile.get("engine_min_version") or archetypes.get("engine_min_version")
    if declared_min and _version_tuple(ENGINE_VERSION) < _version_tuple(declared_min):
        raise ProfileLoadError(
            f"profile requires engine >= {declared_min} but this engine is "
            f"{ENGINE_VERSION}; upgrade chameleon-mcp"
        )

    declared_schema = profile.get("schema_version")
    if isinstance(declared_schema, int) and declared_schema > MAX_SUPPORTED_SCHEMA_VERSION:
        raise ProfileLoadError(
            f"profile schema_version {declared_schema} is newer than this "
            f"engine supports (max {MAX_SUPPORTED_SCHEMA_VERSION}); "
            f"upgrade chameleon-mcp"
        )

    # Use the same helper as the cache-hit check so the read- and write-side
    # tokens are byte-identical (4 artifacts + idioms.md + conventions.json).
    # mtimes_after == mtimes_before was just verified, so re-stat is consistent.
    mtime_token = _compute_mtime_token(artifact_paths, idioms_path, conventions_path)

    # Drop archetype-name keys that don't match ARCHETYPE_NAME_RE so a poisoned
    # or hand-edited archetypes.json can't push a name with an embedded newline
    # + prose into <chameleon-context>. Legitimately-generated names always
    # match (bootstrap naming enforces the same pattern).
    _arch_map = archetypes.get("archetypes")
    if isinstance(_arch_map, dict):
        archetypes["archetypes"] = {
            k: v for k, v in _arch_map.items() if isinstance(k, str) and ARCHETYPE_NAME_RE.match(k)
        }
    archetype_names = sorted((archetypes.get("archetypes") or {}).keys())

    loaded = LoadedProfile(
        profile=profile,
        archetypes=archetypes,
        canonicals=canonicals,
        rules=rules,
        conventions=conventions,
        idioms_text=idioms_text,
        generation=gens[0],
        profile_dir=profile_dir,
        mtime_token=mtime_token,
        archetype_names=archetype_names,
    )

    _PROFILE_CACHE[str(profile_dir)] = (mtime_token, loaded)

    return loaded
