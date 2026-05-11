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
import shutil
import subprocess
import sys
from pathlib import Path

import xxhash

from chameleon_mcp.extractors._base import Extractor, ParsedFile, ParseResult
from chameleon_mcp.plugin_paths import plugin_root


class TypeScriptExtractor:
    """TypeScript AST extractor backed by ts_dump.mjs subprocess."""

    language = "typescript"

    # Resolved at construction; subprocess spawned lazily on first parse_repo call.
    _ts_dump_script: Path

    def __init__(self, ts_dump_script: Path | None = None) -> None:
        if ts_dump_script is None:
            self._ts_dump_script = plugin_root() / "scripts" / "ts_dump.mjs"
        else:
            self._ts_dump_script = ts_dump_script

    def _ensure_node_modules(self) -> None:
        """Run `npm install` in mcp/ the first time TS extraction is needed.

        Required because uvx-based MCP install does not run the npm step,
        and the TS extractor depends on mcp/node_modules/typescript.
        """
        mcp_dir = plugin_root() / "mcp"
        if (mcp_dir / "node_modules" / "typescript").exists():
            return

        if not shutil.which("npm"):
            raise RuntimeError(
                "chameleon: `npm` not found on PATH. Install Node.js >= 20 "
                "to use the TypeScript extractor."
            )

        print(
            "chameleon: first-run setup — installing Node deps (~10s)...",
            file=sys.stderr,
            flush=True,
        )
        result = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(mcp_dir),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "chameleon: `npm install` failed in "
                f"{mcp_dir}:\n{result.stderr}"
            )

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
        paths: list[Path] | None = None,
    ) -> ParseResult:
        """Parse files under `repo_root`. Returns ParseResult.

        Args:
            repo_root: absolute path to the repo root
            glob: file glob (only used if `paths` not provided)
            limit: optional cap on files to parse
            paths: explicit file list (overrides glob); typically from
                   bootstrap.discovery.discover_files() so exclusion logic
                   stays in one place

        Returns:
            ParseResult with files + skipped lists.
        """
        # 1. Use explicit paths if given (preferred — keeps exclusion logic in
        # bootstrap/discovery.py); else fall back to the local glob.
        if paths is not None:
            files = list(paths)
        else:
            files = list(_expand_glob(repo_root, glob))
        if limit is not None:
            files = files[:limit]
        if not files:
            return ParseResult(files=[], skipped=[])

        # 2. Spawn ts_dump.mjs subprocess
        if not self._ts_dump_script.exists():
            raise FileNotFoundError(
                f"ts_dump.mjs not found at {self._ts_dump_script}; "
                "the plugin install appears incomplete."
            )
        self._ensure_node_modules()
        env = os.environ.copy()
        # NODE_PATH so the script can resolve TypeScript from mcp/node_modules
        env["NODE_PATH"] = str(plugin_root() / "mcp" / "node_modules")

        # Build input as one big string; communicate() handles pipe-deadlock
        # internally via threads (avoids the classic stdout-buffer-full hang).
        input_data = "".join(f"{fp.resolve()}\n" for fp in files)

        proc = subprocess.Popen(
            ["node", str(self._ts_dump_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(plugin_root() / "mcp"),
        )

        # Communicate writes all input + reads all stdout/stderr in
        # background threads; safe for arbitrarily large data.
        # Timeout sized for ~5,000 files at ~75ms each ≈ 6.5min; we cap at
        # 10min for headroom.
        try:
            stdout_data, _stderr = proc.communicate(input=input_data, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, _stderr = proc.communicate()

        # Parse NDJSON output line by line
        results = []
        skipped: list[tuple[Path, str]] = []
        for line in stdout_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = Path(record.get("path", ""))
            if "error" in record:
                skipped.append((path, record["error"]))
                continue
            results.append(_parsed_file_from_record(path, record))

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
    # The double-read (parse in JS, hash in Python) is intentional: ts_dump.mjs
    # stays narrow (it only does what the TypeScript Compiler API gives it for
    # free), and xxhash here is dominated by disk-page-cache reads, not parse
    # cost. If profiling on >5k-file repos eventually shows this is a real
    # bottleneck, push the hash into ts_dump.mjs and stream it back in the
    # NDJSON record instead of re-opening here. No benchmark today says it is.
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
