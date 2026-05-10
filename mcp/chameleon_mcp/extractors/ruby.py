"""Ruby AST extractor — Phase 8 (v1.5) scaffold.

Phase 1C-2D ship TypeScript only. Phase 8 wires Ruby support:
- Vendored Prism gem (Ruby AST library) at ruby/vendor/bundle/ or via
  rbenv-managed gemset (decision deferred to Phase 8 implementation)
- scripts/prism_dump.rb: long-lived Ruby process consuming file paths
  from stdin, emitting NDJSON ParsedFile records to stdout
- Detection: presence of `Gemfile` or `*.gemspec`

This module is the placeholder. The Phase 8 implementation will mirror
typescript.py's structure with `prism_dump.rb` substituted for `ts_dump.mjs`.

Per ADR-0003: TypeScript only in v1.0; Ruby in v1.5 after engine
validation gate passes.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.extractors._base import Extractor, ParseResult


class RubyExtractor:
    """Phase 8 (v1.5) placeholder. Real implementation lands after v1.0 ships."""

    language = "ruby"

    def can_handle(self, repo_root: Path) -> bool:
        """Detect Ruby via Gemfile or *.gemspec."""
        if (repo_root / "Gemfile").exists():
            return True
        return any(repo_root.glob("*.gemspec"))

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*.rb",
        limit: int | None = None,
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Phase 8 placeholder. Returns empty ParseResult.

        Phase 8 implementation will:
        - Spawn `scripts/prism_dump.rb` long-lived Ruby subprocess
        - Send paths via stdin, collect NDJSON via communicate() (mirrors
          typescript.py Phase 5 pipe-deadlock fix)
        - Convert records to ParsedFile dataclasses
        - Map Ruby AST node kinds to the same normalized shape used by TS
          (so the cluster signature function is language-agnostic at the
          Python layer)
        """
        del repo_root, glob, limit, paths
        return ParseResult(files=[], skipped=[])


# Verify protocol conformance at import time
_extractor: Extractor = RubyExtractor()
