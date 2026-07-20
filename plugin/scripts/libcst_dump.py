#!/usr/bin/env python3
"""Python AST dump — the libcst counterpart of prism_dump.rb / ts_dump.mjs.

A long-lived subprocess: reads absolute file paths on stdin (one per line) and
emits one NDJSON ``ParsedFile`` record per file on stdout, flushed per record.
The schema is the same normalized shape every chameleon extractor produces, so
the downstream clustering, archetype derivation, body-shape norms, signature
consensus, and calls index treat Python identically to TypeScript and Ruby.

Runs under the plugin's own interpreter (``sys.executable``), which is where
libcst is installed (it is a hard dependency of ``chameleon-mcp``), so a user's
repo never needs libcst on its own. libcst is a lossless CST, but here it is
used purely as a parser: this script only reads the tree, never the repo's
runtime, and it drops PYTHONPATH/PYTHONSTARTUP at the extractor boundary as
defense-in-depth (see extractors/python.py).

Two libcst specifics shape the port:
* Nodes carry no line numbers on their own; positions come from a
  ``MetadataWrapper`` + ``PositionProvider`` pass, read via ``get_metadata``.
* Top-level small statements (import/assign/expr) are wrapped in
  ``SimpleStatementLine``; ``top_level_node_kinds`` unwraps them so the kinds
  are the meaningful inner statements, while compound statements
  (``FunctionDef``/``ClassDef``/``If``/...) emit their own kind directly.
"""

from __future__ import annotations

import ast
import json
import os
import sys

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

# Pathological-file guard on the node walk. Counts libcst CST nodes, which are
# ~3.3x denser than the CPython `ast` nodes the TS/Ruby extractors' equivalent
# 50_000 cap effectively counts (measured: a 3561-line file = 17_453 ast vs
# 57_994 CST nodes). Set to 165_000 (~3.3x of 50_000) so a valid sub-MAX_FILE_SIZE
# Python file is not dropped where the line-equivalent TS/Ruby file survives;
# MAX_FILE_SIZE remains the real DoS bound.
MAX_AST_NODES = 165_000
MAX_FILE_SIZE = 1_000_000
# A real module declares a few dozen callables; cap the recorded headers so one
# outlier file cannot bloat the dump record (consensus needs a sample, not all).
MAX_CALLABLE_SIGNATURES = 200
# One file's recorded call sites are capped so a generated megafile cannot bloat
# the dump; the true total is preserved for honest truncation. A real hub
# module (a 5k-line helper) can legitimately carry several thousand sites, so
# the default leaves headroom; CHAMELEON_MAX_CALL_SITES overrides it (same
# variable and default in ts_dump.mjs / prism_dump.rb — keep the three in sync).


def _env_cap(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


MAX_CALL_SITES = _env_cap("CHAMELEON_MAX_CALL_SITES", 10_000)

# Decision points for branch_count: the cyclomatic decision set minus boolean
# operators. `elif` is a nested `If` in libcst, so it counts as another branch
# on its own. Each `except` handler and each `match` case is its own branch.
_BRANCH_TYPES = (cst.If, cst.While, cst.For, cst.ExceptHandler, cst.IfExp, cst.MatchCase)
# Nodes that also open a structural indent level (raise max_depth). `match`
# raises depth; its individual cases do not (they sit at the match's indent,
# mirroring how `when`/`case` is handled in the Ruby extractor).
_NESTING_TYPES = (cst.If, cst.While, cst.For, cst.With, cst.Try, cst.Match)


class _NodeCeilingExceeded(Exception):
    """Raised to abort a walk that exceeds MAX_AST_NODES (pathological file)."""


def _dotted_name(node) -> str | None:
    """Flatten a Name/Attribute/Call target into a dotted string.

    ``app.route`` from ``@app.route("/x")``, ``models.Model`` from a base, or a
    plain ``staticmethod``. Returns None for a target the static walk cannot name
    (a subscription, a chained call result, a lambda).
    """
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr.value}" if base else None
    if isinstance(node, cst.Call):
        return _dotted_name(node.func)
    return None


def _decorator_targets(decorators) -> list[str]:
    """Dotted target of each decorator, in source order, skipping the unnamed."""
    out = []
    for dec in decorators:
        name = _dotted_name(dec.decorator)
        if name:
            out.append(name)
    return out


def _base_names(class_node: cst.ClassDef) -> list[str]:
    """Dotted name of each base class, in declaration order.

    A subscripted generic base (``BaseRepository[User]``, ``Generic[T]``,
    ``mod.Base[X]``) is the standard typed-Python idiom, but ``_dotted_name``
    cannot name a Subscript. Dropping it silently is worse than unhelpful: a
    whole typed cohort looks base-less, so its shared base and class contract
    never derive, and a class like ``C(mod.Base[X], Mixin)`` reports ``Mixin``
    as its FIRST base -- the wrong base entirely. Unwrap the subscript and keep
    the base's own name, which is the level conventions are counted at anyway.
    """
    out = []
    for base in class_node.bases:
        node = base.value
        if isinstance(node, cst.Subscript):
            node = node.value
        name = _dotted_name(node)
        if name:
            out.append(name)
    return out


def _extends_display(bases: list[str]) -> str | None:
    """Single-string heritage summary for a class's ``extends`` field.

    Mirrors ts_dump's single-base string for the common single-inheritance
    case, but Python classes can carry multiple bases (unlike TS/JS
    ``extends``), so a class with more than one base keeps the first and
    appends a ``(+N more)`` marker rather than silently dropping the rest.
    The full list survives separately in the ``bases`` field.
    """
    if not bases:
        return None
    if len(bases) == 1:
        return bases[0]
    return f"{bases[0]} (+{len(bases) - 1} more)"


# Cap on captured class-body attribute names per class -- a presence signal, not
# an inventory, so a generated megaclass cannot bloat the record.
_MAX_CLASS_ATTRS = 50


def _class_attr_names(class_node: cst.ClassDef) -> list[str]:
    """Simple-name targets of direct class-body assignments, in source order.

    Captures only that an attribute is assigned (e.g. ``permission_classes``,
    ``queryset``, ``serializer_class``) -- never the value -- as a presence
    signal for class-level configuration. Direct class-body statements only:
    assignments inside method bodies are a new scope and are not descended.
    """
    out: list[str] = []
    try:
        statements = class_node.body.body
    except AttributeError:
        return out
    for stmt in statements:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            target = None
            if isinstance(small, cst.Assign):
                for tgt in small.targets:
                    if isinstance(tgt.target, cst.Name):
                        out.append(tgt.target.value)
            elif isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
                target = small.target.value
            if target:
                out.append(target)
            if len(out) >= _MAX_CLASS_ATTRS:
                return out[:_MAX_CLASS_ATTRS]
    return out


def _import_specifier(node) -> list[tuple[str, str]]:
    """``[module, kind]`` pairs for one Import / ImportFrom node.

    ``import x``        -> ``(x, "namespace")``   whole-module bind
    ``from m import a`` -> ``(m, "named")``        one pair per from-statement
    ``from m import *`` -> ``(m, "namespace")``
    ``from . import x`` -> ``(".", "named")``      relative dots preserved
    The module string keeps its full dotted path (``django.db``, ``fastapi``) so
    framework discrimination downstream can key on the import root.
    """
    if isinstance(node, cst.Import):
        out = []
        for alias in node.names:
            mod = _dotted_name(alias.name)
            if mod:
                out.append((mod, "namespace"))
        return out
    if isinstance(node, cst.ImportFrom):
        dots = "." * len(node.relative)
        mod = _dotted_name(node.module) if node.module is not None else ""
        target = dots + (mod or "")
        if not target:
            return []
        kind = "namespace" if isinstance(node.names, cst.ImportStar) else "named"
        return [(target, kind)]
    return []


_EMPTY_MODULE = cst.Module([])


def _code(node) -> str | None:
    """Best-effort source text of a node (annotations, bases). None on failure."""
    try:
        return _EMPTY_MODULE.code_for_node(node).strip() or None
    except Exception:
        return None


def _param_type(p: cst.Param) -> str | None:
    """Declared type-annotation text of a param (``x: int`` -> ``int``), or None.

    Pure parse, no type checker -- mirrors ts_dump's best-effort declared-type
    text for definition hydration. An unannotated param has none.
    """
    ann = getattr(p, "annotation", None)
    if ann is not None and getattr(ann, "annotation", None) is not None:
        return _code(ann.annotation)
    return None


def _param_shapes(params: cst.Parameters) -> list[dict]:
    """Structured param shape mirroring the Ruby/TS extractors.

    Each entry is ``{name, optional, kind}`` plus an optional declared ``type``
    (omitted when absent, like ts_dump) so the cross-language signature consensus
    treats all three languages the same way. ``optional`` is True when the
    binding can be dropped at a call site (a default, ``*args``, ``**kwargs``, a
    keyword-with-default).
    """
    shapes: list[dict] = []

    def _add(p: cst.Param, *, optional: bool, kind: str) -> None:
        shape: dict = {"name": p.name.value, "optional": optional, "kind": kind}
        t = _param_type(p)
        if t:
            shape["type"] = t
        shapes.append(shape)

    for p in list(params.posonly_params) + list(params.params):
        has_default = p.default is not None
        _add(p, optional=has_default, kind="optional" if has_default else "positional")
    if isinstance(params.star_arg, cst.Param):
        _add(params.star_arg, optional=True, kind="rest")
    for p in params.kwonly_params:
        _add(p, optional=p.default is not None, kind="keyword")
    if params.star_kwarg is not None:
        _add(params.star_kwarg, optional=True, kind="keyword_rest")
    return shapes


def _call_site_of(node: cst.Call) -> dict | None:
    """Classify one Call into the dump's call-site shape, or None when the callee
    can never be index-resolved (a chained-call result, a subscription, a
    ``super().__init__`` style receiver)."""
    func = node.func
    if isinstance(func, cst.Name):
        return {"name": func.value, "receiver": None, "kind": "bare"}
    if isinstance(func, cst.Attribute):
        attr = func.attr.value
        recv = func.value
        if isinstance(recv, cst.Name):
            if recv.value == "self":
                return {"name": attr, "receiver": "self", "kind": "self"}
            return {"name": attr, "receiver": recv.value, "kind": "member"}
    return None


class _Collector(cst.CSTVisitor):
    """Single-pass walker mirroring prism_dump.rb's ``walker`` lambda.

    Maintains the same enter/leave stacks (body-shape frames, enclosing classes,
    lexical nesting, enclosing def names) so a nested def is measured
    independently of its enclosing def and a method records the class + base it
    belongs to.
    """

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self) -> None:
        super().__init__()
        self.node_count = 0
        self.import_specifiers: list[list] = []
        self.function_scopes: list[dict] = []
        self.callable_signatures: list[dict] = []
        self.class_shapes: list[dict] = []
        self.call_sites: list[dict] = []
        self.call_sites_total = 0
        self.call_sites_truncated = False
        # Named-import bindings (reverse index + calls-index import grade) and
        # whole-module namespace binds (namespace-call resolution), matching the
        # ts_dump shapes the consumers expect.
        self.import_symbols: list[dict] = []
        self.namespace_imports: list[dict] = []
        self._frames: list[dict] = []
        self._classes: list[dict] = []
        self._nesting: list[str] = []
        self._defs: list[str] = []

    def _line(self, node) -> int | None:
        try:
            return self.get_metadata(PositionProvider, node).start.line
        except Exception:
            return None

    def _span(self, node) -> tuple[int | None, int | None]:
        try:
            rng = self.get_metadata(PositionProvider, node)
            return rng.start.line, rng.end.line
        except Exception:
            return None, None

    def on_visit(self, node) -> bool:
        self.node_count += 1
        if self.node_count > MAX_AST_NODES:
            raise _NodeCeilingExceeded()

        for spec in _import_specifier(node):
            self.import_specifiers.append([spec[0], spec[1]])

        if isinstance(node, cst.ImportFrom):
            self._collect_from_import(node)
        elif isinstance(node, cst.Import):
            self._collect_import(node)

        if isinstance(node, cst.Call):
            site = _call_site_of(node)
            if site is not None:
                self.call_sites_total += 1
                if len(self.call_sites) < MAX_CALL_SITES:
                    site["line"] = self._line(node)
                    site["caller"] = self._defs[-1] if self._defs else "<module>"
                    self.call_sites.append(site)
                else:
                    self.call_sites_truncated = True

        if isinstance(node, cst.ClassDef):
            name = node.name.value
            bases = _base_names(node)
            path = ".".join(self._nesting + [name])
            # Capped like callable_signatures and call_sites: a generated megafile
            # can declare thousands of trivial classes, and the recorded heritage
            # sample feeds class-contract derivation, which needs a sample not all.
            # ts_dump gates the equivalent push on the same MAX_CALLABLE_SIGNATURES.
            if len(self.class_shapes) < MAX_CALLABLE_SIGNATURES:
                self.class_shapes.append(
                    {
                        "name": name,
                        # start_line lets the symbol index record a searchable
                        # class definition (name -> file:line) alongside callables.
                        "start_line": self._line(node),
                        "bases": bases,
                        # `extends` mirrors ts_dump's single-base string so consumers
                        # that read the TS-shaped class_shapes pick up the base too;
                        # a class with more than one base keeps the full list in
                        # `bases` and gets a `(+N more)` marker here so multiple
                        # inheritance is never silently reduced to one name.
                        "extends": _extends_display(bases),
                        "decorators": _decorator_targets(node.decorators),
                        # Presence of class-level config attributes (e.g. DRF's
                        # permission_classes) -- target names only, no values.
                        "class_attrs": _class_attr_names(node),
                    }
                )
            self._classes.append({"name": name, "base": bases[0] if bases else None, "path": path})
            self._nesting.append(name)
        elif isinstance(node, cst.FunctionDef):
            self._enter_function(node)
        elif self._frames:
            frame = self._frames[-1]
            if isinstance(node, _BRANCH_TYPES):
                frame["branch_count"] += 1
            if isinstance(node, _NESTING_TYPES):
                frame["depth"] += 1
                frame["max_depth"] = max(frame["max_depth"], frame["depth"])

        return True

    def on_leave(self, node) -> None:
        if isinstance(node, cst.FunctionDef):
            self._defs.pop()
            frame = self._frames.pop()
            self.function_scopes.append(
                {
                    "start_line": frame["start_line"],
                    "end_line": frame["end_line"],
                    "line_span": frame["line_span"],
                    "max_depth": frame["max_depth"],
                    "branch_count": frame["branch_count"],
                    "param_count": frame["param_count"],
                }
            )
        elif self._frames and isinstance(node, _NESTING_TYPES):
            self._frames[-1]["depth"] -= 1

        if isinstance(node, cst.ClassDef):
            self._classes.pop()
            self._nesting.pop()

    def _enter_function(self, node: cst.FunctionDef) -> None:
        start, end = self._span(node)
        params = _param_shapes(node.params)
        self._frames.append(
            {
                "start_line": start,
                "end_line": end,
                "line_span": (end - start + 1) if (start and end) else None,
                "param_count": len(params),
                "max_depth": 0,
                "branch_count": 0,
                "depth": 0,
            }
        )
        self._defs.append(node.name.value)

        decorators = _decorator_targets(node.decorators)
        enclosing = self._classes[-1] if self._classes else None
        if "staticmethod" in decorators:
            kind = "staticmethod"
        elif "classmethod" in decorators:
            kind = "classmethod"
        elif enclosing is not None:
            kind = "method"
        else:
            kind = "function"

        if len(self.callable_signatures) < MAX_CALLABLE_SIGNATURES:
            sig: dict = {
                "name": node.name.value,
                "kind": kind,
                "params": params,
                "is_default_export": False,
                # `async def` vs `def` changes the caller's required syntax (an
                # unawaited coroutine silently no-ops; `await` on a sync callable
                # is a SyntaxError), so it must survive as its own field rather
                # than folding into `kind` -- `kind` values are matched elsewhere
                # (contract derivation, function_catalog) against a fixed
                # function/method/staticmethod/classmethod set.
                "is_async": node.asynchronous is not None,
                "enclosing_class": enclosing["name"] if enclosing else None,
                "enclosing_class_path": enclosing["path"] if enclosing else None,
                "base_class": enclosing["base"] if enclosing else None,
                "decorators": decorators,
                "start_line": start,
                "end_line": end,
            }
            # Declared return-type text (`def f() -> int`), omitted when absent,
            # for definition hydration -- the ts_dump `return_type` analogue.
            if node.returns is not None:
                rt = _code(node.returns.annotation)
                if rt:
                    sig["return_type"] = rt
            self.callable_signatures.append(sig)

    def _collect_from_import(self, node: cst.ImportFrom) -> None:
        """`from m import a, b as c` -> import_symbols rows {name, local, module, line}."""
        if isinstance(node.names, cst.ImportStar):
            return
        dots = "." * len(node.relative)
        mod = _dotted_name(node.module) if node.module is not None else ""
        module = dots + (mod or "")
        if not module:
            return
        line = self._line(node)
        for alias in node.names:
            name = _dotted_name(alias.name)
            if not name:
                continue
            local = name
            if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                local = alias.asname.name.value
            self.import_symbols.append(
                {"name": name, "local": local, "module": module, "line": line}
            )

    def _collect_import(self, node: cst.Import) -> None:
        """`import m`, `import a.b as x` -> namespace_imports rows {alias, module, line}.

        The bound name is the asname when present, else the top package segment
        (`import a.b` binds `a`), so namespace-call resolution can key on it.
        """
        line = self._line(node)
        for alias in node.names:
            module = _dotted_name(alias.name)
            if not module:
                continue
            if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                bound = alias.asname.name.value
            else:
                bound = module.split(".")[0]
            self.namespace_imports.append({"alias": bound, "module": module, "line": line})


def _top_level_kinds(module: cst.Module) -> list[str]:
    """Unwrap SimpleStatementLine so top-level kinds are the meaningful inner
    statements (Import/ImportFrom/Assign), while compound statements emit their
    own kind."""
    kinds: list[str] = []
    for stmt in module.body:
        if isinstance(stmt, cst.SimpleStatementLine):
            kinds.extend(type(small).__name__ for small in stmt.body)
        else:
            kinds.append(type(stmt).__name__)
    return kinds


def _module_exports(module: cst.Module, file_path: str | None = None) -> tuple[list[str], bool]:
    """The names importable from this module, and whether the set is open.

    Python has no `export` keyword: every top-level binding is importable, so the
    set is the names bound at module level -- def/class names, assignment
    targets, and import locals (re-exports). Bindings inside a top-level
    conditional/loop/context block (``try/except`` import fallbacks, ``if
    TYPE_CHECKING``, version gates) are module-level too, so the walk descends
    into those compound bodies; a def/class body is a new scope and is NOT
    descended. An ``__init__`` module additionally re-exports its sibling
    submodules (``from pkg import submodule`` resolves to ``pkg/submodule.py`` on
    disk regardless of what ``__init__`` names), so their basenames are added.
    The set is OPEN (non-authoritative) on ``from X import *``. Mirrors the
    purpose of ts_dump's named_export_names / export_set_open for the
    phantom-symbol existence check.
    """
    names: set[str] = set()
    open_set = False

    def _add_targets(small) -> None:
        if isinstance(small, cst.Assign):
            for t in small.targets:
                if isinstance(t.target, cst.Name):
                    names.add(t.target.value)
        elif isinstance(small, cst.AnnAssign) and isinstance(small.target, cst.Name):
            names.add(small.target.value)

    def _process_simple(stmt) -> None:
        nonlocal open_set
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom):
                if isinstance(small.names, cst.ImportStar):
                    open_set = True
                else:
                    for alias in small.names:
                        local = alias.asname.name if alias.asname else alias.name
                        if isinstance(local, cst.Name):
                            names.add(local.value)
            elif isinstance(small, cst.Import):
                for alias in small.names:
                    if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                        names.add(alias.asname.name.value)
                    else:
                        dotted = _dotted_name(alias.name)
                        if dotted:
                            names.add(dotted.split(".")[0])
            else:
                _add_targets(small)

    _try_types = (cst.Try, getattr(cst, "TryStar", cst.Try))

    def _walk(statements) -> None:
        for stmt in statements:
            if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
                names.add(stmt.name.value)
            elif isinstance(stmt, cst.SimpleStatementLine):
                _process_simple(stmt)
            elif isinstance(stmt, cst.If):
                _walk(stmt.body.body)
                orelse = stmt.orelse
                if isinstance(orelse, cst.If):
                    _walk([orelse])
                elif isinstance(orelse, cst.Else):
                    _walk(orelse.body.body)
            elif isinstance(stmt, _try_types):
                _walk(stmt.body.body)
                for handler in stmt.handlers:
                    _walk(handler.body.body)
                if stmt.orelse is not None:
                    _walk(stmt.orelse.body.body)
                if stmt.finalbody is not None:
                    _walk(stmt.finalbody.body)
            elif isinstance(stmt, (cst.For, cst.While)):
                _walk(stmt.body.body)
                if stmt.orelse is not None:
                    _walk(stmt.orelse.body.body)
            elif isinstance(stmt, cst.With):
                _walk(stmt.body.body)

    try:
        _walk(module.body)
    except Exception:
        # Unexpected node shape: keep what was collected, never crash the dump.
        pass

    if file_path is not None:
        base = os.path.basename(file_path)
        if base in ("__init__.py", "__init__.pyi"):
            try:
                pkg_dir = os.path.dirname(file_path)
                # A PEP 562 module-level __getattr__ can export names that cannot
                # be enumerated statically, so the package's export set is open --
                # otherwise adding the enumerable siblings would flip a sparse
                # __init__ to a CLOSED set that false-flags a lazily-exported name.
                if "__getattr__" in names:
                    open_set = True
                for entry in os.listdir(pkg_dir):
                    if entry.startswith("__"):
                        continue
                    if entry.endswith((".py", ".pyi")):
                        names.add(entry.rsplit(".", 1)[0])
                    elif entry.endswith((".so", ".pyd")):
                        # Compiled extension submodule; the module name is the first
                        # dot segment (platform/ABI tags follow, e.g.
                        # _speedups.cpython-311-darwin.so).
                        names.add(entry.split(".", 1)[0])
                    elif os.path.isfile(
                        os.path.join(pkg_dir, entry, "__init__.py")
                    ) or os.path.isfile(os.path.join(pkg_dir, entry, "__init__.pyi")):
                        names.add(entry)
            except OSError:
                pass

    return sorted(names), open_set


def _recover_with_ast(file_path: str, content: str) -> dict | None:
    """Degraded record from the running interpreter's parser when libcst rejects.

    libcst's grammar is coupled to its pinned release, so valid Python written
    in syntax newer than that grammar is refused wholesale even though the
    interpreter running this dump accepts it. Rather than drop such a file (the
    TS/Ruby dumps keep contributing through a bounded number of recoverable parse
    errors), re-parse with stdlib ``ast`` and, when it succeeds, emit the
    import/export surface so the file still contributes its symbol hints. The
    body-shape, signature, and call-site fields stay empty: that path is the
    libcst CST walk, which this tree cannot drive. ``parse_diagnostics_count`` is
    1 to mark the record as recovered, not a clean parse. The export set is left
    OPEN: this scan only reads top-level bindings, not the conditional bodies
    (``if TYPE_CHECKING``, ``try/except ImportError`` fallbacks) the authoritative
    ``_module_exports`` walk descends into, so the recovered name set is a
    non-authoritative hint and must never drive the phantom-symbol absence check.
    Returns None when the interpreter also rejects the file (a genuine syntax
    error, not grammar skew).
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return None

    names: set[str] = set()
    import_specifiers: list[list] = []
    top_level_kinds: list[str] = []

    for stmt in tree.body:
        top_level_kinds.append(type(stmt).__name__)
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name:
                    import_specifiers.append([alias.name, "namespace"])
                bound = alias.asname or (alias.name.split(".")[0] if alias.name else None)
                if bound:
                    names.add(bound)
        elif isinstance(stmt, ast.ImportFrom):
            dots = "." * (stmt.level or 0)
            target = dots + (stmt.module or "")
            if target:
                star = any(a.name == "*" for a in stmt.names)
                import_specifiers.append([target, "namespace" if star else "named"])
            for alias in stmt.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)

    top_classes = [s for s in tree.body if isinstance(s, ast.ClassDef)]
    top_funcs = [s for s in tree.body if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)]
    if len(top_classes) == 1 and not top_funcs:
        default_export_kind = "ClassDef"
    elif len(top_funcs) == 1 and not top_classes:
        default_export_kind = "FunctionDef"
    else:
        default_export_kind = None

    return {
        "path": file_path,
        "content_first_200_bytes": content[:200],
        "top_level_node_kinds": top_level_kinds,
        "default_export_kind": default_export_kind,
        "named_export_count": len(top_classes) + len(top_funcs),
        "named_export_names": sorted(names),
        # Open: a top-level-only scan cannot authoritatively enumerate the closed
        # export set, so the absence check must skip this recovered record.
        "export_set_open": True,
        "import_specifiers": import_specifiers,
        "import_symbols": [],
        "namespace_imports": [],
        "has_jsx": False,
        "parse_diagnostics_count": 1,
        "function_scopes": [],
        "callable_signatures": [],
        "class_shapes": [],
        "call_sites": [],
        "call_sites_total": 0,
        "call_sites_truncated": False,
    }


def extract_file(file_path: str) -> dict:
    try:
        stat = os.lstat(file_path)
    except OSError as e:
        return {"path": file_path, "error": "read_error", "message": str(e)}

    if os.path.islink(file_path):
        return {"path": file_path, "error": "symlink_refused"}

    if stat.st_size > MAX_FILE_SIZE:
        return {"path": file_path, "error": "file_too_large", "size": stat.st_size}

    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as e:
        return {"path": file_path, "error": "read_error", "message": str(e)}

    try:
        module = cst.parse_module(content)
    except cst.ParserSyntaxError as e:
        recovered = _recover_with_ast(file_path, content)
        if recovered is not None:
            return recovered
        return {"path": file_path, "error": "parse_error", "message": str(e)}
    except Exception as e:  # pragma: no cover - defensive: any non-syntax parse fault
        recovered = _recover_with_ast(file_path, content)
        if recovered is not None:
            return recovered
        return {"path": file_path, "error": "parse_error", "message": str(e)}

    collector = _Collector()
    try:
        MetadataWrapper(module, unsafe_skip_copy=True).visit(collector)
    except _NodeCeilingExceeded:
        return {"path": file_path, "error": "ast_node_ceiling_exceeded"}
    except RecursionError as e:
        return {"path": file_path, "error": "walk_error", "message": str(e)}
    except Exception as e:
        # A metadata-resolution fault or an unexpected node shape during the walk
        # is a walk-time failure, not a top-level extractor crash. Classify it as
        # walk_error to match ts_dump / prism_dump, which already distinguish the
        # two so the skipped-file reason is actionable.
        return {"path": file_path, "error": "walk_error", "message": str(e)}

    top_classes = [s for s in module.body if isinstance(s, cst.ClassDef)]
    top_funcs = [s for s in module.body if isinstance(s, cst.FunctionDef)]
    if len(top_classes) == 1 and not top_funcs:
        default_export_kind = "ClassDef"
    elif len(top_funcs) == 1 and not top_classes:
        default_export_kind = "FunctionDef"
    else:
        default_export_kind = None

    named_export_names, export_set_open = _module_exports(module, file_path)

    return {
        "path": file_path,
        "content_first_200_bytes": content[:200],
        "top_level_node_kinds": _top_level_kinds(module),
        "default_export_kind": default_export_kind,
        "named_export_count": len(top_classes) + len(top_funcs),
        "named_export_names": named_export_names,
        "export_set_open": export_set_open,
        "import_specifiers": collector.import_specifiers,
        "import_symbols": collector.import_symbols,
        "namespace_imports": collector.namespace_imports,
        "has_jsx": False,
        "parse_diagnostics_count": 0,
        "function_scopes": collector.function_scopes,
        "callable_signatures": collector.callable_signatures,
        "class_shapes": collector.class_shapes,
        "call_sites": collector.call_sites,
        "call_sites_total": collector.call_sites_total,
        "call_sites_truncated": collector.call_sites_truncated,
    }


def main() -> None:
    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        try:
            record = extract_file(path)
        except RecursionError:
            record = {"path": path, "error": "walk_error", "message": "recursion limit"}
        except Exception as e:  # noqa: BLE001 - per-file crash guard, never abort the corpus
            record = {"path": path, "error": "extractor_crash", "message": str(e)}
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
