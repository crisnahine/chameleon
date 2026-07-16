"""Exported-symbol index and caller reverse-index for the symbol checks.

Phantom-import only checks that an import PATH resolves to a file on disk; it
never checks the named binding is actually exported. So
``import { fetchUser } from './api'`` passes when ``api.ts`` exports only
``getUser``, and a hallucinated helper that resolves to a real file but is not
exported ships as a silent no-op.

This module builds and reads a committed ``exports_index.json`` mapping each
repo-relative TypeScript/JS source path to the set of names it exports. The
phantom-import pass resolves a specifier to a concrete on-disk file (it already
does this for the path check), looks that file up here, and flags any named
specifier absent from the exported set.

It also builds a committed ``reverse_index.json`` -- the inverse view:
exported-name -> the files that import it by name, with the import line. That
backs two cross-file checks the per-file lint is blind to:

- An edit-time advisory: editing a module that exports ``editPrice`` can note
  "N files import editPrice from this module" purely from the prebuilt index,
  with no re-parse of any caller on the hot path.
- An existence-break query: when a module no longer exports a name an indexed
  importer still references, the importer's call site is now broken. The query
  consumes the prebuilt index plus the module's CURRENT export set and returns
  the concrete ``(importer, line)`` witnesses.

Two halves live here per index so the build (bootstrap-time, populates the
artifact) and the read (hot-path / query, consumes it) share one key scheme and
can't drift:

- :func:`build_exports_index` / :func:`build_reverse_index` turn parsed files
  into the artifact payloads.
- :func:`load_exports_index` / :func:`load_reverse_index` read the committed
  artifacts, cached on mtime so a mid-session refresh is picked up without
  re-reading every call.

Conservative by construction. A file that does ``export * from`` cannot have its
export set enumerated statically (the star pulls in an unknown set from another
module), so its entry is marked OPEN and the symbol check skips every import
from it -- barrel/index files are the dominant false-positive source and this
mirrors the path check's skip-on-ambiguity stance. A target absent from the
index (edited this turn, generated, ambient ``.d.ts``) is also skipped. The
reverse index keys on the SAME repo-relative target path the exports index uses,
so a name and its importers always agree on which file "this module" is.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int

EXPORTS_INDEX_FILENAME = "exports_index.json"
REVERSE_INDEX_FILENAME = "reverse_index.json"
# v2: reverse-index importer rows may carry an optional ``via`` chain (the barrel
# files a through-barrel edge was chased across at build time). Both index halves
# share this constant so they can never drift on the shape question that matters
# (via support); it is what build_exports_index / build_reverse_index stamp on
# every write.
SCHEMA_VERSION = 2
# A v1 artifact is still safe to READ under the current parser: v1 predates the
# reverse-index ``via`` breadcrumb, and every row-level read already treats
# ``via`` as optional (falls back to an empty tuple), so a v1 reverse index loads
# with no barrel-chase attribution -- accurate, since it was built before that
# feature existed. The exports-index payload never gained a v2-only field at all
# (only reverse_index's row shape did), so a v1 exports index is byte-for-byte
# readable as-is. Only a schema outside this set (a corrupt value, or a genuinely
# newer engine's schema) is treated as unparseable.
_READABLE_SCHEMA_VERSIONS = (1, SCHEMA_VERSION)
# Profile languages whose extractors carry the named-export/import extras these
# builders read, so bootstrap writes both index artifacts for them. Ruby has no
# static export surface (its cross-file view is the constant index). Shared by
# the bootstrap build gate and the read tools' unavailable-reason so "this
# language never gets an index" and "this language's index is missing/damaged"
# cannot drift apart.
REVERSE_INDEXED_LANGUAGES: frozenset[str] = frozenset({"typescript", "python"})
# A reverse index over a giant monorepo can grow large; cap recorded importer
# rows per (target, name) so one heavily-imported util cannot bloat the artifact
# or the per-edit advisory count. Only the retained rows are persisted, so a
# symbol imported by more than the cap reports the capped count, not the true
# total -- acceptable since the advisory's "N files import X" is a blast-radius
# hint and the cap is far above any realistic per-symbol fan-in.
_MAX_IMPORTERS_PER_SYMBOL = 500

# Max hops the build-time barrel-chase follows a named re-export chain before it
# stops and attributes the edge to the last file reached. Read at import time.
_MAX_REEXPORT_HOPS = threshold_int("REEXPORT_CHASE_MAX_HOPS")

# Candidate suffixes a bare (extensionless) module base may resolve to, plus the
# index-file forms for a directory import. Kept in sync with the resolution the
# path check uses so a file the path check considers "resolved" maps to the same
# index key here. Order matters: the first existing candidate wins, matching
# TS/Node module resolution (a sibling .ts beats a directory index).
_BASE_SUFFIXES = ("", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_INDEX_SUFFIXES = ("/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.mjs")
# NodeNext/ESM: a .js-family specifier commonly maps to a .ts source on disk.
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}


@dataclass(frozen=True)
class FileExports:
    """One file's exported-symbol entry.

    ``open`` True means the export set is non-authoritative (the file does
    ``export * from`` and re-exports an unenumerable set); callers must skip the
    symbol check for imports from such a file. When ``open`` is False, ``names``
    is the complete set of importable named bindings.
    """

    names: frozenset[str]
    open: bool


class ExportsIndex:
    """Repo-relative path -> :class:`FileExports`, loaded from the committed
    artifact. ``lookup`` returns None for a path not in the index (the caller
    treats that as "can't verify", no flag)."""

    def __init__(self, entries: dict[str, FileExports]) -> None:
        self._entries = entries

    def lookup(self, rel_key: str) -> FileExports | None:
        return self._entries.get(rel_key)

    def __len__(self) -> int:
        return len(self._entries)


def build_exports_index(files, repo_root: Path | str) -> dict:
    """Build the ``exports_index.json`` payload from parsed TypeScript/JS files.

    ``files`` is the bootstrap's parsed-file list; each entry's ``extras`` carries
    ``named_export_names`` and ``export_set_open`` (emitted by ts_dump.mjs). Files
    with neither an export set nor the open flag are omitted: a file that exports
    nothing can never satisfy a NAMED import anyway, and recording it would only
    bloat the artifact. Keys are repo-relative POSIX paths so the index is
    portable across checkouts and reproducible byte-for-byte (it is hashed into
    the trust SHA).
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    out: dict[str, dict] = {}
    for pf in files or ():
        extras = getattr(pf, "extras", None) or {}
        names_raw = extras.get("named_export_names")
        is_open = bool(extras.get("export_set_open"))
        names = (
            sorted({n for n in names_raw if isinstance(n, str)})
            if isinstance(names_raw, list)
            else []
        )
        if not names and not is_open:
            continue
        try:
            rel = Path(pf.path).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        out[rel] = {"names": names, "open": is_open}

    return {"schema_version": SCHEMA_VERSION, "files": out}


# Process-global cache of parsed indexes, keyed on the artifact path. The value
# carries the (mtime, size) the index was parsed at so a refresh that rewrites
# the artifact is picked up without re-reading on every lint call. The hot path
# touches this once per edited file.
_INDEX_CACHE: dict[str, tuple[tuple[int, int], ExportsIndex]] = {}


def load_exports_index(repo_root: Path | str | None) -> ExportsIndex | None:
    """Load the committed ``exports_index.json`` for ``repo_root``, or None.

    Returns None (no flag) on any ambiguity: no repo_root, no artifact, a corrupt
    or unreadable-schema payload, or any I/O error. A schema in
    ``_READABLE_SCHEMA_VERSIONS`` (the current version and its safely-readable
    predecessor) is NOT an ambiguity and loads normally. The symbol check is
    purely additive over the path check, so failing open here only means the
    check does not fire -- never a crash and never a false positive.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    # Follow a linked git worktree to the main worktree's profile, mirroring
    # load_reverse_index (this loader's sibling) -- without this, lint_file's
    # phantom-symbol check silently loses named-import-existence checking on a
    # linked worktree. worktree.py is pure filesystem, safe on this hot path.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    # Honor the atomic-commit sentinel like every other profile loader: an
    # uncommitted/torn .chameleon must read as index-unavailable, never served
    # as existence/importer ground truth while the sibling tools report
    # profile_corrupted for the same tree.
    from chameleon_mcp.bootstrap.transaction import is_committed

    if not is_committed(root / ".chameleon"):
        return None
    artifact = root / ".chameleon" / EXPORTS_INDEX_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 8_000_000:
        # Empty or implausibly large (a real index is well under this); skip
        # rather than read a pathological file on the hot path.
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") not in _READABLE_SCHEMA_VERSIONS:
        return None
    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        return None

    entries: dict[str, FileExports] = {}
    for rel, body in raw_files.items():
        if not isinstance(rel, str) or not isinstance(body, dict):
            continue
        names_raw = body.get("names")
        names = (
            frozenset(n for n in names_raw if isinstance(n, str))
            if isinstance(names_raw, list)
            else frozenset()
        )
        entries[rel] = FileExports(names=names, open=bool(body.get("open")))

    index = ExportsIndex(entries)
    _INDEX_CACHE[key] = (token, index)
    return index


def exports_index_mtime(repo_root: Path | str | None) -> float | None:
    """mtime of the committed ``exports_index.json`` for ``repo_root``, or None.

    Same path resolution as :func:`load_exports_index` (including the linked
    git-worktree redirect) so the phantom-symbol check can tell whether a target
    module was edited SINCE the index was built -- a stale index (a same-turn
    export rename, or a genuinely out-of-date committed one) must not drive a
    false "not exported" flag. None on any ambiguity/error (the caller then does
    not suppress -- it simply has no staleness signal)."""
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    try:
        return (root / ".chameleon" / EXPORTS_INDEX_FILENAME).stat().st_mtime
    except OSError:
        return None


def resolve_index_key(base: Path, repo_root: Path) -> str | None:
    """Map a resolved module base path to its repo-relative index key, or None.

    ``base`` is the specifier joined onto the importing file's directory WITHOUT
    an assumed extension (the same value the path check probes). This finds the
    concrete source file that base resolves to -- trying explicit suffixes, then
    the NodeNext .js->.ts remap, then a directory ``index.*`` -- and returns its
    repo-relative POSIX path so it can be looked up in the index.

    Returns None when nothing resolves under the repo (out-of-repo, a bare
    directory with no index file, or any I/O error): the caller then skips the
    symbol check for that specifier.
    """
    try:
        s = str(base)
        candidates: list[Path] = []
        # NodeNext/ESM .js-family specifier may map to a .ts source on disk.
        for js_ext, ts_exts in _JS_TO_TS.items():
            if s.endswith(js_ext):
                stem = s[: -len(js_ext)]
                candidates.extend(Path(stem + te) for te in ts_exts)
        candidates.extend(base if suf == "" else Path(s + suf) for suf in _BASE_SUFFIXES)
        candidates.extend(Path(s + suf) for suf in _INDEX_SUFFIXES)
        for cand in candidates:
            if cand.is_file():
                return cand.resolve().relative_to(repo_root).as_posix()
        return None
    except (ValueError, OSError):
        return None


@dataclass(frozen=True)
class Importer:
    """One call site that imports a name from a module: the importer's
    repo-relative path and the 1-based import line (``None`` when the dump could
    not place it).

    ``via`` is the barrel chain a through-barrel edge was chased across at build
    time (the re-export files between the importer's named module and this
    defining file), outermost first; empty for a direct import. It lets a query
    show ``importer -> barrel -> this file`` so a caller understands why an edge
    that never names this file lands on it.
    """

    path: str
    line: int | None
    via: tuple[str, ...] = ()


_PY_INDEX_SUFFIXES = (".py", ".pyi")


def resolve_python_index_key(base: Path, repo_root: Path) -> str | None:
    """Map a resolved Python module base path to its repo-relative index key.

    A module is ``base.py`` / ``base.pyi``; a package is ``base/__init__.py``.
    Returns the resolved file's repo-relative POSIX path (the same key form the
    exports index uses), or None when nothing resolves under the repo.
    """
    try:
        s = str(base)
        candidates = [Path(s + suf) for suf in _PY_INDEX_SUFFIXES]
        candidates += [base / "__init__.py", base / "__init__.pyi"]
        for cand in candidates:
            if cand.is_file():
                return cand.resolve().relative_to(repo_root).as_posix()
        return None
    except (ValueError, OSError):
        return None


def _python_module_base(module: str, importer_dir: Path, root: Path) -> Path:
    """The filesystem base path a Python import module points at (no extension).

    Relative (``.mod`` / ``..pkg.sub``) joins onto the importer's package walking
    up one dir per extra dot; absolute (``pkg.sub``) is repo-root-relative (Python
    packages import from the repo root).
    """
    if module.startswith("."):
        dots = len(module) - len(module.lstrip("."))
        rest = module[dots:]
        base = importer_dir
        for _ in range(dots - 1):
            base = base.parent
        return base / Path(rest.replace(".", "/")) if rest else base
    return root / Path(module.replace(".", "/"))


_PY_NON_SOURCE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".chameleon",
        "build",
        "dist",
        ".eggs",
        "site-packages",
    }
)


def _python_source_roots(root: Path) -> list[Path]:
    """Package roots an absolute Python import may resolve against.

    The repo root (flat layout); the PyPA ``src/`` root, always probed because it
    is the universal convention (and a PEP 420 namespace src-layout has no
    ``__init__`` for discovery to key on); plus any other immediate child that is
    NOT itself a package but DIRECTLY CONTAINS one (e.g. a ``backend/`` service
    dir). An absolute ``pkg.sub`` then resolves under whichever root holds
    ``pkg/``. A child that is itself a package (has ``__init__``) is the package,
    not a root, so it is skipped -- flat-layout and package-rooted repos are
    unchanged because the root is probed first. Build-time only, bounded to one
    directory level; a root that holds nothing simply yields None on probe.
    """
    roots = [root]
    src = root / "src"
    try:
        if src.is_dir():
            roots.append(src)
    except OSError:
        src = None
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return roots
    for child in children:
        if child == src or child.name in _PY_NON_SOURCE_DIRS or child.name.startswith("."):
            continue
        try:
            if (child / "__init__.py").exists() or (child / "__init__.pyi").exists():
                # child is itself a package: its modules import from the root.
                continue
            if any(
                (sub / "__init__.py").exists() or (sub / "__init__.pyi").exists()
                for sub in child.iterdir()
                if sub.is_dir() and sub.name not in _PY_NON_SOURCE_DIRS
            ):
                roots.append(child)
        except OSError:
            continue
    return roots


def make_module_resolver(
    root: Path, language: str = "typescript"
) -> Callable[[str, Path], str | None]:
    """Return ``resolve(module, importer_dir) -> rel_key | None`` for ``root``.

    ``root`` must be pre-resolved (``Path.resolve()``); passing a relative or
    symlinked path produces incorrect relative-to results.

    One resolver instance per build: relative specifiers join onto the
    importer's directory (the same probe the path check uses), and tsconfig/
    jsconfig path-alias specifiers (``~/utils/x``, ``@app/y``) map through the
    importer's NEAREST tsconfig so a monorepo where two apps bind the same
    ``~/*`` to different roots resolves each importer against its own config.
    The per-tsconfig-dir alias map is resolved lazily and cached on the closure
    (None marks a dir with no usable alias config), so the cache lives exactly
    as long as the build that needs it. Bare-package and out-of-repo
    specifiers resolve to None.

    Shared by the reverse index and the calls index so both resolve a
    specifier identically -- a target the one can see, the other can too.
    """
    if language == "python":
        # Relative (.mod / ..pkg) joins onto the importer's package; absolute
        # (pkg.sub) is probed against each source root (repo root first, then a
        # src-layout src/ root) so src-layout absolute imports resolve instead of
        # silently dropping every cross-file edge. All probe .py/.pyi/__init__.
        src_roots = _python_source_roots(root)

        def _resolve_python(module: str, importer_dir: Path) -> str | None:
            if module.startswith("."):
                base = _python_module_base(module, importer_dir, root)
                return resolve_python_index_key(base, root)
            rel = Path(module.replace(".", "/"))
            for src_root in src_roots:
                key = resolve_python_index_key(src_root / rel, root)
                if key is not None:
                    return key
            return None

        return _resolve_python

    # Imported lazily: phantom_imports imports this module for
    # resolve_index_key, so a top-level import here would be circular.
    from chameleon_mcp.phantom_imports import (
        _alias_targets,
        _load_tsconfig_paths,
        _nearest_tsconfig_dir,
    )

    alias_cache: dict[Path, tuple[Path, dict] | None] = {}

    def _alias_config_for(importer_dir: Path) -> tuple[Path, dict] | None:
        ts_dir = _nearest_tsconfig_dir(importer_dir, root)
        if ts_dir is None:
            return None
        cached = alias_cache.get(ts_dir, False)
        if cached is not False:
            return cached
        _, norm = _load_tsconfig_paths(str(ts_dir))
        paths = {k: list(v) for k, v in norm} if norm else {}
        result = (ts_dir, paths) if paths else None
        alias_cache[ts_dir] = result
        return result

    def _resolve_module(module: str, importer_dir: Path) -> str | None:
        if module.startswith("."):
            return resolve_index_key(importer_dir / module, root)
        cfg = _alias_config_for(importer_dir)
        if cfg is None:
            return None
        ts_dir, paths = cfg
        for base in _alias_targets(module, paths, ts_dir):
            key = resolve_index_key(base, root)
            if key is not None:
                return key
        return None

    return _resolve_module


def build_reexport_map(
    files, root: Path, resolve_module: Callable[[str, Path], str | None]
) -> dict[str, dict[str, tuple[str, str]]]:
    """Map each named re-export barrel to the targets its re-exports resolve to.

    ``barrel_rel -> exported_name -> (origin_name, target_rel)``: file ``barrel``
    does ``export { <origin> as <exported> } from '<module>'`` and ``module``
    resolves in-repo to ``target_rel`` (``exported`` == ``origin`` when there is
    no ``as`` alias). Only unambiguous, in-repo edges are kept: an exported name
    re-exported from two distinct resolved sources (a duplicate-export shape) is
    DROPPED rather than guessed, and a name whose module resolves out-of-repo (a
    bare package, an unresolved alias) is omitted so the chase stops at the
    barrel. ``resolve_module`` must be the same resolver the reverse / calls
    index uses so a target one build can see, the other can too. Build-time only.
    """
    # barrel_rel -> exported_name -> set of (origin_name, target_rel | None)
    raw: dict[str, dict[str, set[tuple[str, str | None]]]] = {}
    for pf in files or ():
        extras = getattr(pf, "extras", None) or {}
        rows = extras.get("re_exports")
        if not isinstance(rows, list) or not rows:
            continue
        try:
            barrel_rel = Path(pf.path).resolve().relative_to(root).as_posix()
            barrel_dir = Path(pf.path).resolve().parent
        except (ValueError, OSError):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            exported = row.get("exported")
            origin = row.get("origin")
            module = row.get("module")
            if not (
                isinstance(exported, str) and isinstance(origin, str) and isinstance(module, str)
            ):
                continue
            target = resolve_module(module, barrel_dir)
            raw.setdefault(barrel_rel, {}).setdefault(exported, set()).add((origin, target))

    out: dict[str, dict[str, tuple[str, str]]] = {}
    for barrel_rel, by_name in raw.items():
        resolved: dict[str, tuple[str, str]] = {}
        for exported, entries in by_name.items():
            in_repo = {(o, t) for (o, t) in entries if t is not None}
            # Exactly one distinct in-repo source keeps the chain deterministic;
            # zero (all out-of-repo) or many (ambiguous) leaves it unchased.
            if len(in_repo) == 1:
                origin, target = next(iter(in_repo))
                resolved[exported] = (origin, target)
        if resolved:
            out[barrel_rel] = resolved
    return out


def chase_reexport(
    target_rel: str,
    name: str,
    reexport_map: dict[str, dict[str, tuple[str, str]]],
    max_hops: int = _MAX_REEXPORT_HOPS,
) -> tuple[str, str, list[str]]:
    """Follow ``name`` from ``target_rel`` through named re-export barrels.

    Returns ``(final_rel, final_name, via)`` where ``final_rel`` is the file that
    DEFINES the symbol -- the first file in the chain that does not re-export the
    current name onward -- and ``via`` is the barrel files traversed to reach it,
    outermost first. When ``target_rel`` does not re-export ``name`` (the common
    case) the input is returned unchanged with an empty ``via``. Bounded by
    ``max_hops`` and cycle-safe: a re-export cycle or an over-deep chain stops at
    the last file reached rather than looping or over-attributing. ``name`` maps
    per hop (an ``as`` alias re-export changes the name the next file exports).
    """
    via: list[str] = []
    seen: set[tuple[str, str]] = {(target_rel, name)}
    cur_rel, cur_name = target_rel, name
    for _ in range(max_hops):
        entry = (reexport_map.get(cur_rel) or {}).get(cur_name)
        if entry is None:
            break
        origin, next_rel = entry
        if (next_rel, origin) in seen:
            break
        via.append(cur_rel)
        seen.add((next_rel, origin))
        cur_rel, cur_name = next_rel, origin
    return cur_rel, cur_name, via


# Cross-workspace index constants. The cross index has its OWN schema version,
# decoupled from reverse_index's SCHEMA_VERSION, so a shape change to one never
# forces a rebuild of the other.
CROSS_REVERSE_INDEX_FILENAME = "cross_reverse_index.json"
CROSSWS_SCHEMA_VERSION = 1
# Per-workspace cap on captured cross-package candidates ridden out in-memory on
# the BootstrapReport, so a pathological workspace cannot balloon workspace_reports.
_MAX_CROSS_CANDIDATES_PER_WS = 5000


def _is_cross_package_specifier(module: str, importer_dir: Path, ws_root: Path) -> bool:
    """True when an unresolved-in-workspace specifier is a CROSS-package shape.

    Two shapes qualify: a bare/scoped package name (`@scope/a`, `@scope/a/sub`,
    or a plain `lodash` -- the coordinator's package-name map decides whether it
    actually names a SIBLING workspace, so an external npm package falls out there
    with no false edge), or a relative specifier whose resolved path ESCAPES the
    importer's workspace root (`../other-pkg/x`). An unresolved relative specifier
    that stays inside the workspace is just a missing file, not a cross-package
    edge, so it is excluded.
    """
    if not module.startswith("."):
        return True
    try:
        target = (importer_dir / module).resolve()
    except OSError:
        return False
    try:
        target.relative_to(ws_root)
        return False
    except ValueError:
        return True


def collect_cross_package_candidates(files, ws_root: Path | str, language: str = "typescript"):
    """Cross-PACKAGE import rows that :func:`build_reverse_index` DROPS, captured
    for the coordinator cross-workspace JOIN. Returns a list of candidate dicts.

    build_reverse_index resolves each named import against the importer's OWN
    workspace and keeps only in-workspace targets; a ``@scope/a`` or an escaping
    ``../other-pkg`` import resolves to None and is dropped -- exactly the
    cross-workspace edge WP-C5 needs. This re-walks the same rows, keeps only the
    dropped ones of a cross-package shape, and records the RAW specifier (so the
    coordinator can resolve it against the package-name map it builds from every
    workspace's package.json). Importer paths are workspace-relative; the
    coordinator re-roots them to monorepo-relative. Fails open to a (possibly
    partial) list; bounded by ``_MAX_CROSS_CANDIDATES_PER_WS``.
    """
    out: list[dict] = []
    try:
        root = Path(ws_root).resolve()
        resolve = make_module_resolver(root, language)
        for pf in files or ():
            if len(out) >= _MAX_CROSS_CANDIDATES_PER_WS:
                break
            extras = getattr(pf, "extras", None) or {}
            rows = extras.get("import_symbols")
            if not isinstance(rows, list) or not rows:
                continue
            try:
                importer_rel = Path(pf.path).resolve().relative_to(root).as_posix()
                importer_dir = Path(pf.path).resolve().parent
            except (ValueError, OSError):
                continue
            for row in rows:
                if len(out) >= _MAX_CROSS_CANDIDATES_PER_WS:
                    break
                if not isinstance(row, dict):
                    continue
                name = row.get("name")
                module = row.get("module")
                if not isinstance(name, str) or not isinstance(module, str):
                    continue
                if resolve(module, importer_dir) is not None:
                    continue  # resolves in-workspace -> build_reverse_index already has it
                if not _is_cross_package_specifier(module, importer_dir, root):
                    continue  # external package or in-workspace miss -> not a cross edge
                line = row.get("line")
                out.append(
                    {
                        "importer": importer_rel,
                        "name": name,
                        "module": module,
                        "line": int(line) if isinstance(line, int) else None,
                    }
                )
    except Exception:
        return out
    return out


_JS_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _probe_module_file(mono_root: Path, rel_no_ext: str) -> str | None:
    """Resolve a module path (no extension) to an actual in-repo file's mono-key.

    Probes the JS/TS extension set then the ``/index.*`` directory form, mirroring
    the resolver's own probing. Returns the mono-root-relative POSIX key of the
    first file that exists, or None. Keys stay repo-relative for reproducibility.
    """
    base = (mono_root / rel_no_ext).resolve()
    for ext in _JS_EXTS:
        cand = base.with_suffix(base.suffix + ext) if base.suffix else Path(str(base) + ext)
        try:
            if cand.is_file():
                return cand.relative_to(mono_root).as_posix()
        except (OSError, ValueError):
            continue
    for ext in _JS_EXTS:
        cand = base / f"index{ext}"
        try:
            if cand.is_file():
                return cand.relative_to(mono_root).as_posix()
        except (OSError, ValueError):
            continue
    return None


def _resolve_package_main(mono_root: Path, pkg_dir_rel: str) -> str | None:
    """The entry file (mono-key) for a bare package import ``@scope/a``.

    Reads the workspace's package.json ``main``/``module`` (best-effort v1); falls
    back to ``index.*``. Deep ``exports`` conditional maps are a documented v1 gap
    (fail-closed -> None). None when nothing resolves.
    """
    pkg_dir = (mono_root / pkg_dir_rel).resolve()
    try:
        pj = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pj = {}
    for key in ("module", "main"):
        entry = pj.get(key) if isinstance(pj, dict) else None
        if isinstance(entry, str) and entry.strip():
            rel = f"{pkg_dir_rel}/{entry.strip()}"
            # main may already carry an extension; probe both the literal and the
            # extension-less form.
            direct = (mono_root / rel).resolve()
            try:
                if direct.is_file():
                    return direct.relative_to(mono_root).as_posix()
            except (OSError, ValueError):
                pass
            probed = _probe_module_file(mono_root, rel.rsplit(".", 1)[0] if "." in entry else rel)
            if probed:
                return probed
    return _probe_module_file(mono_root, f"{pkg_dir_rel}/index")


def _split_scoped_package(module: str) -> tuple[str, str]:
    """Split a package specifier into (package_name, subpath). ``@scope/a/b/c`` ->
    ('@scope/a', 'b/c'); ``lodash/fp`` -> ('lodash', 'fp'); ``@scope/a`` ->
    ('@scope/a', '')."""
    parts = module.split("/")
    if module.startswith("@"):
        pkg = "/".join(parts[:2])
        sub = "/".join(parts[2:])
    else:
        pkg = parts[0]
        sub = "/".join(parts[1:])
    return pkg, sub


def build_cross_reverse_index(candidates, packages: dict, mono_root: Path | str, exports_by_key):
    """Resolve every workspace's captured cross-package candidate to the sibling
    workspace file it targets and emit the cross_reverse_index.json payload.

    ``candidates``: list of ``{importer, name, module, line}`` with MONO-relative
    importer paths (the coordinator has already re-rooted them). ``packages``:
    ``{package_name -> workspace mono-relative dir}`` from each workspace's
    package.json ``name`` -- the link that lets ``@scope/a`` find package A.
    ``exports_by_key``: callable ``mono_key -> set[str]`` (or dict) of the names
    that file actually exports, for the FAIL-CLOSED name check -- an edge is
    emitted only when the target file genuinely exports the imported name, so a
    name that does not exist there (or an external npm package with no workspace
    entry) yields NO edge. Returns ``{schema_version, targets, packages}`` with
    mono-relative keys; deterministic (sorted, deduped, capped). Never raises.
    """
    root = Path(mono_root)
    lookup = (
        exports_by_key if callable(exports_by_key) else (lambda k: exports_by_key.get(k) or set())
    )
    accum: dict[str, dict[str, set[tuple[str, int | None]]]] = {}
    for c in candidates or ():
        try:
            if not isinstance(c, dict):
                continue
            importer = c.get("importer")
            name = c.get("name")
            module = c.get("module")
            if not (
                isinstance(importer, str) and isinstance(name, str) and isinstance(module, str)
            ):
                continue
            target_key = None
            if module.startswith("."):
                # Relative specifier that escaped its workspace: resolve against the
                # importer's mono-relative directory to a mono-key.
                resolved = ((root / importer).resolve().parent / module).resolve()
                if _under(root, resolved):
                    target_key = _probe_module_file(root, resolved.relative_to(root).as_posix())
            else:
                pkg, sub = _split_scoped_package(module)
                pkg_dir = packages.get(pkg)
                if pkg_dir is None:
                    continue  # external package (no sibling workspace) -> no edge
                target_key = (
                    _probe_module_file(root, f"{pkg_dir}/{sub}")
                    if sub
                    else _resolve_package_main(root, pkg_dir)
                )
            if not target_key:
                continue
            exps = lookup(target_key)
            if not isinstance(exps, (set, frozenset, list, tuple)) or name not in exps:
                continue  # fail-closed: target does not actually export the name
            line = c.get("line")
            line_val = int(line) if isinstance(line, int) else None
            accum.setdefault(target_key, {}).setdefault(name, set()).add((importer, line_val))
        except Exception:
            continue

    targets: dict[str, dict[str, list[dict]]] = {}
    for tkey, by_name in accum.items():
        names_out: dict[str, list[dict]] = {}
        for name, rows in by_name.items():
            rows_sorted = sorted(rows, key=lambda r: (r[0], r[1] if r[1] is not None else -1))
            names_out[name] = [
                {"path": p, "line": ln} for p, ln in rows_sorted[:_MAX_IMPORTERS_PER_SYMBOL]
            ]
        if names_out:
            targets[tkey] = names_out
    return {
        "schema_version": CROSSWS_SCHEMA_VERSION,
        "targets": targets,
        "packages": {k: v for k, v in sorted((packages or {}).items())},
    }


def _under(root: Path, p: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def build_reverse_index(files, repo_root: Path | str, language: str = "typescript") -> dict:
    """Build the ``reverse_index.json`` payload from parsed TypeScript/JS files.

    Inverts the import graph: for every named import an importer file carries
    (each ``extras['import_symbols']`` row is ``{name, module, line}``), resolve
    ``module`` against the importer's directory to the same repo-relative target
    key the exports index uses, then record the importer under
    ``target -> name -> [(importer, line[, via])]``.

    Keys are repo-relative POSIX paths so the artifact is portable across
    checkouts and reproducible byte-for-byte (it is hashed into the trust SHA).
    Bare-package and out-of-repo specifiers resolve to no in-repo target and are
    dropped: an existence break can only be reasoned about for a module that
    lives in this repo. A tsconfig/jsconfig path-alias specifier (``~/utils/x``,
    ``@app/y``) DOES name an in-repo file and is resolved through the same alias
    machinery the phantom-import path check uses, so an alias-dominant repo (where
    most named imports go through ``~/*``) is not blind to its own existence
    breaks. Importer rows are sorted and de-duplicated for a stable record.

    Barrel-chase (additive): when the resolved module RE-EXPORTS the imported
    name from another in-repo file (``export { x } from './impl'``), the same
    importer is ALSO recorded against the file that DEFINES the symbol, carrying
    the barrel chain in ``via``. The direct edge on the named module is kept
    unchanged, so an existence break is caught whether the barrel drops the
    re-export or the implementation drops the definition, and a query on the
    implementation file finally sees its through-barrel consumers.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    _resolve_module = make_module_resolver(root, language)
    reexport_map = build_reexport_map(files, root, _resolve_module)

    # target_rel -> name -> set of (importer_rel, line, via_tuple)
    accum: dict[str, dict[str, set[tuple[str, int | None, tuple[str, ...]]]]] = {}
    for pf in files or ():
        extras = getattr(pf, "extras", None) or {}
        rows = extras.get("import_symbols")
        if not isinstance(rows, list) or not rows:
            continue
        try:
            importer_rel = Path(pf.path).resolve().relative_to(root).as_posix()
            importer_dir = Path(pf.path).resolve().parent
        except (ValueError, OSError):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            module = row.get("module")
            if not isinstance(name, str) or not isinstance(module, str):
                continue
            target_key = _resolve_module(module, importer_dir)
            if target_key is None:
                continue
            line = row.get("line")
            line_val = int(line) if isinstance(line, int) else None
            accum.setdefault(target_key, {}).setdefault(name, set()).add(
                (importer_rel, line_val, ())
            )
            final_key, final_name, via = chase_reexport(target_key, name, reexport_map)
            if final_key != target_key:
                accum.setdefault(final_key, {}).setdefault(final_name, set()).add(
                    (importer_rel, line_val, tuple(via))
                )

    out: dict[str, dict[str, list[dict]]] = {}
    for target_key, by_name in accum.items():
        names_out: dict[str, list[dict]] = {}
        for name, importer_set in by_name.items():
            # Sort by (path, line, via) for a deterministic record; line None
            # sorts last via the -1 sentinel so a placed import precedes an
            # unplaced one from the same file.
            rows_sorted = sorted(
                importer_set, key=lambda r: (r[0], r[1] if r[1] is not None else -1, r[2])
            )
            capped = rows_sorted[:_MAX_IMPORTERS_PER_SYMBOL]
            rows_list: list[dict] = []
            for p, ln, via in capped:
                entry: dict = {"path": p, "line": ln}
                if via:
                    entry["via"] = list(via)
                rows_list.append(entry)
            names_out[name] = rows_list
        if names_out:
            out[target_key] = names_out

    return {"schema_version": SCHEMA_VERSION, "targets": out}


class ReverseIndex:
    """exported-name -> importers, scoped to one module file at a time.

    Loaded from the committed artifact. The internal shape is
    ``target_rel -> name -> [Importer]``; callers always go through one of the
    two query methods, which take the module's repo-relative path so a name and
    its importers can never be read against the wrong file.
    """

    def __init__(self, targets: dict[str, dict[str, list[Importer]]]) -> None:
        self._targets = targets

    def importers_of(self, target_rel: str, name: str) -> list[Importer]:
        """Files that import ``name`` from the module at ``target_rel``."""
        return list((self._targets.get(target_rel) or {}).get(name, ()))

    def names_for(self, target_rel: str) -> dict[str, list[Importer]]:
        """All imported-name -> importers entries recorded for one module."""
        return dict(self._targets.get(target_rel) or {})

    def target_keys(self) -> list[str]:
        """Repo-relative keys of every module the index records importers for.

        Lets a repo-wide consumer (the cross-file existence scan) walk every
        imported module without reaching into the internal mapping."""
        return list(self._targets.keys())

    def broken_importers(
        self, target_rel: str, current_exports: frozenset[str]
    ) -> dict[str, list[Importer]]:
        """Importers left dangling by the module's current export set.

        For the module at ``target_rel``, return ``name -> importers`` for every
        indexed name that is NOT in ``current_exports`` -- i.e. a binding the
        module USED to export (so an importer references it) but does not export
        now. This is the deterministic existence-break case: a removed or renamed
        export with a call site still naming the old binding.
        """
        out: dict[str, list[Importer]] = {}
        for name, importers in (self._targets.get(target_rel) or {}).items():
            if name not in current_exports and importers:
                out[name] = list(importers)
        return out

    def __len__(self) -> int:
        return len(self._targets)


# Process-global cache of parsed reverse indexes, keyed on the artifact path,
# carrying the (mtime, size) the index was parsed at so a refresh that rewrites
# the artifact is picked up without re-reading on every call.
_REVERSE_CACHE: dict[str, tuple[tuple[int, int], ReverseIndex]] = {}


def _parse_reverse_targets(raw_targets) -> dict[str, dict[str, list[Importer]]]:
    """Parse a reverse-index ``targets`` payload (target -> name -> importer rows)
    into the internal ``dict[str, dict[str, list[Importer]]]``. Shared by the
    reverse index and the WP-C5 cross index, which carry the identical target
    shape. Skips any malformed row; never raises."""
    targets: dict[str, dict[str, list[Importer]]] = {}
    for target_rel, by_name in (raw_targets or {}).items():
        if not isinstance(target_rel, str) or not isinstance(by_name, dict):
            continue
        names: dict[str, list[Importer]] = {}
        for name, rows in by_name.items():
            if not isinstance(name, str) or not isinstance(rows, list):
                continue
            importers: list[Importer] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                p = r.get("path")
                if not isinstance(p, str):
                    continue
                ln = r.get("line")
                raw_via = r.get("via")
                via = (
                    tuple(v for v in raw_via if isinstance(v, str))
                    if isinstance(raw_via, list)
                    else ()
                )
                importers.append(
                    Importer(path=p, line=ln if isinstance(ln, int) else None, via=via)
                )
            if importers:
                names[name] = importers
        if names:
            targets[target_rel] = names
    return targets


def load_reverse_index(repo_root: Path | str | None) -> ReverseIndex | None:
    """Load the committed ``reverse_index.json`` for ``repo_root``, or None.

    Returns None (no advisory, no finding) on any ambiguity: no repo_root, no
    artifact, a corrupt or unreadable-schema payload, or any I/O error. A schema
    in ``_READABLE_SCHEMA_VERSIONS`` (the current version and its safely-readable
    predecessor) is NOT an ambiguity and loads normally -- a v1 artifact simply
    carries no barrel-chase ``via`` breadcrumbs, since it predates that feature.
    The reverse index only ADDS cross-file context; failing open here means the
    advisory and the existence query simply do not fire -- never a crash, never a
    false claim.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    # Follow a linked git worktree to the main worktree's profile, mirroring
    # load_calls_index -- without this, every cross-file existence-break check
    # (query_symbol_importers, get_crossfile_context, the Stop-hook advisory and
    # deny) reads the worktree's absent .chameleon and silently degrades to
    # "no data" instead of the real signal.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    # Honor the atomic-commit sentinel like every other profile loader: an
    # uncommitted/torn .chameleon must read as index-unavailable, never served
    # as importer ground truth while the sibling tools report
    # profile_corrupted for the same tree.
    from chameleon_mcp.bootstrap.transaction import is_committed

    if not is_committed(root / ".chameleon"):
        return None
    artifact = root / ".chameleon" / REVERSE_INDEX_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        # Empty or implausibly large; skip rather than read a pathological file.
        # The reverse index is roomier than the exports index (one row per import
        # site, not per file), so the ceiling is higher.
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _REVERSE_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") not in _READABLE_SCHEMA_VERSIONS:
        return None
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict):
        return None

    index = ReverseIndex(_parse_reverse_targets(raw_targets))
    _REVERSE_CACHE[key] = (token, index)
    return index


# Cross-workspace index cache, same (mtime, size) keying as _REVERSE_CACHE. The
# value is (ReverseIndex over the cross targets, package-name -> mono-dir map).
_CROSS_REVERSE_CACHE: dict[str, tuple[tuple[int, int], tuple[ReverseIndex, dict]]] = {}


def load_cross_reverse_index(path: Path | str | None):
    """Load the WP-C5 cross_reverse_index.json at ``path`` -> ``(ReverseIndex,
    packages)``, or None.

    ``path`` is the PLUGIN-DATA artifact (``<data>/<coordinator repo_id>/
    cross_reverse_index.json``), NOT a repo-resident file -- the caller resolves
    it. The cross index carries the identical target->name->importers shape as the
    reverse index (so ``ReverseIndex.broken_importers`` works unchanged) plus a
    ``packages`` name->mono-dir map. Fail-open to None on any ambiguity: missing,
    empty, oversize, corrupt, or a foreign ``CROSSWS_SCHEMA_VERSION``. Keys are
    monorepo-root-relative, so the consumer must join importer paths against the
    coordinator root, never a workspace root.
    """
    if path is None:
        return None
    try:
        p = Path(path)
        st = os.stat(p)
    except OSError:
        return None
    if not st.st_size or st.st_size > 16_000_000:
        return None
    key = str(p)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CROSS_REVERSE_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != CROSSWS_SCHEMA_VERSION:
        return None
    targets = _parse_reverse_targets(data.get("targets"))
    raw_packages = data.get("packages")
    packages = (
        {k: v for k, v in raw_packages.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_packages, dict)
        else {}
    )
    result = (ReverseIndex(targets), packages)
    _CROSS_REVERSE_CACHE[key] = (token, result)
    return result


def module_key_for_path(file_path: Path | str, repo_root: Path | str | None) -> str | None:
    """Repo-relative POSIX key for an edited/queried module file, or None.

    The reverse index keys targets on the repo-relative path of the imported
    module. An edit-time advisory and the existence query both hold the module's
    own path, which maps to its key directly (no specifier resolution needed),
    so this is just "make it repo-relative POSIX" with the same fail-open stance
    as the rest of the module.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
        return Path(file_path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None
