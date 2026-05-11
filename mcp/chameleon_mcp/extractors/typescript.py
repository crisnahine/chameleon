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
        """Detect TS via tsconfig.json or package.json with TS-related deps.

        BUG-010 (v0.5.6): also accept "any *.ts/*.tsx file in the
        workspace" as a signal. Hoisted-deps monorepos (excalidraw's
        excalidraw-app, Nx-style packages where every TS dep lives at the
        root) have workspaces whose own package.json carries no TS deps
        and whose own dir has no tsconfig — yet the workspace is clearly
        TS. The shallow scan is bounded (depth 3, capped at 50 files)
        so a pathological tree can't hang detection.

        IMPORTANT: the .ts-file fallback is SKIPPED when this directory
        is itself a workspace coordinator (declares ``"workspaces"`` or
        has a sibling ``pnpm-workspace.yaml``) OR carries any of the
        conventional ``apps/`` / ``packages/`` / ``services/`` /
        ``workspaces/`` subdirs that themselves contain a package.json.
        In those cases the orchestrator's per-workspace fanout — not the
        root extractor — should claim the children. Pre-v0.5.6's
        path-only signal naturally returned False at these roots and the
        workspace fanout depended on it.
        """
        if (repo_root / "tsconfig.json").exists():
            return True
        package_json = repo_root / "package.json"
        if package_json.exists():
            try:
                content = package_json.read_text(errors="replace")
            except OSError:
                pass
            else:
                if any(token in content for token in ("typescript", '"ts-node"', '"vite"')):
                    return True
                if '"workspaces"' in content:
                    return False
        if (repo_root / "pnpm-workspace.yaml").exists():
            return False
        # Workspace-shaped subdirs (Turborepo / Nx style) — defer.
        for parent in ("apps", "packages", "services", "workspaces"):
            parent_dir = repo_root / parent
            if not parent_dir.is_dir():
                continue
            try:
                for child in parent_dir.iterdir():
                    if (child / "package.json").is_file():
                        return False
            except (OSError, PermissionError):
                continue
        # BUG-010 fallback: any .ts/.tsx file within depth 3 of the root.
        return _has_typescript_source_files(repo_root, max_depth=3, max_found=50)

    @staticmethod
    def _shallow_ts_scan(repo_root: Path) -> bool:  # pragma: no cover - thin alias
        return _has_typescript_source_files(repo_root, max_depth=3, max_found=50)

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


def _has_typescript_source_files(
    repo_root: Path, *, max_depth: int = 3, max_found: int = 50
) -> bool:
    """Shallow-walk to find any .ts/.tsx file (BUG-010 detection fallback).

    Bounded by depth and total files found so a giant tree can't hang
    detection. Skips conventional ignore dirs to avoid wasting walks on
    node_modules / dist / .git / etc.
    """
    if not repo_root.is_dir():
        return False
    ignore_dirs = {
        ".git",
        ".chameleon",
        "node_modules",
        "dist",
        "build",
        "coverage",
        ".next",
        ".turbo",
        ".cache",
        "__pycache__",
        ".venv",
        "vendor",
    }
    found = 0
    # Walk breadth-first up to max_depth so shallow .ts files are found
    # before paying for any deep descent.
    frontier: list[tuple[Path, int]] = [(repo_root, 0)]
    while frontier:
        next_frontier: list[tuple[Path, int]] = []
        for current, depth in frontier:
            try:
                entries = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                name = entry.name
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if name in ignore_dirs or name.startswith("."):
                        continue
                    if depth + 1 <= max_depth:
                        next_frontier.append((entry, depth + 1))
                else:
                    if name.endswith(".ts") or name.endswith(".tsx"):
                        # One hit is enough — anything > 0 means TS.
                        return True
            if found >= max_found:
                return True
        frontier = next_frontier
    return found > 0


# Verify protocol conformance at import time
_extractor: Extractor = TypeScriptExtractor()
