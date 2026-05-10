"""File discovery for bootstrap.

Walks a repo, applies path-based exclusions, enforces the 50k post-glob
ceiling per Round 2 cost adversary recommendation.

Two exclusion sets:
1. EXCLUDE_FROM_CLUSTERING — paths never analyzed (vendor, build, generated)
2. EXCLUDE_FROM_CANONICAL_POOL — clustered but never picked as canonical
   (tests, legacy, archive, deprecated)

Per ARCHITECTURE.md "Bootstrap interview flow" steps (e), (f).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Hard ceiling on file count post-exclusion. Bootstrap refuses above this
# without explicit user-supplied paths_glob.
REPO_SIZE_GUARD = 50_000

# Paths skipped entirely from clustering (architecture's exclusion list +
# common build/vendor patterns).
EXCLUDE_FROM_CLUSTERING_PATTERNS = (
    "**/node_modules/**",
    "**/vendor/**",
    "**/dist/**",
    "**/build/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/.turbo/**",
    "**/.cache/**",
    "**/.parcel-cache/**",
    "**/__generated__/**",
    "**/generated/**",
    "**/.git/**",
    "**/storage/**",
    "**/tmp/**",
    "**/coverage/**",
    "**/.coverage/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.mypy_cache/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/.idea/**",
    "**/.vscode/**",
    "**/.DS_Store",
)

# Paths INCLUDED in clustering but EXCLUDED from canonical pool.
# Per ARCHITECTURE.md: tests, legacy, archive directories shouldn't become
# the team's "this is how we do it" reference.
EXCLUDE_FROM_CANONICAL_POOL_PATTERNS = (
    "**/__tests__/**",
    "**/test/**",
    "**/tests/**",
    "**/spec/**",
    "**/specs/**",
    "**/legacy/**",
    "**/archive/**",
    "**/_archive/**",
    "**/.archive/**",
    "**/deprecated/**",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*.stories.*",
    "**/*.fixture.*",
    "**/cypress/**",
    "**/e2e/**",
    "**/.storybook/**",
)


class TooManyFilesError(Exception):
    """Raised when a repo exceeds REPO_SIZE_GUARD without explicit paths_glob."""

    def __init__(self, count: int, ceiling: int = REPO_SIZE_GUARD) -> None:
        self.count = count
        self.ceiling = ceiling
        super().__init__(
            f"repo has {count} files (ceiling {ceiling}); "
            "use explicit paths_glob to scope analysis"
        )


def _matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Return True if rel_path matches any glob pattern."""
    return any(fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def discover_files(
    repo_root: Path,
    *,
    glob: str = "**/*.{ts,tsx,js,jsx,mjs,cjs}",
    paths_glob: str | None = None,
) -> list[Path]:
    """Discover candidate source files in a repo.

    Args:
        repo_root: absolute path to the repo root
        glob: default file glob (TS/JS variants); ignored if paths_glob given
        paths_glob: user-supplied scope override (per architecture "with globs:
                    still enforce 50k post-glob count")

    Returns:
        List of absolute Paths, with EXCLUDE_FROM_CLUSTERING_PATTERNS already removed.
        Order: sorted lexicographically (deterministic for clustering stability).

    Raises:
        TooManyFilesError: if post-exclusion count exceeds REPO_SIZE_GUARD.
    """
    target_glob = paths_glob if paths_glob else glob

    # Reuse the brace-expansion helper from extractors.typescript._expand_glob
    # by inlining the same logic here (avoids circular import).
    if "{" in target_glob and "}" in target_glob:
        prefix, _, rest = target_glob.partition("{")
        body, _, suffix = rest.partition("}")
        alts = [a.strip() for a in body.split(",")]
        candidates: list[Path] = []
        seen: set[Path] = set()
        for alt in alts:
            for p in repo_root.glob(f"{prefix}{alt}{suffix}"):
                if p not in seen:
                    seen.add(p)
                    candidates.append(p)
    else:
        candidates = list(repo_root.glob(target_glob))

    # Apply path-based exclusions
    filtered: list[Path] = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(repo_root))
        except ValueError:
            continue
        if _matches_any(rel, EXCLUDE_FROM_CLUSTERING_PATTERNS):
            continue
        filtered.append(p)

    # Repo-size ceiling
    if len(filtered) > REPO_SIZE_GUARD:
        raise TooManyFilesError(len(filtered))

    # Deterministic order for clustering stability
    filtered.sort()
    return filtered


def is_eligible_as_canonical(rel_path: str) -> bool:
    """Return True if a file may be picked as a canonical witness.

    Files in test/, legacy/, archive/, etc. are excluded from canonical
    selection but remain eligible for clustering.
    """
    return not _matches_any(rel_path, EXCLUDE_FROM_CANONICAL_POOL_PATTERNS)


def is_likely_generated(content_first_512_bytes: str) -> bool:
    """Heuristic: True if file content starts with a generated-code marker.

    Common patterns:
    - `// Code generated by ... DO NOT EDIT.` (gRPC, protobuf, etc.)
    - `# Generated by ...` (Python codegen)
    - `/* eslint-disable */ // ... auto-generated ...`
    - `// @generated SignedSource<<...>>` (Meta tooling)

    Phase 2B keeps this minimal; Phase 4 adds .gitattributes linguist-generated=true.
    """
    head = content_first_512_bytes.lower()
    markers = (
        "code generated by",
        "do not edit",
        "@generated",
        "auto-generated",
        "autogenerated",
        "this file was generated",
        "this file is generated",
    )
    return any(m in head for m in markers)
