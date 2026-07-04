"""Committed calls index: deterministic caller->callee edges for judge facts.

Inverts the dumpers' raw call_sites into callee-file -> callable -> callers
with exactly three grades, mirroring symbol_index's build/load split,
fail-open reads, and honest truncation. Name-only repo-wide matches are
deliberately NOT stored: they are the false-positive bulk, and chameleon
asserts only what is deterministic.

Grades:

- ``same_file`` - callee defined in the caller's own file: bare calls to a
  file-local callable, and this./self. calls to a member of any class
  defined in the same file (call sites carry no enclosing-class field, so
  per-class scoping is impossible). The member lookup is file-scoped: a
  this-call whose method lives on a base class in another file yields no
  edge rather than a guess (cross-file inheritance is out of scope).
- ``import`` - TypeScript and Python: a bare or new call of a named import
  (matched on its LOCAL binding name -- ``import { x as y }`` binds ``y`` --
  with the edge recorded under the EXPORTED name it resolves to), or
  ns.member() / new ns.Foo() through a runtime namespace import, resolved with
  the same specifier machinery as the reverse index; the callee must exist in
  the target's CLOSED export set. An open (barrel) set proves nothing, so it
  yields no edge. ``new`` is TypeScript-only (Python has no construction
  call); Python contributes bare calls of named imports and ``recv.attr()``
  through a runtime namespace import (``import a.b as x; x.f()``).
- ``constant_receiver`` - Ruby only: Const.method where Const matches a
  class key exactly. Keys are fully qualified (``enclosing_class_path``,
  module nesting included; old dumps fall back to the lexical class name),
  so a bare receiver matches a top-level class only and a namespaced class
  is reachable only through its qualified name. The matched key must name
  exactly one defining file across the dump AND the matched member must be
  class-level (kind ``singleton_method``: ``def self.x`` or a ``class <<
  self`` def) -- an instance def with the same name is undispatchable from
  a constant receiver, so it yields no edge. ``new`` maps to the INSTANCE
  ``initialize`` when the target defines it, is skipped (never invented)
  when it does not, and is also skipped when the class overrides ``def
  self.new`` (the override owns construction; mapping through it is
  unprovable).

Known limitations (accepted imprecision, by design):

- Binding shadowing: a parameter or local variable that shadows an
  imported binding can yield a false ``import`` edge. Call-site
  identifiers are matched against the import map with no scope analysis.
- Metaprogrammed calls (Ruby ``send``/``define_method``, Rails dynamic
  finders) are invisible to the dumpers and simply record no edge.

Named re-export barrels ARE resolved: a call through a named re-export
(``export { x } from './impl'``) is attributed additively to BOTH the
barrel and the file that defines the implementation, with the barrel chain
recorded in the caller row's ``via``, so a query on the implementation sees
its through-barrel callers. Ambiguous (same name from two sources) and
out-of-repo re-exports stay unchased; wildcard barrels (``export * from``)
remain open sets that yield no edge.

Two halves live here so the build (bootstrap-time, populates the artifact)
and the read (query-time, consumes it) share one key scheme and can't drift:
:func:`build_calls_index` turns parsed files into the artifact payload;
:func:`load_calls_index` reads the committed artifact, cached on mtime so a
mid-session refresh is picked up without re-reading every call.

``total`` in each emitted entry is a true count of graded sites (a lower
bound when a contributing file hit the dump-time cap).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.symbol_index import build_reexport_map, chase_reexport, make_module_resolver

CALLS_INDEX_FILENAME = "calls_index.json"
# v2: import-grade caller rows may carry an optional ``via`` barrel chain (the
# re-export files a through-barrel call edge was chased across at build time). A
# v1 artifact fails the load gate and refreshes on the next engine-upgrade
# session; the read is fail-open, so a caller query simply omits the chased edge
# until then. Never a crash, never a false claim.
SCHEMA_VERSION = 2

# The closed grade set build_calls_index emits. A row carrying any other
# grade is malformed (hand-edited or future-schema) and is skipped on load
# like every other malformed row.
VALID_GRADES = frozenset({"same_file", "import", "constant_receiver"})


def build_calls_index(files, repo_root: Path | str, language: str) -> dict:
    """Build the ``calls_index.json`` payload from parsed source files.

    ``files`` is the bootstrap's parsed-file list; each entry's ``extras``
    carries ``call_sites`` plus the per-file facts the grades need
    (``callable_signatures`` with ``enclosing_class``, ``named_export_names``
    / ``export_set_open``, ``import_symbols``, ``namespace_imports``). Keys
    are repo-relative POSIX paths so the artifact is portable across
    checkouts and reproducible byte-for-byte.

    Every call site that does not meet one grade's full conditions is
    skipped, never downgraded to a fuzzy match. Caps keep the artifact
    bounded while ``total`` stays the true count of graded sites (a lower
    bound when a contributing file hit the dump-time cap), so a capped
    entry reads as "N callers, M shown" rather than silently lying.
    """
    try:
        root = Path(repo_root).resolve()
    except OSError:
        root = Path(repo_root)

    resolve_module = make_module_resolver(root, language)
    # Named re-export barrels, so an import-grade edge whose named module merely
    # re-exports the callee is ALSO attributed to the file that defines it.
    reexport_map = build_reexport_map(files, root, resolve_module)

    # Pass 1: per-file fact tables. A rel appearing twice merges, mirroring
    # the reverse index's dedupe stance.
    callables: dict[str, set[str]] = {}
    # rel -> class name -> member name -> kinds recorded for that member.
    # Kinds matter to the constant_receiver grade only: Const.method can
    # dispatch only to a class-level (singleton_method) member, Const.new
    # only to an instance initialize. The this/self grade matches on names
    # alone (call sites carry no class-vs-instance context).
    class_members: dict[str, dict[str, dict[str, set[str]]]] = {}
    exports: dict[str, tuple[set[str], bool]] = {}
    # rel -> local binding name -> (module specifier, exported name). Keyed on
    # the LOCAL binding because that is what call-site identifiers carry
    # (``import { x as y }`` binds y); the exported name rides along because
    # the target's closed export set and the recorded edge both use it.
    import_map: dict[str, dict[str, tuple[str, str]]] = {}
    ns_aliases: dict[str, dict[str, str]] = {}
    file_dir: dict[str, Path] = {}
    sites_by_rel: dict[str, list[dict]] = {}
    # Caller files whose site list was capped at dump time. Every entry that
    # file contributed callers to is marked truncated: its recorded sites are
    # a lower bound on the real call count.
    dump_capped_rels: set[str] = set()
    # class name -> rels that define it, across the whole dump. The
    # constant_receiver grade only fires when this set has exactly one
    # member: an ambiguous constant proves nothing.
    class_defs: dict[str, set[str]] = {}

    for pf in files or ():
        path = getattr(pf, "path", None)
        if path is None:
            continue
        extras = getattr(pf, "extras", None) or {}
        try:
            resolved = Path(path).resolve()
            rel = resolved.relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        file_dir[rel] = resolved.parent

        for row in extras.get("callable_signatures") or ():
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name:
                continue
            callables.setdefault(rel, set()).add(name)
            # Ruby dumps carry the fully qualified class path (module nesting
            # joined with "::"); keying on it stops a short class name from
            # matching across namespaces. Rows without it (old dumps, TS) fall
            # back to the lexical class name.
            class_path = row.get("enclosing_class_path")
            cls = (
                class_path
                if isinstance(class_path, str) and class_path
                else row.get("enclosing_class")
            )
            if isinstance(cls, str) and cls:
                kinds = (
                    class_members.setdefault(rel, {}).setdefault(cls, {}).setdefault(name, set())
                )
                kind = row.get("kind")
                if isinstance(kind, str) and kind:
                    kinds.add(kind)
                class_defs.setdefault(cls, set()).add(rel)

        names_raw = extras.get("named_export_names")
        is_open = bool(extras.get("export_set_open"))
        names = (
            {n for n in names_raw if isinstance(n, str)} if isinstance(names_raw, list) else set()
        )
        if names or is_open:
            prev = exports.get(rel)
            if prev is not None:
                names |= prev[0]
                is_open = is_open or prev[1]
            exports[rel] = (names, is_open)

        for row in extras.get("import_symbols") or ():
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            module = row.get("module")
            if isinstance(name, str) and name and isinstance(module, str) and module:
                # Old dumps carry no `local`; without an alias the exported
                # name IS the local binding, so falling back is exact.
                local = row.get("local")
                binding = local if isinstance(local, str) and local else name
                import_map.setdefault(rel, {})[binding] = (module, name)

        for row in extras.get("namespace_imports") or ():
            if not isinstance(row, dict):
                continue
            alias = row.get("alias")
            module = row.get("module")
            if isinstance(alias, str) and alias and isinstance(module, str) and module:
                ns_aliases.setdefault(rel, {})[alias] = module

        raw_sites = extras.get("call_sites")
        if isinstance(raw_sites, list) and raw_sites:
            sites_by_rel.setdefault(rel, []).extend(r for r in raw_sites if isinstance(r, dict))
        if bool(extras.get("call_sites_truncated")):
            dump_capped_rels.add(rel)

    # Per-build memo for module resolution: keyed (module_specifier, importer_dir)
    # so each distinct specifier is probed exactly once across all files.
    _resolve_memo: dict[tuple[str, Path], str | None] = {}

    def _resolved_module(module: str, importer_dir: Path) -> str | None:
        """Module rel iff the specifier resolves to an in-repo file; None otherwise."""
        key = (module, importer_dir)
        if key not in _resolve_memo:
            _resolve_memo[key] = resolve_module(module, importer_dir)
        return _resolve_memo[key]

    def _closed_target(target_rel: str, name: str) -> str | None:
        """``target_rel`` iff its CLOSED export set contains ``name``; None otherwise."""
        exp = exports.get(target_rel)
        if exp is None or exp[1] or name not in exp[0]:
            return None
        return target_rel

    # Pass 2: grade each call site. accum maps
    # callee_rel -> callee_name -> {(caller_path, caller_fn, line, grade, via)};
    # the tuple set dedupes a site the dump happened to emit twice.
    accum: dict[str, dict[str, set[tuple[str, str, int | None, str, tuple[str, ...]]]]] = {}

    def _add(
        callee_rel: str, callee_name: str, caller_rel: str, caller_fn, line, grade, via=()
    ) -> None:
        # The loader enforces the same closed set; an unknown grade must never
        # be emitted from the builder so the two halves can't drift.
        if grade not in VALID_GRADES:
            return
        fn = caller_fn if isinstance(caller_fn, str) and caller_fn else "<module>"
        ln = line if isinstance(line, int) else None
        accum.setdefault(callee_rel, {}).setdefault(callee_name, set()).add(
            (caller_rel, fn, ln, grade, tuple(via))
        )

    def _add_import_edge(target_rel: str, exported: str, caller_rel: str, caller_fn, line) -> None:
        """Record an import-grade edge, plus a barrel-chased edge on the file that
        DEFINES the callee when ``target_rel`` merely re-exports it. Additive: the
        direct edge on the named module is kept, mirroring the reverse index, so a
        query on the barrel and a query on the implementation both see the caller.
        """
        _add(target_rel, exported, caller_rel, caller_fn, line, "import")
        final_rel, final_name, via = chase_reexport(target_rel, exported, reexport_map)
        if final_rel != target_rel:
            _add(final_rel, final_name, caller_rel, caller_fn, line, "import", tuple(via))

    for rel, sites in sites_by_rel.items():
        own_callables = callables.get(rel) or set()
        own_members: set[str] = set()
        for members in (class_members.get(rel) or {}).values():
            own_members |= members.keys()
        own_imports = import_map.get(rel) or {}
        own_aliases = ns_aliases.get(rel) or {}
        fdir = file_dir[rel]

        # Resolve each DISTINCT module specifier once per file. The site loop
        # below does dict lookups only -- zero resolve_module calls inside it.
        # local binding -> (resolved target rel | None, exported name).
        import_targets: dict[str, tuple[str | None, str]] = {
            binding: (_resolved_module(module_spec, fdir), exported)
            for binding, (module_spec, exported) in own_imports.items()
        }
        alias_targets: dict[str, str | None] = {
            alias: _resolved_module(module_spec, fdir) for alias, module_spec in own_aliases.items()
        }

        for site in sites:
            name = site.get("name")
            kind = site.get("kind")
            if not isinstance(name, str) or not name or not isinstance(kind, str):
                continue
            line = site.get("line")
            caller_fn = site.get("caller")

            if kind == "bare":
                # A file-local definition is the deterministic anchor; only a
                # name with no local definition is checked against imports.
                # The site identifier is a LOCAL binding; the closed-set check
                # and the recorded edge use the exported name it resolves to.
                if name in own_callables:
                    _add(rel, name, rel, caller_fn, line, "same_file")
                elif language in ("typescript", "python") and name in import_targets:
                    # Python calls an imported function bare too
                    # (`from .svc import run; run()`); the local binding resolves
                    # to the exported name, checked against the target's closed set.
                    t, exported = import_targets[name]
                    if t is not None and _closed_target(t, exported) is not None:
                        _add_import_edge(t, exported, rel, caller_fn, line)
            elif kind in ("this", "self"):
                if name in own_members:
                    _add(rel, name, rel, caller_fn, line, "same_file")
            elif kind in ("new", "member"):
                if language not in ("typescript", "python"):
                    continue
                receiver = site.get("receiver")
                # Python has no `new`; its only member sites are `recv.attr()`,
                # graded below against a runtime namespace import (`import a.b as
                # x; x.f()`). The receiver-less `new` grade is TS construction.
                if language == "typescript" and kind == "new" and receiver is None:
                    # `new Foo()` of a named import: the EXPORTED name the
                    # local binding resolves to is the callee key (the index
                    # keys on exported names, not constructors or aliases).
                    # Only a receiver-less construction may resolve here:
                    # `new ns.Foo()` carries a receiver, and its property name
                    # coinciding with a named import proves nothing about the
                    # receiver.
                    if name in import_targets:
                        t, exported = import_targets[name]
                        if t is not None and _closed_target(t, exported) is not None:
                            _add_import_edge(t, exported, rel, caller_fn, line)
                    continue
                # `obj.member()` or `new ns.Foo()`: resolvable only when the
                # receiver names a runtime namespace import, against the alias
                # target's closed export set.
                if isinstance(receiver, str) and receiver in alias_targets:
                    t = alias_targets[receiver]
                    if t is not None and _closed_target(t, name) is not None:
                        _add_import_edge(t, name, rel, caller_fn, line)
            elif kind == "constant":
                if language != "ruby":
                    continue
                receiver = site.get("receiver")
                # Class keys are fully qualified, so a receiver matches only on
                # exact equality: a bare receiver can match a top-level class
                # only, never a namespaced one. A bare name CAN lexically
                # resolve to a namespaced class from inside its namespace, but
                # call sites carry no lexical context, so asserting that edge
                # would be a guess; the bare form stays unmatched (accepted
                # undercoverage).
                defs = class_defs.get(receiver) if isinstance(receiver, str) else None
                if not defs or len(defs) != 1:
                    continue
                (target_rel,) = defs
                members = (class_members.get(target_rel) or {}).get(receiver) or {}
                if name == "new":
                    # A `def self.new` override owns construction: whether and
                    # how it reaches initialize is not provable statically, so
                    # the new->initialize map is suppressed entirely.
                    if "singleton_method" in (members.get("new") or set()):
                        continue
                    callee_name = "initialize"
                else:
                    callee_name = name
                kinds = members.get(callee_name) or set()
                # Const.method dispatches on the class object: only a
                # class-level member proves the edge. Const.new constructs an
                # instance, so it requires the instance initialize.
                required_kind = "method" if name == "new" else "singleton_method"
                if required_kind in kinds:
                    _add(target_rel, callee_name, rel, caller_fn, line, "constant_receiver")
            # Every other kind (super, unresolvable member chains) is skipped.

    # Emission: sorted rels, names, and rows so the payload is byte-identical
    # across runs and input orderings. The global cap is applied in this same
    # sorted order, so WHICH rows survive the ceiling is deterministic too.
    per_callee_cap = threshold_int("CALLS_INDEX_MAX_CALLERS_PER_CALLEE")
    global_cap = threshold_int("CALLS_INDEX_MAX_TOTAL_EDGES")
    stored = 0

    callees_out: dict[str, dict[str, dict]] = {}
    for callee_rel in sorted(accum):
        names_out: dict[str, dict] = {}
        for callee_name in sorted(accum[callee_rel]):
            rows = sorted(
                accum[callee_rel][callee_name],
                # (path, line None-last, caller, grade, via): a placed call sorts
                # before an unplaced one from the same file; via is the final
                # tiebreak so a direct edge sorts before its chased twin.
                key=lambda r: (
                    r[0],
                    r[2] is None,
                    r[2] if r[2] is not None else 0,
                    r[1],
                    r[3],
                    r[4],
                ),
            )
            total = len(rows)
            keep = rows[:per_callee_cap]
            truncated = len(keep) < total
            if stored + len(keep) > global_cap:
                keep = keep[: max(0, global_cap - stored)]
                truncated = True
            stored += len(keep)
            # A contributing file that hit the dump-time site cap means the
            # recorded callers from it are a lower bound; mark the entry so
            # consumers know the total may undercount.
            if not truncated and any(p in dump_capped_rels for p, *_ in keep):
                truncated = True
            callers_out: list[dict] = []
            for p, fn, ln, g, via in keep:
                row: dict = {"path": p, "caller": fn, "line": ln, "grade": g}
                if via:
                    row["via"] = list(via)
                callers_out.append(row)
            names_out[callee_name] = {
                "callers": callers_out,
                "total": total,
                "truncated": truncated,
            }
        callees_out[callee_rel] = names_out

    return {"schema_version": SCHEMA_VERSION, "callees": callees_out}


class CallsIndex:
    """callee rel-path -> callable -> caller rows, loaded from the committed
    artifact. ``callers_of`` returns None for an unrecorded (file, name) pair
    (the consumer treats that as "no cross-file facts", never as "no
    callers")."""

    def __init__(self, callees: dict[str, dict[str, dict]]) -> None:
        self._callees = callees

    def callers_of(self, rel: str, name: str) -> dict | None:
        """``{"callers": [...], "total": int, "truncated": bool}`` or None.

        Rows are copied out so a consumer mutating its result cannot poison
        the process-global cache entry."""
        entry = (self._callees.get(rel) or {}).get(name)
        if entry is None:
            return None
        return {
            "callers": [dict(r) for r in entry["callers"]],
            "total": entry["total"],
            "truncated": entry["truncated"],
        }

    def items(self):
        """``(callee_rel, {name: entry})`` pairs for the whole index.

        The full-index walk comprehension needs: ranking callees by caller count
        (god symbols) and inverting caller rows into forward callees. The reverse
        lookup hot path uses :meth:`callers_of` instead. Entries are the live
        internal dicts; callers walk them read-only, never mutate."""
        return self._callees.items()

    def __len__(self) -> int:
        return len(self._callees)


# Process-global cache of parsed calls indexes, keyed on the artifact path,
# carrying the (mtime, size) the index was parsed at so a refresh that
# rewrites the artifact is picked up without re-reading on every call.
_CALLS_CACHE: dict[str, tuple[tuple[int, int], CallsIndex]] = {}


def load_calls_index(repo_root: Path | str | None) -> CallsIndex | None:
    """Load the committed ``calls_index.json`` for ``repo_root``, or None.

    Returns None (no facts) on any ambiguity: no repo_root, no artifact, a
    corrupt or future-schema payload, or any I/O error. The calls index only
    ADDS grounding context for the judge; failing open here means the facts
    block simply does not render -- never a crash, never a false claim.
    """
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    # Follow a linked git worktree to the main worktree's profile, the same way
    # get_pattern_context / lint_file resolve theirs. Without this, a review run
    # from a worktree (the pr-review skill's own recommended way to inspect
    # another revision) reads the worktree's absent .chameleon and every
    # blast-radius / contract-break / caller fact silently degrades to unknown --
    # a false "clean/complex" instead of the real signal. Identity off a worktree.
    from chameleon_mcp.worktree import resolve_profile_root

    root = resolve_profile_root(root)
    artifact = root / ".chameleon" / CALLS_INDEX_FILENAME
    try:
        st = os.stat(artifact)
    except OSError:
        return None
    # Derive the read ceiling from the build edge cap so the two can never
    # drift: a fixed 16MB cap rejected a legitimately-built index on a large
    # repo (a real index reached ~21MB / ~40k edges at ~555 bytes/edge, and a
    # full CALLS_INDEX_MAX_TOTAL_EDGES index reaches ~120MB), silently zeroing
    # get_callers / get_blast_radius / get_callees. ~700 bytes/edge gives
    # headroom over the observed density. This loader is tool-time + the Stop
    # judge, never the per-edit hot path, so a large read here is acceptable; it
    # still rejects a genuinely pathological (build-cap-exceeding) file.
    max_read_bytes = threshold_int("CALLS_INDEX_MAX_TOTAL_EDGES") * 700
    if not st.st_size or st.st_size > max_read_bytes:
        return None

    key = str(artifact)
    token = (int(st.st_mtime_ns), int(st.st_size))
    cached = _CALLS_CACHE.get(key)
    if cached is not None and cached[0] == token:
        return cached[1]

    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    raw_callees = data.get("callees")
    if not isinstance(raw_callees, dict):
        return None

    callees: dict[str, dict[str, dict]] = {}
    for rel, by_name in raw_callees.items():
        if not isinstance(rel, str) or not isinstance(by_name, dict):
            continue
        names: dict[str, dict] = {}
        for name, body in by_name.items():
            if not isinstance(name, str) or not isinstance(body, dict):
                continue
            raw_rows = body.get("callers")
            if not isinstance(raw_rows, list):
                continue
            rows: list[dict] = []
            for r in raw_rows:
                if not isinstance(r, dict):
                    continue
                p = r.get("path")
                g = r.get("grade")
                if not isinstance(p, str) or not isinstance(g, str) or g not in VALID_GRADES:
                    continue
                fn = r.get("caller")
                ln = r.get("line")
                raw_via = r.get("via")
                row: dict = {
                    "path": p,
                    "caller": fn if isinstance(fn, str) else "<module>",
                    "line": ln if isinstance(ln, int) else None,
                    "grade": g,
                }
                if isinstance(raw_via, list):
                    via = [v for v in raw_via if isinstance(v, str)]
                    if via:
                        row["via"] = via
                rows.append(row)
            total = body.get("total")
            if not isinstance(total, int) or isinstance(total, bool):
                total = len(rows)
            # An entry with zero surviving rows is kept when the payload says
            # so: a globally-capped entry legitimately reads "N callers, none
            # shown, truncated".
            names[name] = {
                "callers": rows,
                "total": total,
                "truncated": bool(body.get("truncated")),
            }
        if names:
            callees[rel] = names

    index = CallsIndex(callees)
    _CALLS_CACHE[key] = (token, index)
    return index
