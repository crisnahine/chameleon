"""Repo-level import-layering graph, derived once at bootstrap.

Chameleon clusters each file in isolation, so a file that imports exactly the
wrong layer still matches its archetype perfectly. This module looks across
files: it resolves each member's imports to an on-disk target, maps that target
to its archetype, and accumulates a cluster-to-cluster edge multiset. From the
multiset it derives two advisory artifacts:

  - forbidden-upward edges: a directional pair A imports B in several files while
    B never imports A. A new B->A import inverts the established direction, which
    is the layering mistake worth surfacing.
  - a static import-cycle report at the cluster level, for status/PR-review.

Everything here is advisory context. There is no per-edit cycle detection and no
block-eligible rule: a many-to-many cluster edge graph is too noisy to gate an
edit on, and the forbidden edge is learned from the absence of a reverse
crossing, which only reads as a rule when the direction is genuinely unanimous.

Bare-package and workspace-package imports resolve to no in-repo file and are
silently skipped (fail-open) rather than guessed at.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from chameleon_mcp._thresholds import threshold

if TYPE_CHECKING:
    from chameleon_mcp.extractors._base import ParsedFile

# Candidate suffixes for resolving a specifier to a real source file. Kept local
# (not imported from phantom_imports) so the layering build owns its resolution
# policy and a tweak to the lint resolver can't silently shift the graph.
_TS_CODE_SUFFIXES = ("", ".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
_TS_INDEX_SUFFIXES = ("/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.mjs")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}
_RUBY_SUFFIXES = ("", ".rb")


def _is_bare_specifier(spec: str) -> bool:
    """True for a package-style import (``react``, ``@scope/pkg``, ``active_support``).

    A relative specifier starts with ``.`` or ``/``; everything else is a bare
    package or a tsconfig alias. Aliases are resolved separately; a plain bare
    package resolves to no in-repo file and is skipped.
    """
    return not (spec.startswith(".") or spec.startswith("/"))


def _resolve_ts_relative(spec: str, from_file: Path, repo_root: Path) -> Path | None:
    """Resolve a relative TS/JS specifier to a real file under ``repo_root``.

    Returns the resolved path, or None when nothing on disk matches (the import
    points outside the repo, at a barrel that doesn't exist, or at a non-code
    asset). Resolution is suffix-probing identical in spirit to the phantom-import
    lint, but here it must return the concrete target so it can be mapped to an
    archetype.
    """
    base = (from_file.parent / spec).resolve()
    try:
        base.relative_to(repo_root)
    except ValueError:
        return None
    return _probe_ts(base)


def _probe_ts(base: Path) -> Path | None:
    s = str(base)
    try:
        for js_ext, ts_exts in _JS_TO_TS.items():
            if s.endswith(js_ext):
                stem = s[: -len(js_ext)]
                for te in ts_exts:
                    cand = Path(stem + te)
                    if cand.is_file():
                        return cand
        for suf in _TS_CODE_SUFFIXES:
            cand = base if suf == "" else Path(s + suf)
            if cand.is_file():
                return cand
        for suf in _TS_INDEX_SUFFIXES:
            cand = Path(s + suf)
            if cand.is_file():
                return cand
    except OSError:
        return None
    return None


def _resolve_via_alias(
    spec: str,
    tsconfig_paths: tuple[tuple[str, tuple[str, ...]], ...],
    base_url: str,
    repo_root: Path,
) -> Path | None:
    """Resolve a tsconfig ``paths`` alias to a real file, or None.

    The alias map points a pattern (``@app/*``) at one or more targets
    (``src/*``). The wildcard tail is substituted into each target and probed.
    Returns the first resolving target; an alias that maps to nothing on disk is
    treated as unresolvable (skipped) rather than guessed.
    """
    base_dir = (repo_root / base_url).resolve() if base_url else repo_root
    for pattern, targets in tsconfig_paths:
        tail: str | None = None
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # keep trailing slash so @app/ != @apple/
            if spec.startswith(prefix):
                tail = spec[len(prefix) :]
            elif spec == pattern[:-2]:
                tail = ""
        elif spec == pattern:
            tail = ""
        if tail is None:
            continue
        for target in targets:
            mapped = target.replace("*", tail) if "*" in target else target
            resolved = _probe_ts((base_dir / mapped).resolve())
            if resolved is not None:
                return resolved
    return None


def _resolve_ruby(spec: str, from_file: Path, repo_root: Path) -> Path | None:
    """Resolve a Ruby ``require_relative`` target to a real file, or None.

    Only ``require_relative`` is resolvable statically; bare ``require`` reaches
    the load path (gems, autoload) and is skipped. The dump tags require_relative
    with import-kind ``namespace``; the caller filters on that.
    """
    base = (from_file.parent / spec).resolve()
    try:
        base.relative_to(repo_root)
    except ValueError:
        return None
    try:
        for suf in _RUBY_SUFFIXES:
            cand = base if suf == "" else Path(str(base) + suf)
            if cand.is_file():
                return cand
    except OSError:
        return None
    return None


def _load_tsconfig_paths(repo_root: Path) -> tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]:
    """(baseUrl, ((pattern, (targets,...)),...)) from tsconfig/jsconfig at the root."""
    import json

    for name in ("tsconfig.json", "jsconfig.json"):
        p = repo_root / name
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        co = data.get("compilerOptions")
        co = co if isinstance(co, dict) else {}
        base = co.get("baseUrl") or "."
        paths = co.get("paths")
        paths = paths if isinstance(paths, dict) else {}
        norm = tuple((k, tuple(v)) for k, v in paths.items() if isinstance(v, list))
        return base, norm
    return ".", ()


def _build_path_to_archetype(
    files_by_archetype: dict[str, list[ParsedFile]],
) -> dict[str, str]:
    """Reverse index from a resolved (string) file path to its archetype name."""
    index: dict[str, str] = {}
    for arch, files in files_by_archetype.items():
        for f in files:
            try:
                index[str(f.path.resolve())] = arch
            except OSError:
                continue
    return index


def _resolve_import_archetype(
    spec: str,
    kind: str,
    from_file: Path,
    repo_root: Path,
    language: str,
    path_to_arch: dict[str, str],
    tsconfig_paths: tuple[tuple[str, tuple[str, ...]], ...],
    base_url: str,
) -> str | None:
    """Map one import specifier to the archetype of the file it resolves to.

    Returns None when the specifier is a bare package, resolves outside the repo,
    resolves to a file no archetype owns, or cannot be resolved at all. None is
    the fail-open path: an unresolved import contributes no edge.
    """
    target: Path | None = None
    if language == "ruby":
        # The Ruby dump tags require_relative as namespace; plain require/autoload
        # reach the load path and are unresolvable statically.
        if kind == "namespace" and not _is_bare_specifier(spec):
            target = _resolve_ruby(spec, from_file, repo_root)
    else:
        if _is_bare_specifier(spec):
            if tsconfig_paths:
                target = _resolve_via_alias(spec, tsconfig_paths, base_url, repo_root)
        else:
            target = _resolve_ts_relative(spec, from_file, repo_root)
    if target is None:
        return None
    return path_to_arch.get(str(target))


def _find_cycles(
    adjacency: dict[str, set[str]],
    max_cycles: int,
) -> list[list[str]]:
    """Enumerate distinct cluster-level cycles in the directed edge graph.

    A cluster self-edge (A imports A, common and benign) is not a cycle. Cycles
    are normalized to start at their lexicographically smallest node and
    de-duplicated so the same loop reported from two entry points counts once.
    The walk is depth-first with a recursion-stack set; it is bounded by
    ``max_cycles`` on found cycles and by a total step budget, because the
    cycle bound alone never fires on a dense acyclic graph where simple-path
    enumeration is exponential.
    """
    cycles: list[list[str]] = []
    # Cluster graphs are tens of nodes, so this budget is generous in practice
    # while keeping the worst case linear-bounded.
    steps_left = 50_000
    seen: set[tuple[str, ...]] = set()
    nodes = sorted(adjacency.keys())

    def normalize(path: list[str]) -> tuple[str, ...]:
        # path is the cycle body (no repeated closing node); rotate to start at
        # the smallest member so A->B->A and B->A->B read as one cycle.
        if not path:
            return ()
        i = path.index(min(path))
        return tuple(path[i:] + path[:i])

    def dfs(node: str, stack: list[str], on_stack: set[str]) -> None:
        nonlocal steps_left
        if len(cycles) >= max_cycles or steps_left <= 0:
            return
        steps_left -= 1
        for nxt in sorted(adjacency.get(node, ())):
            if nxt == node:
                continue  # self-edge is not a cycle
            if nxt in on_stack:
                start = stack.index(nxt)
                body = stack[start:]
                key = normalize(body)
                if key and key not in seen:
                    seen.add(key)
                    cycles.append(list(key))
                    if len(cycles) >= max_cycles:
                        return
                continue
            stack.append(nxt)
            on_stack.add(nxt)
            dfs(nxt, stack, on_stack)
            on_stack.discard(nxt)
            stack.pop()
            if len(cycles) >= max_cycles:
                return

    for start in nodes:
        if len(cycles) >= max_cycles:
            break
        dfs(start, [start], {start})
    return cycles


def build_layering(
    *,
    files_by_archetype: dict[str, list[ParsedFile]],
    repo_root: Path,
    language: str = "typescript",
) -> dict:
    """Build the advisory import-layering artifact for the conventions blob.

    Returns ``{}`` when no in-repo import edges resolve (too few files, all bare
    imports, single archetype). Otherwise returns forbidden-upward edges and a
    static cycle report, both repo-level and advisory.
    """
    if not files_by_archetype:
        return {}

    path_to_arch = _build_path_to_archetype(files_by_archetype)
    base_url, tsconfig_paths = _load_tsconfig_paths(repo_root) if language != "ruby" else (".", ())

    # Per-file edge dedup: a file importing the same archetype 5 times is one
    # crossing, not five, so the frequency reflects how many FILES cross, not how
    # many import statements.
    edge_file_counts: dict[tuple[str, str], int] = {}
    for src_arch, files in files_by_archetype.items():
        for f in files:
            crossed: set[str] = set()
            for entry in f.import_specifiers or ():
                # Each entry is a (module, kind) pair from the extractor; guard
                # against a malformed shape rather than crash the bootstrap.
                if not (isinstance(entry, (tuple, list)) and len(entry) == 2):
                    continue
                spec, kind = entry
                dst_arch = _resolve_import_archetype(
                    spec,
                    kind,
                    f.path,
                    repo_root,
                    language,
                    path_to_arch,
                    tsconfig_paths,
                    base_url,
                )
                if dst_arch is None or dst_arch == src_arch:
                    continue  # unresolved or intra-archetype edge: no layering signal
                crossed.add(dst_arch)
            for dst_arch in crossed:
                edge_file_counts[(src_arch, dst_arch)] = (
                    edge_file_counts.get((src_arch, dst_arch), 0) + 1
                )

    if not edge_file_counts:
        return {}

    min_edge_files = int(threshold("LAYERING_MIN_EDGE_FILES"))
    max_forbidden = int(threshold("LAYERING_MAX_FORBIDDEN_EDGES"))
    max_cycles = int(threshold("LAYERING_MAX_CYCLES"))

    # Forbidden-upward: A imports B in >= threshold files AND B never imports A.
    # The forbidden direction is the unobserved reverse (B->A): a new B->A import
    # inverts the established layering. Only unanimous pairs qualify; a single
    # existing reverse crossing makes the direction ambiguous and disqualifies it.
    forbidden: list[dict] = []
    for (a, b), count in edge_file_counts.items():
        if count < min_edge_files:
            continue
        if edge_file_counts.get((b, a), 0) != 0:
            continue
        forbidden.append(
            {
                "from": b,  # the layer that should NOT import...
                "to": a,  # ...this one (the reverse of the observed A->B)
                "observed_direction": {"from": a, "to": b, "files": count},
            }
        )
    forbidden.sort(key=lambda e: (-e["observed_direction"]["files"], e["from"], e["to"]))
    forbidden = forbidden[:max_forbidden]

    adjacency: dict[str, set[str]] = {}
    for a, b in edge_file_counts:
        adjacency.setdefault(a, set()).add(b)
    cycles = _find_cycles(adjacency, max_cycles)

    result: dict = {}
    if forbidden:
        result["forbidden_upward_edges"] = forbidden
    if cycles:
        result["import_cycles"] = cycles
    if not result:
        return {}
    result["edge_count"] = len(edge_file_counts)
    return result
