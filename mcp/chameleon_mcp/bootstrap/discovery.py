"""File discovery for bootstrap.

Walks a repo, applies path-based exclusions, enforces the 50k post-glob
ceiling per Round 2 cost adversary recommendation.

Two exclusion sets:
1. EXCLUDE_FROM_CLUSTERING — paths never analyzed (vendor, build, generated)
2. EXCLUDE_FROM_CANONICAL_POOL — clustered but never picked as canonical
   (tests, legacy, archive, deprecated)

Per docs/architecture.md "Bootstrap interview flow" steps (e), (f).
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

REPO_SIZE_GUARD = 200_000

EXCLUDE_FROM_CLUSTERING_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".parcel-cache",
        "__generated__",
        "generated",
        ".git",
        "storage",
        "tmp",
        "coverage",
        ".coverage",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "__pycache__",
        ".idea",
        ".vscode",
        ".chameleon",
        ".claude",
    }
)

EXCLUDE_FROM_CLUSTERING_FILE_GLOBS = (
    ".DS_Store",
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.lock",
)

EXCLUDE_FROM_CLUSTERING_EXACT_RELPATHS = frozenset(
    {
        "db/schema.rb",
        "db/structure.sql",
    }
)

# Directory-component exclusions for canonical selection. Matched against any
# path segment (top-level OR nested), mirroring EXCLUDE_FROM_CLUSTERING_DIRS.
# fnmatch globs like "**/tests/**" silently miss a top-level "tests/" dir
# (the leading "**/" requires a preceding segment), which is the most common
# layout in TS/JS and Rails repos — so component matching is used instead.
EXCLUDE_FROM_CANONICAL_POOL_DIRS = frozenset(
    {
        "__tests__",
        "test",
        "tests",
        "spec",
        "specs",
        "legacy",
        "archive",
        "_archive",
        ".archive",
        "deprecated",
        "cypress",
        "e2e",
        ".storybook",
    }
)

# Leaf-name filename globs for canonical selection (test/story/fixture files
# that live alongside ordinary source). Matched against the bare filename so
# top-level and nested files are both caught.
EXCLUDE_FROM_CANONICAL_POOL_FILE_GLOBS = (
    "*.test.*",
    "*.spec.*",
    "*.stories.*",
    "*.fixture.*",
)


class TooManyFilesError(Exception):
    """Raised when a repo exceeds REPO_SIZE_GUARD without explicit paths_glob."""

    def __init__(self, count: int, ceiling: int = REPO_SIZE_GUARD) -> None:
        self.count = count
        self.ceiling = ceiling
        super().__init__(
            f"repo has {count} files (ceiling {ceiling}); use explicit paths_glob to scope analysis"
        )


def _matches_any(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Return True if rel_path matches any fnmatch pattern.

    Use this for canonical-pool exclusions (where fnmatch semantics work).
    For directory-component matching, use _has_excluded_component instead.
    """
    return any(fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def _has_excluded_component(rel_path: Path, excluded_dirs: frozenset[str]) -> bool:
    """True if any path component is in the excluded directory denylist."""
    return any(part in excluded_dirs for part in rel_path.parts)


def _matches_filename_glob(name: str, patterns: tuple[str, ...]) -> bool:
    """True if the bare filename matches any leaf-name glob."""
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _glob_candidates(
    repo_root: Path,
    target_glob: str,
    *,
    workspace_roots: list[str] | None = None,
) -> list[Path]:
    """Run the brace-expansion glob against ``repo_root`` (and optional
    workspace sub-roots) and return the union of matched paths.

    Used by both ``discover_files`` and ``discovery_stats`` so the
    pre-exclusion counter and the post-exclusion list always agree on
    what the walker saw.
    """
    bases: list[Path]
    if workspace_roots:
        bases = [(repo_root / ws).resolve() for ws in workspace_roots]
    else:
        bases = [repo_root]

    expanded_globs = _expand_brace_groups(target_glob)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for pattern in expanded_globs:
            for p in base.glob(pattern):
                if p not in seen:
                    seen.add(p)
                    candidates.append(p)
    return candidates


_BRACE_EXPANSION_CAP = 512


def _find_matching_brace(pattern: str, open_idx: int) -> int:
    """Return the index of the `}` matching `pattern[open_idx]`, or -1.

    Walks the pattern from ``open_idx + 1``, tracking nesting depth so
    `{a,{b,c}}` correctly pairs the outermost `}` with the outermost `{`
    instead of the inner one. Returns -1 if unbalanced.
    """
    depth = 1
    i = open_idx + 1
    while i < len(pattern):
        ch = pattern[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_alternatives(body: str) -> list[str]:
    """Split `body` on top-level commas, respecting nested `{...}`.

    `a,{b,c},d` → `["a", "{b,c}", "d"]`, NOT `["a", "{b", "c}", "d"]`.
    """
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    out.append("".join(current))
    return out


def _expand_brace_groups(pattern: str, _depth: int = 0) -> list[str]:
    """Fully expand `{a,b,c}` brace groups in a glob pattern.

    ``"{src,cypress}/**/*.{ts,tsx}"`` → ``["src/**/*.ts", "src/**/*.tsx",
    "cypress/**/*.ts", "cypress/**/*.tsx"]``.

    Uses balanced brace matching so nested groups like `{a,{b,c}}`
    expand correctly. Recursive on the LEFTMOST brace group; each
    alternative is then expanded further. Output is capped at
    ``_BRACE_EXPANSION_CAP`` patterns; pathological inputs fall back
    to the raw pattern (pathlib.glob handles or ignores it gracefully).

    A pattern without braces, with unbalanced braces, or with an empty
    body returns ``[pattern]`` unchanged.
    """
    if _depth > 16:
        return [pattern]
    open_idx = pattern.find("{")
    if open_idx < 0:
        return [pattern]
    close_idx = _find_matching_brace(pattern, open_idx)
    if close_idx < 0 or close_idx <= open_idx + 1:
        return [pattern]
    prefix = pattern[:open_idx]
    body = pattern[open_idx + 1 : close_idx]
    suffix = pattern[close_idx + 1 :]
    alts = [a.strip() for a in _split_top_alternatives(body) if a.strip()]
    if not alts:
        return [pattern]
    out: list[str] = []
    for alt in alts:
        for sub in _expand_brace_groups(prefix + alt + suffix, _depth + 1):
            if sub not in out:
                out.append(sub)
                if len(out) >= _BRACE_EXPANSION_CAP:
                    return out
    return out


def discovery_stats(
    repo_root: Path,
    *,
    glob: str = "**/*.{ts,tsx,js,jsx,mjs,cjs}",
    paths_glob: str | None = None,
    workspace_roots: list[str] | None = None,
) -> dict[str, int]:
    """Return pre- and post-exclusion file counts without raising.

    v0.5.3 (Bug D): instrumentation helper for bootstrap_repo so callers
    can report coverage (how many files were discovered, how many made
    it past the exclusion sets, how many were clustered) without
    re-walking the tree multiple times in different layers.

    Unlike ``discover_files`` this never raises ``TooManyFilesError`` —
    coverage telemetry on an oversized repo is still useful diagnostics.

    Counter semantics (post-rec-13):
    - Symlinks and non-regular files are dropped before either counter
      increments — they are never eligible for clustering, so counting
      them would overstate the discoverable surface.
    - ``pre_exclusion`` counts files that survive the symlink + is_file
      gate but before the EXCLUDE_FROM_CLUSTERING_* sets are applied.
    - ``post_exclusion`` counts files that survive both gates.

    Hardlinks are not detected — that requires same-filesystem write
    access to the repo, which the threat model already assumes a
    trusted user has. The symlink filter targets the cross-filesystem
    teammate-planted-link attack class.

    Returns:
        ``{"pre_exclusion": int, "post_exclusion": int}``.
    """
    target_glob = paths_glob if paths_glob else glob
    candidates = _glob_candidates(repo_root, target_glob, workspace_roots=workspace_roots)

    pre = 0
    post = 0
    for p in candidates:
        if os.path.islink(p):
            continue
        if not p.is_file():
            continue
        pre += 1
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            continue
        if ".." in rel.parts:
            # relative_to() is purely lexical, so a glob that escapes the repo
            # (e.g. paths_glob='../../secrets/**') yields a ../-prefixed rel that
            # is NOT actually under repo_root. Drop it before it reaches the
            # profile (path-traversal guard).
            continue
        if _has_excluded_component(rel, EXCLUDE_FROM_CLUSTERING_DIRS):
            continue
        if _matches_filename_glob(p.name, EXCLUDE_FROM_CLUSTERING_FILE_GLOBS):
            continue
        if rel.as_posix() in EXCLUDE_FROM_CLUSTERING_EXACT_RELPATHS:
            continue
        post += 1
    return {"pre_exclusion": pre, "post_exclusion": post}


def discover_files(
    repo_root: Path,
    *,
    glob: str = "**/*.{ts,tsx,js,jsx,mjs,cjs}",
    paths_glob: str | None = None,
    workspace_roots: list[str] | None = None,
) -> list[Path]:
    """Discover candidate source files in a repo.

    Args:
        repo_root: absolute path to the repo root
        glob: default file glob (TS/JS variants); ignored if paths_glob given
        paths_glob: user-supplied scope override (per architecture "with globs:
                    still enforce 50k post-glob count")
        workspace_roots: v0.5.3 (Bug B) optional list of repo-relative
                    workspace dirs (e.g. ``["apps/web", "apps/api"]``).
                    When provided, the walker scans only inside those dirs
                    (avoiding the empty monorepo root + unrelated siblings).
                    Used by the orchestrator's monorepo path-down detection.

    Returns:
        List of absolute Paths, with EXCLUDE_FROM_CLUSTERING_PATTERNS already removed.
        Order: sorted lexicographically (deterministic for clustering stability).

    Raises:
        TooManyFilesError: if post-exclusion count exceeds REPO_SIZE_GUARD.
    """
    target_glob = paths_glob if paths_glob else glob
    candidates = _glob_candidates(repo_root, target_glob, workspace_roots=workspace_roots)

    filtered: list[Path] = []
    for p in candidates:
        if os.path.islink(p):
            continue
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(repo_root)
        except ValueError:
            continue
        if ".." in rel.parts:
            # relative_to() is purely lexical, so a glob that escapes the repo
            # (e.g. paths_glob='../../secrets/**') yields a ../-prefixed rel that
            # is NOT actually under repo_root. Drop it before it reaches the
            # profile (path-traversal guard).
            continue
        if _has_excluded_component(rel, EXCLUDE_FROM_CLUSTERING_DIRS):
            continue
        if _matches_filename_glob(p.name, EXCLUDE_FROM_CLUSTERING_FILE_GLOBS):
            continue
        if rel.as_posix() in EXCLUDE_FROM_CLUSTERING_EXACT_RELPATHS:
            continue
        filtered.append(p)

    if len(filtered) > REPO_SIZE_GUARD:
        raise TooManyFilesError(len(filtered))

    filtered.sort()
    return filtered


def is_eligible_as_canonical(rel_path: str) -> bool:
    """Return True if a file may be picked as a canonical witness.

    Files in test/, legacy/, archive/, etc. are excluded from canonical
    selection but remain eligible for clustering. Directory exclusions match
    any path component (top-level OR nested) — a top-level "tests/" dir is
    just as disqualifying as a nested "src/tests/" one. Test/story/fixture
    files are excluded by their leaf filename.
    """
    if _has_excluded_component(Path(rel_path), EXCLUDE_FROM_CANONICAL_POOL_DIRS):
        return False
    if _matches_any(Path(rel_path).name, EXCLUDE_FROM_CANONICAL_POOL_FILE_GLOBS):
        return False
    return True


def is_likely_generated(content_first_200_bytes: str) -> bool:
    """Heuristic: True if file content starts with a generated-code marker.

    Common patterns:
    - `// Code generated by ... DO NOT EDIT.` (gRPC, protobuf, etc.)
    - `# Generated by ...` (Python codegen)
    - `/* eslint-disable */ // ... auto-generated ...`
    - `// @generated SignedSource<<...>>` (Meta tooling)

    Phase 2B keeps this minimal; Phase 4 adds .gitattributes linguist-generated=true.
    """
    head = content_first_200_bytes.lower()
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
