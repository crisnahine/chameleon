"""TypeScript extractor — Phase 1C stub.

Phase 2 implements the full integration with `scripts/ts_dump.mjs`.
Phase 1C stub returns empty ParseResult so the rest of the engine can
be wired end-to-end without TS Compiler integration yet.

Design (Phase 2):
- `scripts/ts_dump.mjs` is a long-lived Node process consuming file paths
  from stdin (NDJSON) and emitting AST extraction results to stdout (NDJSON).
- Worker pool: min(cpu_count // 2, 8) workers for parallelism.
- Each worker has TypeScript Compiler API loaded once (vendored at
  mcp/node_modules/typescript, integrity-verified by typescript-checksums.json).
- Subprocess limits per file: 5s CPU, 512 MB RSS, 1 MB file size, 50k AST nodes.
- Files with > 20 parse diagnostics are skipped.

See ARCHITECTURE.md:
- "TypeScript-first extractor (vendored, integrity-checked)"
- "Performance characteristics" → "ts_dump.mjs batching"
- "Cluster signature function" → "Compiler API mode"
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.extractors._base import Extractor, ParseResult


class TypeScriptExtractor:
    """Phase 1C stub. Phase 2 will implement the real subprocess pool."""

    language = "typescript"

    def can_handle(self, repo_root: Path) -> bool:
        """Detect TS via tsconfig.json or package.json with TS-related deps."""
        if (repo_root / "tsconfig.json").exists():
            return True
        package_json = repo_root / "package.json"
        if package_json.exists():
            content = package_json.read_text(errors="replace")
            return any(token in content for token in ("typescript", '"ts-node"', '"vite"'))
        return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*.{ts,tsx,js,jsx,mjs,cjs}",
        limit: int | None = None,
    ) -> ParseResult:
        """Phase 1C: returns empty result. Phase 2 implements real parsing."""
        # TODO Phase 2: spawn ts_dump.mjs worker pool
        # TODO Phase 2: stream file paths via stdin, collect ParsedFile from stdout
        # TODO Phase 2: respect limit + repo_size_guard (50k post-glob ceiling)
        # TODO Phase 2: skip files with > 20 parse diagnostics
        # TODO Phase 2: compute cluster signature from extracted fields
        # TODO Phase 2: cache sig results in drift.db keyed by (path, sha_hint)
        return ParseResult()


# Verify protocol conformance at import time
_extractor: Extractor = TypeScriptExtractor()
