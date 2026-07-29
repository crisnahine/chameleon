"""Python kind tables, matching what libcst_dump.py emits.

Two things make Python differ from Ruby beyond vocabulary. Its top-level kinds
are libcst CST class names with small statements UNWRAPPED (a bare
``import os`` reports ``Import``, not the ``SimpleStatementLine`` wrapping it),
and its call-site classification is far stricter than Prism's: only a bare name
or a single-name receiver is recorded, so a chained ``a.b().c()`` contributes
one row rather than two.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

language = "python"

# tree-sitter rule -> libcst CST class name. `expression_statement` is NOT here:
# it is the SimpleStatementLine analogue and gets unwrapped by
# refine_top_level_kind into the kind of the statement it holds.
TOP_LEVEL_KINDS: dict[str, str] = {
    "function_definition": "FunctionDef",
    "decorated_definition": "FunctionDef",
    "class_definition": "ClassDef",
    "import_statement": "Import",
    "import_from_statement": "ImportFrom",
    "future_import_statement": "ImportFrom",
    "expression_statement": "Expr",
    "type_alias_statement": "TypeAlias",
    "if_statement": "If",
    "for_statement": "For",
    "while_statement": "While",
    "try_statement": "Try",
    "with_statement": "With",
    "match_statement": "Match",
    "return_statement": "Return",
    "raise_statement": "Raise",
    "assert_statement": "Assert",
    "delete_statement": "Del",
    "global_statement": "Global",
    "nonlocal_statement": "Nonlocal",
    "pass_statement": "Pass",
    "break_statement": "Break",
    "continue_statement": "Continue",
    "comment": "",
}

# `decorated_definition` counts as a function node so a decorator's own calls
# land inside the decorated function's frame: libcst models decorators as
# attributes OF the FunctionDef, so `@router.post(...)` is attributed to the
# function, not to `<module>`. callable_signature returns None when the wrapper
# holds a class, letting the inner class_definition be handled normally.
FUNCTION_NODES = frozenset({"function_definition", "decorated_definition"})
CLASS_NODES = frozenset({"class_definition"})

# libcst _BRANCH_TYPES: If, While, For, ExceptHandler, IfExp, MatchCase.
BRANCH_NODES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "while_statement",
        "for_statement",
        "except_clause",
        "except_group_clause",
        "conditional_expression",
        "case_clause",
    }
)

# libcst _NESTING_TYPES: If, While, For, With, Try, Match. Narrower than the
# branch set -- an except handler and a match case score a decision point but
# sit at their statement's own indent.
NESTING_NODES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "while_statement",
        "for_statement",
        "with_statement",
        "try_statement",
        "match_statement",
    }
)

SKIP_SUBTREE_NODES = frozenset({"comment", "string_content"})

# Top-level blocks libcst's export scan descends into.
# Clause and body nodes that hold statements but are not statements themselves.
_BLOCK_WRAPPERS = frozenset(
    {"block", "else_clause", "elif_clause", "except_clause", "finally_clause", "case_clause"}
)

_CONTROL_FLOW_BLOCKS = frozenset(
    {
        "if_statement",
        "try_statement",
        "with_statement",
        # libcst descends loop bodies as well: a name assigned inside a
        # top-level `for` is importable exactly like a flat one.
        "for_statement",
        "while_statement",
    }
)

# ts_dump and libcst both omit the lexical nesting list Prism attaches.
call_site_carries_nesting = False

# libcst's class_shapes row carries no qualified name; only Prism emits one.
class_shape_carries_qualified = False

# Python nests with a dot; `::` is Prism's spelling.
qualified_name_separator = "."


def _name_of(node: Any, text: Callable[[Any], str]) -> str | None:
    name = node.child_by_field_name("name")
    return text(name) if name is not None else None


def _decorators_of(node: Any, text: Callable[[Any], str]) -> list[str]:
    """Decorator NAMES on a decorated_definition wrapper.

    libcst records the decorator's callee, not its source: `@router.post("/x",
    status_code=201)` is reported as `router.post`. Keeping the argument list
    would make every parameterized route decorator a unique string, which the
    framework-role consumers match against exactly.
    """
    out: list[str] = []
    for child in node.named_children:
        if child.type != "decorator":
            continue
        inner = child.named_children[0] if child.named_children else None
        if inner is not None and inner.type == "call":
            callee = inner.child_by_field_name("function")
            out.append(text(callee) if callee is not None else text(inner))
        elif inner is not None:
            out.append(text(inner))
        else:
            out.append(text(child).lstrip("@").strip())
    return out


def refine_top_level_kind(node: Any, text: Callable[[Any], str], mapped: str) -> str:
    """Unwrap the small-statement wrapper and the decorator wrapper.

    libcst reports the INNER statement of a SimpleStatementLine, so a top-level
    `x = 1` is `Assign` and `x: int = 1` is `AnnAssign` -- never the wrapper.
    tree-sitter's `expression_statement` plays the wrapper role, and
    `decorated_definition` hides whether the decorated thing is a function or a
    class, which the cluster signature distinguishes.
    """
    if node.type == "type_alias_statement":
        # `type` is a soft keyword, so `type(obj).attr = v` is swallowed whole as
        # a PEP 695 alias even though it is an ordinary assignment. A real alias
        # binds a bare identifier, so a target opening with `(` is the misparse.
        left = node.child_by_field_name("left")
        return "Assign" if left is not None and text(left).startswith("(") else mapped

    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None:
            return "ClassDef" if inner.type == "class_definition" else "FunctionDef"
        return mapped
    if node.type != "expression_statement":
        return mapped
    if not node.named_children:
        return mapped
    inner = node.named_children[0]
    if inner.type == "assignment":
        # An annotated assignment carries a `type` field; libcst names the two
        # differently and the kind reaches the cluster signature.
        return "AnnAssign" if inner.child_by_field_name("type") is not None else "Assign"
    if inner.type == "augmented_assignment":
        return "AugAssign"
    return "Expr"


def export_fields(top_level_kinds: list[str]) -> tuple[str | None, int]:
    """Python has no export statement, so libcst repurposes both fields.

    The default is the SOLE top-level definition and only when it is unopposed:
    one class and no functions, or one function and no classes -- a module with
    both reports None. The count is classes plus functions; imports and
    assignments contribute nothing.
    """
    classes = sum(1 for k in top_level_kinds if k == "ClassDef")
    funcs = sum(1 for k in top_level_kinds if k == "FunctionDef")
    if classes == 1 and funcs == 0:
        default = "ClassDef"
    elif funcs == 1 and classes == 0:
        default = "FunctionDef"
    else:
        default = None
    return default, classes + funcs


# libcst caps the class-attribute presence list; a config class with 53 direct
# assignments reports 50, so an uncapped list disagrees on exactly the repos
# where the signal matters least.
_MAX_CLASS_ATTRS = 50


def _extends_display(bases: list[str]) -> str | None:
    """Single-string heritage summary, mirroring ts_dump's single-base field.

    Python allows multiple bases where TS `extends` allows one, so a class with
    several keeps the first and appends a `(+N more)` marker rather than
    silently dropping the rest -- the full list survives in `bases`.
    """
    if not bases:
        return None
    if len(bases) == 1:
        return bases[0]
    return f"{bases[0]} (+{len(bases) - 1} more)"


def _assignment_targets(node: Any, text: Callable[[Any], str]) -> list[str]:
    """Every simple name an assignment binds, in source order.

    A chained assignment (`root_doc = master_doc = "index"`) binds BOTH names:
    libcst's Assign carries a `targets` LIST, while the grammar exposes only a
    single `left` field with the rest as further children, so reading `left`
    alone silently drops every name after the first.
    """
    out: list[str] = []
    current = node
    # The grammar NESTS a chain rather than listing it: `a = b = "x"` is
    # assignment(left=a, right=assignment(left=b, right="x")), so each extra
    # target sits one level deeper on the right.
    while current is not None and current.type == "assignment":
        target = current.child_by_field_name("left")
        if target is not None and target.type == "identifier":
            out.append(text(target))
        current = current.child_by_field_name("right")
    return out


def _aliased(node: Any, text: Callable[[Any], str]) -> tuple[str, str] | None:
    """(imported name, local binding) for one import target."""
    if node.type == "aliased_import":
        name = node.child_by_field_name("name")
        alias = node.child_by_field_name("alias")
        if name is None:
            return None
        return text(name), text(alias) if alias is not None else text(name)
    if node.type in ("dotted_name", "identifier"):
        return text(node), text(node)
    return None


def import_bindings(
    node: Any, text: Callable[[Any], str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(import_symbols, namespace_imports) rows for ONE import node.

    Called per node rather than over the root's children: a function-local
    `import x`, and one inside `if TYPE_CHECKING:`, bind names exactly as a
    top-level import does, and libcst's visitor records them wherever they sit.
    A top-level-only scan missed every nested import in a real repo.
    """
    symbols: list[dict[str, Any]] = []
    namespaces: list[dict[str, Any]] = []
    line = node.start_point[0] + 1

    if node.type == "import_statement":
        for child in node.named_children:
            pair = _aliased(child, text)
            if pair is None:
                continue
            module, local = pair
            bound = local if child.type == "aliased_import" else module.split(".")[0]
            namespaces.append({"alias": bound, "module": module, "line": line})
        return symbols, namespaces

    if node.type in ("import_from_statement", "future_import_statement"):
        module_node = node.child_by_field_name("module_name")
        if node.type == "future_import_statement":
            module = "__future__"
        else:
            module = text(module_node) if module_node is not None else ""
        for child in node.named_children:
            if child.type == "wildcard_import":
                continue
            if module_node is not None and child.id == module_node.id:
                continue
            pair = _aliased(child, text)
            if pair is None:
                continue
            name, local = pair
            symbols.append({"name": name, "local": local, "module": module, "line": line})
    return symbols, namespaces


def file_extras(
    root: Any, text: Callable[[Any], str], path: Any, walked: Any = None
) -> dict[str, Any]:
    """The four whole-file cross-file-intelligence rows libcst emits.

    These drive the exports index, the reverse index, and the phantom-symbol
    check -- the machinery that answers "does this imported name actually exist
    in that module". They are file properties, not node properties: the export
    set is a union over every top-level binding, and one `import *` opens it.
    """
    import_symbols: list[dict[str, Any]] = []
    namespace_imports: list[dict[str, Any]] = []
    names: set[str] = set()
    export_set_open = False

    def bind_from(stmt: Any) -> None:
        nonlocal export_set_open
        line = stmt.start_point[0] + 1

        if stmt.type == "import_statement":
            for child in stmt.named_children:
                pair = _aliased(child, text)
                if pair is None:
                    continue
                module, local = pair
                # `import a.b` binds only the TOP package unless aliased.
                bound = local if child.type == "aliased_import" else module.split(".")[0]
                namespace_imports.append({"alias": bound, "module": module, "line": line})
                names.add(bound)
            return

        if stmt.type in ("import_from_statement", "future_import_statement"):
            module_node = stmt.child_by_field_name("module_name")
            if stmt.type == "future_import_statement":
                module = "__future__"
            else:
                module = text(module_node) if module_node is not None else ""
            for child in stmt.named_children:
                if child.type == "wildcard_import":
                    # The imported set is unknowable, so the module's own export
                    # set has to be treated as open or the symbol check would
                    # report every name it cannot see as a hallucination.
                    export_set_open = True
                    continue
                if module_node is not None and child.id == module_node.id:
                    continue
                pair = _aliased(child, text)
                if pair is None:
                    continue
                name, local = pair
                import_symbols.append(
                    {"name": name, "local": local, "module": module, "line": line}
                )
                names.add(local)
            return

        kind = _definition_name(stmt, text)
        if kind is not None:
            names.add(kind)
            return

        if stmt.type == "expression_statement" and stmt.named_children:
            inner = stmt.named_children[0]
            if inner.type == "assignment":
                names.update(_assignment_targets(inner, text))

    # libcst's _module_exports descends top-level if/try/with/for/while, so a name bound
    # under `if TYPE_CHECKING:` or in a try/except import fallback is exported
    # exactly like a flat one.
    pending = list(root.named_children)
    while pending:
        stmt = pending.pop(0)
        # Wrappers are flattened on POP rather than only when met as a direct
        # child: an `elif_clause` holds its own `block`, so pushing that block
        # whole and reading it as a statement bound nothing inside any elif --
        # a module-level `elif ...: import boto3` exported no name at all.
        if stmt.type in _CONTROL_FLOW_BLOCKS or stmt.type in _BLOCK_WRAPPERS:
            pending[:0] = list(stmt.named_children)
            continue
        bind_from(stmt)

    # A package `__init__.py` also exports its SUBMODULES: `from pkg import
    # sub` resolves against the directory, not against anything written in the
    # file, so the symbol check needs the sibling names too.
    if getattr(path, "name", "") in ("__init__.py", "__init__.pyi"):
        try:
            # A PEP 562 module-level `__getattr__` can export names no static
            # scan can enumerate, so listing the siblings would otherwise flip a
            # deliberately sparse package to a CLOSED set and false-flag every
            # lazily-exported name.
            if "__getattr__" in names:
                export_set_open = True
            for sibling in path.parent.iterdir():
                # Only DUNDER entries are skipped. A single leading underscore
                # means private by convention, not absent: `from pkg import
                # _version` resolves exactly like any other submodule, and
                # skipping it dropped a real name from the export set.
                if sibling.name.startswith("__"):
                    continue
                if sibling.suffix in (".py", ".pyi"):
                    names.add(sibling.stem)
                elif sibling.suffix in (".so", ".pyd"):
                    # A compiled extension module carries platform/ABI tags after
                    # the first dot (`_speedups.cpython-311-darwin.so`), so the
                    # module name is the first segment rather than the stem.
                    names.add(sibling.name.split(".", 1)[0])
                elif sibling.is_dir() and (
                    (sibling / "__init__.py").exists() or (sibling / "__init__.pyi").exists()
                ):
                    names.add(sibling.name)
        except OSError:
            pass

    if walked is not None:
        # The walker saw EVERY import, nested ones included; the local lists
        # above only ever held the reachable-from-root subset.
        # Rows only -- NOT names. A function-local `import code` binds inside
        # that function; it is not importable from the module, so it belongs in
        # import_symbols and never in the export set.
        import_symbols = list(walked.import_symbols)
        namespace_imports = list(walked.namespace_imports)

    return {
        "import_symbols": import_symbols,
        "namespace_imports": namespace_imports,
        "named_export_names": sorted(names),
        "export_set_open": export_set_open,
    }


def _definition_name(stmt: Any, text: Callable[[Any], str]) -> str | None:
    """The bound name of a top-level def/class, seeing through decorators."""
    node = stmt
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is None:
            return None
        node = inner
    if node.type in ("function_definition", "class_definition"):
        return _name_of(node, text)
    return None


def class_shape(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Shape row for a class, carrying both `bases` and a TS-shaped `extends`.

    The consumer reads `extends or bases[0]`, so both are emitted: `bases` is
    the honest list, `extends` the first entry for the shared code path.
    """
    name = _name_of(node, text)
    if name is None:
        return None

    bases: list[str] = []
    arglist = node.child_by_field_name("superclasses")
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type in ("identifier", "attribute"):
                bases.append(text(arg))
            elif arg.type == "subscript":
                # A generic base (`list[object]`, `types.TypeDecorator[object]`)
                # is reported by its NAME; libcst drops the subscript, so keeping
                # it would make every parameterized base its own distinct string.
                value = arg.child_by_field_name("value")
                if value is not None:
                    bases.append(text(value))

    parent = node.parent
    decorators = (
        _decorators_of(parent, text)
        if parent is not None and parent.type == "decorated_definition"
        else []
    )

    # No `kind` key: libcst's class_shapes rows carry name/start_line/bases/
    # extends/decorators only, and `decorators` is always present (empty when
    # undecorated) rather than omitted.
    shape: dict[str, Any] = {
        "name": name,
        "start_line": node.start_point[0] + 1,
        "bases": bases,
        "extends": _extends_display(bases),
        # The RAW first base, kept separate from the display form: a method's
        # `base_class` is bases[0], never the `(+N more)` summary string.
        "_context_base": bases[0] if bases else None,
        "decorators": decorators,
    }

    # Class-level config attributes (permission_classes, queryset) are the
    # Python-exclusive DRF/Django authz signal the class contract reads.
    body = node.child_by_field_name("body")
    attrs: list[str] = []
    if body is not None:
        for stmt in body.named_children:
            if stmt.type != "expression_statement" or not stmt.named_children:
                continue
            inner = stmt.named_children[0]
            if inner.type != "assignment":
                continue
            for bound in _assignment_targets(inner, text):
                if len(attrs) >= _MAX_CLASS_ATTRS:
                    break
                attrs.append(bound)
    # Always present, empty when the class body declares no attributes: the
    # DRF/Django authz consumer reads the key unconditionally.
    shape["class_attrs"] = attrs
    return shape


def class_body_calls(
    node: Any, text: Callable[[Any], str], class_name: str
) -> list[dict[str, Any]]:
    """Python has no receiverless class-body DSL; Ruby's macro concept is n/a."""
    return []


_PARAM_KINDS = {
    "identifier": ("positional", False),
    "typed_parameter": ("positional", False),
    "default_parameter": ("optional", True),
    "typed_default_parameter": ("optional", True),
    "list_splat_pattern": ("rest", True),
    "dictionary_splat_pattern": ("keyword_rest", True),
    "keyword_separator": (None, False),
    "positional_separator": (None, False),
}


def _typed_splat_kind(param: Any) -> str | None:
    """The splat kind of an ANNOTATED variadic, or None.

    `*args: T` wraps the splat pattern in a `typed_parameter`, so the bare
    `list_splat_pattern` lookup misses it and the parameter reads as a plain
    positional named `_`. The untyped form has no wrapper, which is why only
    annotated variadics diverged.
    """
    if param.type != "typed_parameter":
        return None
    for child in param.named_children:
        if child.type == "list_splat_pattern":
            return "rest"
        if child.type == "dictionary_splat_pattern":
            return "keyword_rest"
    return None


def _param_name(param: Any, text: Callable[[Any], str]) -> str:
    if param.type == "identifier":
        return text(param)
    name = param.child_by_field_name("name")
    if name is not None:
        return text(name)
    for child in param.named_children:
        if child.type == "identifier":
            return text(child)
        # A typed variadic hides its binding one level down, inside the splat.
        if child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            for inner in child.named_children:
                if inner.type == "identifier":
                    return text(inner)
    return "_"


def _param_type(param: Any, text: Callable[[Any], str]) -> str | None:
    ann = param.child_by_field_name("type")
    return text(ann) if ann is not None else None


def _dedent_annotation(value: str | None, base_col: int) -> str | None:
    """Match libcst's continuation indent for a WRAPPED annotation.

    libcst renders an annotation detached from its block, so the def-column
    level its ParenthesizedWhitespace re-adds is dropped: each continuation
    line ends up indented by (physical - def column). Raw source text keeps the
    physical indent, so a method's multi-line union type differs by exactly the
    class body's indentation.
    """
    if value is None or base_col <= 0 or "\n" not in value:
        return value
    first, *rest = value.split("\n")
    out = [first]
    for line in rest:
        cut = 0
        while cut < base_col and cut < len(line) and line[cut] in " \t":
            cut += 1
        out.append(line[cut:])
    return "\n".join(out)


def callable_signature(
    node: Any,
    text: Callable[[Any], str],
    class_ctx: tuple[str, str, str | None, tuple[str, ...]] | None,
) -> dict[str, Any] | None:
    """Signature row for a def, carrying declared types and decorators."""
    decorators: list[str] = []
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is None or inner.type != "function_definition":
            # A decorated CLASS: no frame here, the inner class_definition is
            # handled on its own.
            return None
        decorators = _decorators_of(node, text)
        node = inner
    elif node.parent is not None and node.parent.type == "decorated_definition":
        # Already emitted through the wrapper; emitting again would duplicate
        # the signature and open a second frame over the same body.
        return None

    name = _name_of(node, text)
    if name is None:
        return None

    # libcst reduces a wrapped annotation's continuation indent by the def's
    # own column; reproduce that when rendering a multi-line type.
    base_col = node.start_point[1]
    params: list[dict[str, Any]] = []
    plist = node.child_by_field_name("parameters")
    if plist is not None:
        # A bare `*` (keyword_separator) or any splat opens the keyword-only
        # region: libcst files every following param under kwonly_params with
        # kind "keyword", whatever its own node type suggests.
        keyword_only = False
        for param in plist.named_children:
            if param.type == "comment":
                continue
            splat = _typed_splat_kind(param)
            if splat is not None:
                kind, optional = splat, True
            else:
                entry = _PARAM_KINDS.get(param.type)
                kind, optional = entry if entry else ("positional", False)
            if param.type == "keyword_separator" or kind == "rest":
                keyword_only = True
            if kind is None:
                continue
            if keyword_only and kind in ("positional", "optional"):
                kind = "keyword"
            shape: dict[str, Any] = {
                "name": _param_name(param, text),
                "optional": optional,
                "kind": kind,
            }
            declared = _dedent_annotation(_param_type(param, text), base_col)
            if declared:
                shape["type"] = declared
            params.append(shape)

    kind = "function"
    if class_ctx is not None:
        kind = "method"
        if "staticmethod" in decorators:
            kind = "staticmethod"
        elif "classmethod" in decorators:
            kind = "classmethod"

    sig: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "params": params,
        "is_default_export": False,
        # `async` is an anonymous keyword token, so it is invisible to a
        # named-children scan and has to be read off the raw child list.
        "is_async": bool(node.children) and node.children[0].type == "async",
        "enclosing_class": class_ctx[0] if class_ctx else None,
        "enclosing_class_path": class_ctx[1] if class_ctx else None,
        "base_class": class_ctx[2] if class_ctx else None,
        # Decorated definitions start at the `def`, not the first decorator:
        # libcst's position metadata reports the FunctionDef node itself.
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
    }
    # Always present, empty when undecorated -- libcst writes the key
    # unconditionally and an omitted key is a different document downstream.
    sig["decorators"] = decorators
    returns = node.child_by_field_name("return_type")
    if returns is not None:
        sig["return_type"] = _dedent_annotation(text(returns), base_col)
    return sig


def call_site(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Call row, following libcst's deliberately narrow classification.

    Only a bare Name or an Attribute on a single Name is recorded. A chained
    call (`a.b().c()`), a subscript receiver, or a `super().__init__` is dropped
    entirely because the callee can never be index-resolved -- which is why
    Python's call graph is sparser than Ruby's by design, not by omission.
    """
    if node.type == "type_alias_statement":
        # `type` is a soft keyword, so an assignment whose target OPENS with a
        # `type(...)` call -- `type(obj).attr = v`, the idiom for setting a
        # property on an instance's class -- is swallowed whole as a PEP 695
        # alias, and no call node for it is ever built. A real alias binds a
        # bare identifier, so a target starting with `(` is the misparse rather
        # than a declaration, and the call it dissolved is recoverable.
        left = node.child_by_field_name("left")
        if left is not None and text(left).startswith("("):
            return {
                "name": "type",
                "receiver": None,
                "kind": "bare",
                "line": node.start_point[0] + 1,
            }
        return None
    if node.type != "call":
        return None
    func = node.child_by_field_name("function")
    if func is None:
        return None
    line = node.start_point[0] + 1

    if func.type == "identifier":
        return {"name": text(func), "receiver": None, "kind": "bare", "line": line}
    if func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        recv = func.child_by_field_name("object")
        if attr is None or recv is None or recv.type != "identifier":
            return None
        receiver = text(recv)
        if receiver == "self":
            return {"name": text(attr), "receiver": "self", "kind": "self", "line": line}
        return {"name": text(attr), "receiver": receiver, "kind": "member", "line": line}
    return None


def import_specifier(node: Any, text: Callable[[Any], str]) -> list[tuple[str, str]] | None:
    """(module, kind) rows, keeping the full dotted path and relative dots.

    ``import x`` -> namespace; ``from m import a`` -> named; ``from m import *``
    -> namespace; ``from . import x`` -> (".", named). Downstream framework
    discrimination keys on the import root, so the dots and the full path both
    have to survive.
    """
    if node.type == "import_statement":
        out: list[tuple[str, str]] = []
        for alias in node.named_children:
            target = alias
            if alias.type == "aliased_import":
                inner = alias.child_by_field_name("name")
                if inner is None:
                    continue
                target = inner
            if target.type in ("dotted_name", "identifier"):
                out.append((text(target), "namespace"))
        return out or None

    if node.type == "future_import_statement":
        # The grammar gives this its own rule with NO module_name field, since
        # the module is implicit in the syntax. libcst reports it as an ordinary
        # ImportFrom on `__future__`.
        return [("__future__", "named")]

    if node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is None:
            return None
        target = text(module)
        if not target:
            return None
        star = any(c.type == "wildcard_import" for c in node.named_children)
        return [(target, "namespace" if star else "named")]

    return None


end_line_excludes_comments = True
