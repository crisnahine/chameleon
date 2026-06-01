"""Extractor protocol — language-agnostic interface for AST extraction.

All language extractors (TypeScript, Ruby, etc.) implement this protocol.
The downstream clustering, archetype detection, and rule extraction logic operate
on the normalized ParseResult shape — language details are hidden behind the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ParsedFile:
    """Normalized AST representation of a single source file.

    Stays language-agnostic at the boundary; language-specific node kinds
    are stringified (e.g., "FunctionDeclaration" for TS, "DefNode" for Ruby).
    """

    path: Path
    """Absolute path to the parsed file."""

    content_first_200_bytes: str
    """First 200 bytes of file content for content_signal matching."""

    top_level_node_kinds: tuple[str, ...]
    """Tuple of stringified node kinds for direct children of the root.

    Used as part of the cluster signature function. See docs/architecture.md
    "Cluster signature function" for the full 7-tuple.
    """

    default_export_kind: str | None
    """Kind of the default export (e.g., "FunctionDeclaration", "ClassDeclaration"), or None."""

    named_export_count: int
    """Number of named exports in the file."""

    import_specifiers: tuple[tuple[str, str], ...]
    """Tuple of (module_name, import_kind) where import_kind is "default" | "named" | "namespace".

    Used to compute the import_module_set_hash component of the cluster signature.
    """

    has_jsx: bool
    """Whether the file contains JSX/TSX elements."""

    parse_diagnostics_count: int = 0
    """Number of parse errors. Files with > 20 are skipped per docs/architecture.md."""

    sha_hint: str | None = None
    """xxhash64 hex digest of the file content (for drift cache invalidation)."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Language-specific data that doesn't fit the normalized shape (subprocess-emitted).

    Downstream consumers SHOULD NOT rely on extras for cluster signature inputs;
    only the normalized fields above are part of the stability contract.
    """


@dataclass
class ParseResult:
    """Result of parsing a repo (or a subset).

    files: list of successfully-parsed files
    skipped: list of (path, reason) tuples for files that were skipped
    """

    files: list[ParsedFile] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)


class Extractor(Protocol):
    """Language-agnostic extractor interface.

    Implementations live in language-specific modules (typescript.py, ruby.py).
    """

    language: str
    """Short language identifier ("typescript", "ruby", etc.)."""

    def can_handle(self, repo_root: Path) -> bool:
        """Returns True if this extractor can analyze the given repo.

        Detection signals:
        - typescript: presence of `tsconfig.json` or `package.json` with TS deps
        - ruby: presence of `Gemfile` or `*.gemspec`
        """
        ...

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*",
        limit: int | None = None,
    ) -> ParseResult:
        """Parse all source files in a repo matching the glob.

        Args:
            repo_root: absolute path to repo root
            glob: file glob (default everything)
            limit: optional cap on files to parse

        Returns:
            ParseResult with files + skipped lists.
        """
        ...
