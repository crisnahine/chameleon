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
from chameleon_mcp.safe_open import (
    _DEFAULT_PROFILE_ARTIFACT_MAX_BYTES,
    UnsafeFileError,
    safe_read_profile_artifact,
)

# BUG-023: schema_version is monotonic. A profile written by a future
# chameleon with a higher schema_version may contain fields this engine
# doesn't know about; reading it silently risks emitting wrong guidance.
# Keep this constant in sync with bootstrap.orchestrator.PROFILE_SCHEMA_VERSION
# (import-cycle avoidance: don't pull orchestrator at module load).
MAX_SUPPORTED_SCHEMA_VERSION = 7

# Process-global cache: directory (str) -> resolved repo root (Path | None).
# The directory-to-root mapping is structural (determined by marker files like
# .chameleon/, .git/, package.json) and only changes on bootstrap/uninstall.
_REPO_ROOT_CACHE: dict[str, Path | None] = {}


def clear_repo_root_cache() -> None:
    """Drop all cached directory -> repo_root mappings.

    Call on bootstrap_repo entry so a newly created .chameleon/ directory
    is picked up by subsequent find_repo_root calls.
    """
    _REPO_ROOT_CACHE.clear()


# Process-global cache: profile_dir (str) -> (mtime_token, LoadedProfile).
# Profile data only changes on bootstrap/refresh, so we avoid re-reading
# and re-parsing 4 JSON files + idioms.md on every get_pattern_context call.
# The mtime_token includes all 4 JSON artifacts + idioms.md so that
# /chameleon-teach changes (which only touch idioms.md) invalidate the cache.
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


# Back-compat re-export so existing readers that imported the loader cap
# (e.g. older tests) keep finding the constant at the same name.
_MAX_ARTIFACT_BYTES = _DEFAULT_PROFILE_ARTIFACT_MAX_BYTES


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


@dataclass
class LoadedProfile:
    """In-memory representation of a committed `.chameleon/` profile."""

    profile: dict
    archetypes: dict
    canonicals: dict
    rules: dict
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
        return None  # let downstream handle missing dirs
    # Anchor against /tmp and $TMPDIR. Both can be the same; that's fine.
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
    # tempfile.gettempdir() may return something else again (e.g. /var/folders/... on macOS).
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
    # World-writable check (mode bit 0o002). Symlinks are followed by stat.
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    if st.st_mode & 0o002:
        return f"refusing world-writable repo_root (mode={oct(st.st_mode)})"
    return None


REPO_ROOT_MARKERS: tuple[str, ...] = (
    ".chameleon",  # already-bootstrapped chameleon repo (highest priority)
    ".git",        # standard git repository
    "package.json",  # Node / TypeScript project
    "tsconfig.json",  # TypeScript project (no package.json)
    "Gemfile",       # Ruby / Rails project
    "pyproject.toml",  # Python project
    "go.mod",        # Go module
    "Cargo.toml",    # Rust crate
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

    # Check process-global cache before walking the filesystem.
    cache_key = str(current)
    if cache_key in _REPO_ROOT_CACHE:
        return _REPO_ROOT_CACHE[cache_key]

    result = _find_repo_root_uncached(current)
    _REPO_ROOT_CACHE[cache_key] = result
    return result


def _find_repo_root_uncached(current: Path) -> Path | None:
    """Filesystem walk implementation for find_repo_root (uncached)."""
    # Pass 1: walk up; record the first ancestor with ANY marker.
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
        # Closest marker is already a chameleon profile - done.
        reason = _is_unsafe_repo_root(first_marker_ancestor)
        if reason is not None:
            return None
        return first_marker_ancestor

    # Pass 2: closest marker is a language manifest (package.json, etc).
    # Continue walking up to see if an enclosing .chameleon profile exists.
    # If yes, prefer it (BUG-NEW-002 monorepo case). If no, return the
    # closer language-manifest ancestor (pre-fix behavior, preserves
    # test isolation).
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


def _compute_mtime_token(artifact_paths: list[Path], idioms_path: Path) -> str:
    """Build an mtime fingerprint from the 4 JSON artifacts + idioms.md.

    Returns a dash-joined string of st_mtime_ns values. idioms.md
    contributes "0" when absent so the token length is always 5 parts.

    Raises FileNotFoundError / OSError if any JSON artifact is missing
    (idioms.md absence is not an error).
    """
    parts = [str(p.stat().st_mtime_ns) for p in artifact_paths]
    try:
        parts.append(str(idioms_path.stat().st_mtime_ns))
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

    # Quick mtime check — 5 stat calls. On hit, skip all reads + parsing.
    try:
        quick_artifact_paths = [
            profile_dir / "profile.json",
            profile_dir / "archetypes.json",
            profile_dir / "rules.json",
            profile_dir / "canonicals.json",
        ]
        quick_idioms = profile_dir / "idioms.md"
        quick_token = _compute_mtime_token(quick_artifact_paths, quick_idioms)
        cached = _PROFILE_CACHE.get(cache_key)
        if cached is not None and cached[0] == quick_token:
            return cached[1]
    except (FileNotFoundError, OSError):
        # A JSON artifact is missing or unreadable — fall through to the
        # full read path which will raise a proper ProfileLoadError.
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

    # Capture mtime tuple BEFORE reads
    mtimes_before = tuple(p.stat().st_mtime_ns for p in artifact_paths)

    # Read all artifacts
    profile = json.loads(_safe_read_artifact(artifact_paths[0]))
    archetypes = json.loads(_safe_read_artifact(artifact_paths[1]))
    rules = json.loads(_safe_read_artifact(artifact_paths[2]))
    canonicals = json.loads(_safe_read_artifact(artifact_paths[3]))

    idioms_path = profile_dir / "idioms.md"
    try:
        idioms_text = _safe_read_artifact(idioms_path)
    except FileNotFoundError:
        idioms_text = ""

    # Capture mtime tuple AFTER reads — must match
    mtimes_after = tuple(p.stat().st_mtime_ns for p in artifact_paths)
    if mtimes_before != mtimes_after:
        raise ProfileLoadError(
            "profile changed during load (mid-load mutation detected); retry"
        )

    # Verify generation counter consistency across all 4 JSON files
    gens = (
        profile.get("generation"),
        archetypes.get("generation"),
        rules.get("generation"),
        canonicals.get("generation"),
    )
    if not all(isinstance(g, int) for g in gens) or len(set(gens)) != 1:
        raise ProfileLoadError(
            f"profile generation mismatch across artifacts: {gens}; "
            "/chameleon-refresh recommended"
        )

    declared_min = profile.get("engine_min_version") or archetypes.get("engine_min_version")
    if declared_min and _version_tuple(ENGINE_VERSION) < _version_tuple(declared_min):
        raise ProfileLoadError(
            f"profile requires engine >= {declared_min} but this engine is "
            f"{ENGINE_VERSION}; upgrade chameleon-mcp"
        )

    # BUG-023: refuse to load a profile written by a newer schema. This
    # engine doesn't know what new fields mean and may return incorrect
    # data if it silently accepts the read.
    declared_schema = profile.get("schema_version")
    if isinstance(declared_schema, int) and declared_schema > MAX_SUPPORTED_SCHEMA_VERSION:
        raise ProfileLoadError(
            f"profile schema_version {declared_schema} is newer than this "
            f"engine supports (max {MAX_SUPPORTED_SCHEMA_VERSION}); "
            f"upgrade chameleon-mcp"
        )

    # Build mtime_token from all 4 JSON artifacts + idioms.md so that
    # /chameleon-teach (which only touches idioms.md) also invalidates.
    idioms_mtime = "0"
    try:
        idioms_mtime = str(idioms_path.stat().st_mtime_ns)
    except FileNotFoundError:
        pass
    mtime_token = "-".join(str(m) for m in mtimes_after) + "-" + idioms_mtime
    archetype_names = sorted(archetypes.get("archetypes", {}).keys())

    loaded = LoadedProfile(
        profile=profile,
        archetypes=archetypes,
        canonicals=canonicals,
        rules=rules,
        idioms_text=idioms_text,
        generation=gens[0],
        profile_dir=profile_dir,
        mtime_token=mtime_token,
        archetype_names=archetype_names,
    )

    # Store in process-global cache.
    _PROFILE_CACHE[str(profile_dir)] = (mtime_token, loaded)

    return loaded
