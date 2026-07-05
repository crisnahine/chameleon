"""TypeScript extractor — Phase 2A real implementation.

Spawns `scripts/ts_dump.mjs` as a long-lived Node subprocess, sends file
paths via stdin (one per line), reads NDJSON ParsedFile records from stdout.

Phase 2A scope:
- Single-process worker (one ts_dump.mjs subprocess) for simplicity.
- Phase 2B will add the worker pool (cpu_count // 2 workers) for parallelism.

Per docs/architecture.md "TypeScript-first extractor" + "Performance characteristics".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import xxhash

from chameleon_mcp.extractors._base import ExtractorUnavailableError, ParsedFile, ParseResult
from chameleon_mcp.plugin_paths import plugin_root


class NodeUnavailableError(ExtractorUnavailableError):
    """Node.js / npm (or an installed node_modules) is unavailable.

    Raised by the TypeScript extractor when its node deps cannot be
    provisioned. The bootstrap orchestrator catches it (via
    ``ExtractorUnavailableError``) and degrades to a
    ``failed_node_unavailable`` report instead of aborting the whole run.
    """


class TypeScriptExtractor:
    """TypeScript AST extractor backed by ts_dump.mjs subprocess."""

    language = "typescript"

    _ts_dump_script: Path

    def __init__(self, ts_dump_script: Path | None = None) -> None:
        if ts_dump_script is None:
            self._ts_dump_script = plugin_root() / "scripts" / "ts_dump.mjs"
        else:
            self._ts_dump_script = ts_dump_script

    @staticmethod
    def _node_modules_ready(node_modules: Path) -> bool:
        """True only when the real ``require('typescript')`` entry module exists.

        ``npm ci`` builds the tree in place and is NOT atomic: the
        ``typescript/`` directory appears tens of ms before ``lib/typescript.js``
        (the package ``main``) is written. Gating on the bare directory would
        hand a concurrent reader a half-written tree, so check the actual file
        that ``require('typescript')`` loads.
        """
        return (node_modules / "typescript" / "lib" / "typescript.js").is_file()

    def _node_modules_dir(self) -> Path:
        """Per-user, version-scoped install dir for the TS extractor's node deps.

        Lives under the writable chameleon data dir (NOT the read-only,
        rebuilt-per-version plugin cache), so TS extraction survives
        locked-down installs, offline upgrades, and plugin-cache pruning.
        Version-scoped so an upgrade gets a clean install rather than reusing
        a stale TypeScript.
        """
        from chameleon_mcp import __version__
        from chameleon_mcp.plugin_paths import plugin_data_dir

        return plugin_data_dir() / "node-deps" / __version__

    def _ensure_node_modules(self) -> Path:
        """Provision the TS extractor's node deps. Return the node_modules path.

        Resolution order:
          1. Already installed in the per-user data dir -> use it.
          2. Legacy/dev location (``<plugin>/mcp/node_modules``) already has
             it -> use it read-only (we never write into the plugin dir).
          3. Install into the data dir via ``npm ci`` (lockfile) /
             ``npm install``.

        Required because the uvx-based MCP install does not run an npm step.
        Raises ``NodeUnavailableError`` if npm is absent or the install fails.
        """
        data_node_modules = self._node_modules_dir() / "node_modules"
        if self._node_modules_ready(data_node_modules):
            return data_node_modules

        legacy_node_modules = plugin_root() / "mcp" / "node_modules"
        if self._node_modules_ready(legacy_node_modules):
            return legacy_node_modules

        if not shutil.which("npm"):
            raise NodeUnavailableError(
                "chameleon: `npm` not found on PATH. Install Node.js >= 20 "
                "to use the TypeScript extractor."
            )

        from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

        target = self._node_modules_dir()
        node_deps_root = target.parent
        try:
            node_deps_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NodeUnavailableError(
                f"chameleon: node-deps dir is not writable ({node_deps_root}): {exc}"
            ) from exc

        # Version-scope the lock: different plugin versions install disjoint
        # target dirs, so they must not block each other; same-version installs
        # still serialize on this file. (target.name == the plugin version.)
        lock_path = node_deps_root / f".install-{target.name}.lock"
        # Wait at least as long as a peer's worst-case hold: `npm ci` (300s) +
        # the `npm install` fallback (300s), plus margin, so we don't give up
        # while a legitimate install is still running.
        deadline = time.monotonic() + 660.0
        while True:
            # A peer may have finished installing since we last looked.
            if self._node_modules_ready(data_node_modules):
                return data_node_modules
            try:
                with acquire_advisory_lock(lock_path):
                    # Re-check under the lock: the previous holder may have just
                    # completed the install.
                    if self._node_modules_ready(data_node_modules):
                        return data_node_modules
                    self._run_npm_install(target)
                    self._prune_stale_node_deps()
                    return data_node_modules
            except LockHeldError:
                # A live peer is installing into the shared dir. Wait for it
                # rather than racing a destructive `npm ci` that wipes the tree
                # the peer (or a reader) is mid-use.
                if time.monotonic() > deadline:
                    raise NodeUnavailableError(
                        "chameleon: timed out waiting for a concurrent Node "
                        f"dependency install at {data_node_modules}."
                    ) from None
                time.sleep(0.5)
            except OSError as exc:
                raise NodeUnavailableError(
                    f"chameleon: node-deps dir is not writable ({node_deps_root}): {exc}"
                ) from exc

    def _run_npm_install(self, target: Path) -> None:
        """Install node deps into a private staging dir, then atomically swap the
        completed tree into ``target``. The caller holds the install lock.

        Installing into staging + ``os.rename`` (atomic on the same filesystem)
        means a lock-free reader — which gates on
        ``target/node_modules/typescript/lib/typescript.js`` — only ever sees the
        old (absent) state or the complete tree, never a half-written one. Raises
        ``NodeUnavailableError`` on any failure; leaves no staging dir behind.
        """
        node_deps_root = target.parent
        staging = node_deps_root / f"{target.name}.staging-{os.getpid()}"
        try:
            try:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True, exist_ok=True)
                src_mcp = plugin_root() / "mcp"
                for fname in ("package.json", "package-lock.json"):
                    src = src_mcp / fname
                    if src.is_file():
                        shutil.copy2(src, staging / fname)
            except OSError as exc:
                raise NodeUnavailableError(
                    f"chameleon: could not seed node-deps staging dir {staging}: {exc}"
                ) from exc

            has_lock = (staging / "package-lock.json").is_file()
            base_cmd = ["npm", "ci"] if has_lock else ["npm", "install"]
            print(
                "chameleon: first-run setup — installing Node deps (~10s)...",
                file=sys.stderr,
                flush=True,
            )
            try:
                result = subprocess.run(
                    [*base_cmd, "--no-audit", "--no-fund"],
                    cwd=str(staging),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0 and has_lock:
                    # `npm ci` aborts if the lockfile drifts from package.json;
                    # fall back to a plain install once before giving up.
                    result = subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund"],
                        cwd=str(staging),
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise NodeUnavailableError(
                    f"chameleon: npm could not be launched or timed out: {exc}"
                ) from exc
            if result.returncode != 0:
                raise NodeUnavailableError(
                    f"chameleon: Node dependency install failed in {staging}:\n{result.stderr}"
                )
            if not self._node_modules_ready(staging / "node_modules"):
                # Install reported success but the dep is missing — usually means
                # the plugin's mcp/package.json couldn't be found to seed the copy.
                raise NodeUnavailableError(
                    "chameleon: Node dependency install completed but 'typescript' "
                    f"is missing under {staging / 'node_modules'}. Verify the "
                    "plugin's mcp/package.json is present."
                )

            # Atomically promote the completed staging tree into place.
            try:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                os.rename(staging, target)
            except OSError as exc:
                raise NodeUnavailableError(
                    f"chameleon: could not finalize node deps at {target}: {exc}"
                ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _prune_stale_node_deps(self) -> None:
        """Best-effort removal of ``node-deps`` dirs from other plugin versions.

        Keeps the per-user data dir from accumulating stale node_modules trees
        (tens of MB each) across upgrades. Never raises.
        """
        from chameleon_mcp import __version__

        root = self._node_modules_dir().parent
        try:
            children = list(root.iterdir())
        except OSError:
            return
        now = time.time()
        # Don't delete a sibling that may still be live for another plugin
        # version's running ts_dump.mjs (readers hold no lock). Only reclaim
        # dirs untouched for a generous window; the disk cost of the lag is
        # negligible next to corrupting an in-use tree.
        ttl_seconds = 7 * 24 * 3600
        for child in children:
            try:
                if child.name == __version__ or not child.is_dir():
                    continue
                if (now - child.stat().st_mtime) < ttl_seconds:
                    continue
                shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def can_handle(self, repo_root: Path) -> bool:
        """Detect TS via tsconfig.json or package.json with TS-related deps.

        BUG-010: also accept "any *.ts/*.tsx file in the
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
        root extractor — should claim the children. An earlier
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
        return _has_typescript_source_files(repo_root, max_depth=3)

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
        if paths is not None:
            files = list(paths)
        else:
            files = list(_expand_glob(repo_root, glob))
        if limit is not None:
            files = files[:limit]
        if not files:
            return ParseResult(files=[], skipped=[])

        if not self._ts_dump_script.exists():
            raise NodeUnavailableError(
                f"ts_dump.mjs not found at {self._ts_dump_script}; "
                "the plugin install appears incomplete."
            )
        node_modules = self._ensure_node_modules()
        if not shutil.which("node"):
            raise NodeUnavailableError(
                "chameleon: `node` not found on PATH. Install Node.js >= 20 "
                "to use the TypeScript extractor."
            )
        env = os.environ.copy()
        # Defense-in-depth, matching the Ruby/Python extractors: drop the Node
        # interpreter-option vars (the analogues of RUBYOPT / PYTHONSTARTUP) so a
        # poisoned NODE_OPTIONS=--require ... can't preload code before
        # ts_dump.mjs runs. NODE_PATH is load-bearing (it points Node at the
        # bundled node_modules) and is set explicitly below, so it is NOT scrubbed.
        env.pop("NODE_OPTIONS", None)
        env.pop("NODE_REPL_EXTERNAL_MODULE", None)
        env["NODE_PATH"] = str(node_modules)
        env["CHAMELEON_NODE_MODULES"] = str(node_modules)

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

        timed_out = False
        try:
            stdout_data, _stderr = proc.communicate(input=input_data, timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_data, _stderr = proc.communicate()
            timed_out = True

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
            try:
                results.append(_parsed_file_from_record(path, record))
            except (ValueError, TypeError):
                # One malformed record must skip that file, not abort the whole
                # corpus (mirrors the per-line JSONDecodeError skip above).
                skipped.append((path, "malformed_record"))
                continue

        # A timeout or non-zero exit means files past the failure point never
        # reached stdout. Mark them skipped so a truncated sample is VISIBLE
        # instead of being silently treated as the whole corpus.
        rc = proc.returncode
        if timed_out or rc not in (0, None):
            seen = {str(pf.path) for pf in results} | {str(p) for p, _ in skipped}
            reason = "extractor_timeout" if timed_out else f"extractor_exit_{rc}"
            for fp in files:
                rp = str(fp.resolve())
                if rp not in seen:
                    skipped.append((Path(rp), reason))

        return ParseResult(files=results, skipped=skipped)


def _expand_glob(root: Path, glob: str) -> list[Path]:
    """Minimal expansion of a `**/*.{a,b}`-style glob.

    Python's pathlib.Path.glob does not support brace expansion natively, so we
    expand `{...}` alternatives into multiple globs manually.

    Phase 2A scope: handles a single brace alternation. Phase 2B may switch to
    `pathspec` or `wcmatch` for fuller .gitignore-style semantics.
    """
    if "{" in glob and "}" in glob:
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
        import_specifiers=tuple((str(m), str(k)) for m, k in record.get("import_specifiers", [])),
        has_jsx=bool(record.get("has_jsx", False)),
        parse_diagnostics_count=int(record.get("parse_diagnostics_count", 0)),
        sha_hint=sha_hint,
        extras=_extras_from_record(record),
    )


def _extras_from_record(record: dict) -> dict:
    """Carry subprocess-only fields that don't map onto a normalized ParsedFile slot.

    ``function_scopes`` is the per-function body-shape measurement
    (line span, nesting depth, branch and parameter counts) feeding the
    per-archetype body_shape norms. ``callable_signatures`` is the per-callable
    declaration header (name, param shape, default-export flag) feeding the
    per-archetype signature consensus. Both are deliberately kept OUT of the
    cluster signature so they can't perturb signature stability.
    """
    extras: dict = {}
    scopes = record.get("function_scopes")
    if isinstance(scopes, list) and scopes:
        extras["function_scopes"] = scopes
    signatures = record.get("callable_signatures")
    if isinstance(signatures, list) and signatures:
        extras["callable_signatures"] = signatures
    # Per-class decorator + heritage shape feeding the class-contract convention.
    class_shapes = record.get("class_shapes")
    if isinstance(class_shapes, list) and class_shapes:
        extras["class_shapes"] = class_shapes
    # Per-class instance-property declared types, so the calls index can resolve
    # a `this.<prop>.<method>()` (DI / typed field) edge through the property's
    # type to the concrete callee.
    class_property_types = record.get("class_property_types")
    if isinstance(class_property_types, list) and class_property_types:
        extras["class_property_types"] = class_property_types
    # Named export bindings + the open-set flag drive the phantom-symbol index.
    # `named_export_names` is the full set of importable names; `export_set_open`
    # is True when the file does `export * from` and its export set can't be
    # enumerated statically, so the symbol check skips imports from it.
    names = record.get("named_export_names")
    if isinstance(names, list) and names:
        extras["named_export_names"] = [str(n) for n in names if isinstance(n, str)]
    if record.get("export_set_open"):
        extras["export_set_open"] = True
    # Named-import bindings (exported name + local binding + source module +
    # line) drive the reverse index symbol -> importers (keyed on `name`) and
    # the calls index import grade (matched on `local`). Only well-formed rows
    # survive; a malformed entry is dropped rather than aborting the file's
    # record. A missing/invalid `local` (old dump) falls back to the exported
    # name, which is also the local binding when no `as` alias is present.
    import_symbols = record.get("import_symbols")
    if isinstance(import_symbols, list) and import_symbols:
        rows: list[dict] = []
        for sym in import_symbols:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name")
            module = sym.get("module")
            if not isinstance(name, str) or not isinstance(module, str):
                continue
            local = sym.get("local")
            line = sym.get("line")
            rows.append(
                {
                    "name": name,
                    "local": local if isinstance(local, str) and local else name,
                    "module": module,
                    "line": int(line) if isinstance(line, int) else None,
                }
            )
        if rows:
            extras["import_symbols"] = rows
    # Named re-export edges (`export { origin as exported } from 'module'`) feed
    # the build-time barrel-chase: an import/call of `exported` through this file
    # is attributed to the file that DEFINES `origin`, not this re-export barrel.
    # Same drop-malformed-row stance as import_symbols.
    re_exports = record.get("re_exports")
    if isinstance(re_exports, list) and re_exports:
        re_rows: list[dict] = []
        for rex in re_exports:
            if not isinstance(rex, dict):
                continue
            exported = rex.get("exported")
            origin = rex.get("origin")
            module = rex.get("module")
            if not (
                isinstance(exported, str) and isinstance(origin, str) and isinstance(module, str)
            ):
                continue
            line = rex.get("line")
            re_rows.append(
                {
                    "exported": exported,
                    "origin": origin,
                    "module": module,
                    "line": int(line) if isinstance(line, int) else None,
                }
            )
        if re_rows:
            extras["re_exports"] = re_rows
    # Call sites + runtime namespace imports feed the calls-index builder
    # (caller -> callee edges). Row-level validation lives in the builder,
    # which skips anything malformed, so the lists are carried as-is.
    call_sites = record.get("call_sites")
    if isinstance(call_sites, list) and call_sites:
        extras["call_sites"] = call_sites
    call_sites_total = record.get("call_sites_total")
    if isinstance(call_sites_total, int):
        extras["call_sites_total"] = call_sites_total
    call_sites_truncated = record.get("call_sites_truncated")
    if call_sites_truncated:
        extras["call_sites_truncated"] = True
    namespace_imports = record.get("namespace_imports")
    if isinstance(namespace_imports, list) and namespace_imports:
        extras["namespace_imports"] = namespace_imports
    return extras


def _has_typescript_source_files(repo_root: Path, *, max_depth: int = 3) -> bool:
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
                        return True
        frontier = next_frontier
    return False
