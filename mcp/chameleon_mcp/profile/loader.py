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

    A "repo root" is the first ancestor directory containing any of:
    .chameleon, .git, package.json, tsconfig.json, Gemfile, pyproject.toml,
    go.mod, or Cargo.toml. Markers are checked in priority order at each
    level — .chameleon wins over .git wins over a language manifest, so a
    bootstrapped repo without .git (subtree, vendored copy, sparse
    checkout, archive extract) still resolves correctly.

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

    for _ in range(32):
        for marker in REPO_ROOT_MARKERS:
            if (current / marker).exists():
                return current
        parent = current.parent
        if parent == current:
            break
        current = parent
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
