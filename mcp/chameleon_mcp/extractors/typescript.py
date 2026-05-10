"""TypeScript extractor — Phase 2A real implementation.

Spawns `scripts/ts_dump.mjs` as a long-lived Node subprocess, sends file
paths via stdin (one per line), reads NDJSON ParsedFile records from stdout.

Phase 2A scope:
- Single-process worker (one ts_dump.mjs subprocess) for simplicity.
- Phase 2B will add the worker pool (cpu_count // 2 workers) for parallelism.

Per ARCHITECTURE.md "TypeScript-first extractor" + "Performance characteristics".
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import xxhash
from pathlib import Path

from chameleon_mcp.extractors._base import Extractor, ParsedFile, ParseResult


class TypeScriptExtractor:
    """TypeScript AST extractor backed by ts_dump.mjs subprocess."""

    language = "typescript"

    # Resolved at construction; subprocess spawned lazily on first parse_repo call.
    _ts_dump_script: Path

    def __init__(self, ts_dump_script: Path | None = None) -> None:
        # Default: scripts/ts_dump.mjs at the plugin root (sibling of mcp/ dir)
        if ts_dump_script is None:
            # mcp/chameleon_mcp/extractors/typescript.py → ../../../scripts/ts_dump.mjs
            here = Path(__file__).resolve()
            self._ts_dump_script = (
                here.parent.parent.parent.parent / "scripts" / "ts_dump.mjs"
            )
        else:
            self._ts_dump_script = ts_dump_script

    def can_handle(self, repo_root: Path) -> bool:
        """Detect TS via tsconfig.json or package.json with TS-related deps."""
        if (repo_root / "tsconfig.json").exists():
            return True
        package_json = repo_root / "package.json"
        if package_json.exists():
            try:
                content = package_json.read_text(errors="replace")
            except OSError:
                return False
            return any(token in content for token in ("typescript", '"ts-node"', '"vite"'))
        return False

    def parse_repo(
        self,
        repo_root: Path,
        glob: str = "**/*.{ts,tsx,js,jsx,mjs,cjs}",
        limit: int | None = None,
    ) -> ParseResult:
        """Parse files matching `glob` under `repo_root`. Returns ParseResult."""
        # 1. Discover files (supports glob alternation via expansion)
        files = list(_expand_glob(repo_root, glob))
        if limit is not None:
            files = files[:limit]
        if not files:
            return ParseResult(files=[], skipped=[])

        # 2. Spawn ts_dump.mjs subprocess
        if not self._ts_dump_script.exists():
            raise FileNotFoundError(
                f"ts_dump.mjs not found at {self._ts_dump_script}; "
                "run `cd mcp && npm install` to set up TS Compiler."
            )
        env = os.environ.copy()
        # NODE_PATH so the script can resolve TypeScript from mcp/node_modules
        env["NODE_PATH"] = str(self._ts_dump_script.parent.parent / "mcp" / "node_modules")

        proc = subprocess.Popen(
            ["node", str(self._ts_dump_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            env=env,
            cwd=str(self._ts_dump_script.parent.parent / "mcp"),
        )

        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("ts_dump.mjs subprocess pipes failed to attach")

        # 3. Send file paths and collect results
        try:
            for fp in files:
                proc.stdin.write(f"{fp.resolve()}\n")
            proc.stdin.flush()
            proc.stdin.close()  # signal EOF; subprocess processes remaining + exits

            # Collect results (one NDJSON line per input file path; order preserved)
            results = []
            skipped: list[tuple[Path, str]] = []
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Defensive: skip malformed output but don't abort
                    continue
                path = Path(record.get("path", ""))
                if "error" in record:
                    skipped.append((path, record["error"]))
                    continue
                results.append(_parsed_file_from_record(path, record))

            # Wait for subprocess to terminate cleanly (with a short timeout)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass

        return ParseResult(files=results, skipped=skipped)


def _expand_glob(root: Path, glob: str) -> list[Path]:
    """Minimal expansion of a `**/*.{a,b}`-style glob.

    Python's pathlib.Path.glob does not support brace expansion natively, so we
    expand `{...}` alternatives into multiple globs manually.

    Phase 2A scope: handles a single brace alternation. Phase 2B may switch to
    `pathspec` or `wcmatch` for fuller .gitignore-style semantics.
    """
    if "{" in glob and "}" in glob:
        # Expand `prefix{a,b,c}suffix` → ['prefixasuffix', 'prefixbsuffix', ...]
        prefix, _, rest = glob.partition("{")
        body, _, suffix = rest.partition("}")
        alts = [a.strip() for a in body.split(",")]
        all_paths: list[Path] = []
        seen: set[Path] = set()
        for alt in alts:
            for p in root.glob(f"{prefix}{alt}{suffix}"):
                if p not in seen:
                    seen.add(p)
                    all_paths.append(p)
        return all_paths
    return list(root.glob(glob))


def _parsed_file_from_record(path: Path, record: dict) -> ParsedFile:
    """Convert ts_dump.mjs NDJSON record into a ParsedFile dataclass.

    Computes sha_hint (xxhash64) on the Python side to keep ts_dump.mjs lean.
    """
    # Compute sha_hint by re-reading the file (hot path; could be deferred to
    # Phase 2B if perf testing shows this is a bottleneck).
    try:
        sha_hint = xxhash.xxh64(path.read_bytes()).hexdigest()
    except OSError:
        sha_hint = None

    return ParsedFile(
        path=path,
        content_first_200_bytes=record.get("content_first_200_bytes", ""),
        top_level_node_kinds=tuple(record.get("top_level_node_kinds", [])),
        default_export_kind=record.get("default_export_kind"),
        named_export_count=int(record.get("named_export_count", 0)),
        import_specifiers=tuple(
            (str(m), str(k)) for m, k in record.get("import_specifiers", [])
        ),
        has_jsx=bool(record.get("has_jsx", False)),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count", 0)),
        sha_hint=sha_hint,
    )


# Verify protocol conformance at import time
_extractor: Extractor = TypeScriptExtractor()
