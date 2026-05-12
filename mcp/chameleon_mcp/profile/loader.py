"""Profile loader — reads committed `.chameleon/` directory contents.

Per ARCHITECTURE.md "SQLite schemas" → "Cross-file referential integrity":
applies the double-fstat loader pattern with generation counter verification.

Refuses to load if `COMMITTED` sentinel is missing (atomic-commit guard).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from chameleon_mcp import __version__ as ENGINE_VERSION
from chameleon_mcp.bootstrap.transaction import is_committed

# BUG-023: schema_version is monotonic. A profile written by a future
# chameleon with a higher schema_version may contain fields this engine
# doesn't know about; reading it silently risks emitting wrong guidance.
# Keep this constant in sync with bootstrap.orchestrator.PROFILE_SCHEMA_VERSION
# (import-cycle avoidance: don't pull orchestrator at module load).
MAX_SUPPORTED_SCHEMA_VERSION = 7


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

    Two-pass strategy (BUG-NEW-002, v0.5.7):

    Pass 1 — walks up to 32 levels collecting every ancestor that has a
    ``.chameleon/`` directory. If at least one is found, returns the
    DEEPEST such ancestor (closest to the file). A workspace sub-profile
    takes priority over an outer profile, but more importantly: a profile
    that exists higher up the tree is never masked by a closer non-chameleon
    marker (e.g. ``apps/web/package.json``).

    Pass 2 — no ``.chameleon/`` found anywhere. Walks up again and returns
    the first ancestor with any other marker (.git, package.json,
    tsconfig.json, Gemfile, pyproject.toml, go.mod, Cargo.toml).

    Pre-v0.5.7 the walk was single-pass over all markers in priority order,
    which stopped at the first ancestor with any marker. That returned
    `apps/web/` (where package.json existed) and never reached the parent
    monorepo where `.chameleon/` lived. Net effect: in monorepos, every
    file in workspace subdirs reported `profile_status: no_profile`.

    Returns the marker directory, or None if no marker is found within 32
    parent directories.
    """
    current = file_path.expanduser()
    if current.is_file():
        current = current.parent
    try:
        current = current.resolve()
    except OSError:
        return None

    # Pass 1: deepest .chameleon ancestor wins.
    chameleon_ancestors: list[Path] = []
    walker = current
    for _ in range(32):
        if (walker / ".chameleon").exists():
            chameleon_ancestors.append(walker)
        parent = walker.parent
        if parent == walker:
            break
        walker = parent
    if chameleon_ancestors:
        # First one encountered going up is the deepest.
        return chameleon_ancestors[0]

    # Pass 2: first ancestor with any non-.chameleon marker.
    walker = current
    for _ in range(32):
        for marker in REPO_ROOT_MARKERS:
            if marker == ".chameleon":
                continue
            if (walker / marker).exists():
                return walker
        parent = walker.parent
        if parent == walker:
            break
        walker = parent
    return None


def load_profile_dir(profile_dir: Path) -> LoadedProfile:
    """Load and validate all artifacts from a `.chameleon/` directory.

    Implements the double-fstat pattern: capture mtime tuple before reads,
    verify after reads to detect mid-load mutation.

    Raises:
        ProfileLoadError: missing sentinel, malformed JSON, generation
                          mismatch across files, or mid-load mutation.
    """
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
    profile = json.loads(artifact_paths[0].read_text(encoding="utf-8"))
    archetypes = json.loads(artifact_paths[1].read_text(encoding="utf-8"))
    rules = json.loads(artifact_paths[2].read_text(encoding="utf-8"))
    canonicals = json.loads(artifact_paths[3].read_text(encoding="utf-8"))

    idioms_path = profile_dir / "idioms.md"
    idioms_text = idioms_path.read_text(encoding="utf-8") if idioms_path.exists() else ""

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

    mtime_token = "-".join(str(m) for m in mtimes_after)
    archetype_names = sorted(archetypes.get("archetypes", {}).keys())

    return LoadedProfile(
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
