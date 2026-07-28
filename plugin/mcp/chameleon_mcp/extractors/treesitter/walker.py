"""The single iterative tree walk backing every extracted field.

This is the only module in the package that traverses a syntax tree, and that
containment is the design's main safety property rather than a tidiness
preference. Tree-sitter parses pathological input happily -- 50,000 levels of
nested parentheses parse in 0.1s and produce a tree 50,003 nodes deep -- and a
recursive walk over that tree raises RecursionError while the equivalent
iterative walk returns normally. The dump scripts could recurse safely because
a blown stack killed a subprocess; in-process it would take down the MCP
server. So: one walker, explicitly stacked, and language modules that hold data
instead of traversal code, which leaves no place for a second walk to grow.

One pass fills every field. Kind translation happens only at emission, so the
walk reasons in tree-sitter's own vocabulary and downstream keeps reading the
dump scripts' vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# Rules whose `left` field binds a name in the enclosing scope. Shared across
# languages because the field name is a tree-sitter convention, not a Ruby one.
_ASSIGNMENT_NODES = frozenset({"assignment", "operator_assignment"})

# Rules whose named children bind names into the surrounding scope. Excluding
# the binding SITE from call detection is not enough -- every later USE of the
# name has to know it is bound, or `rescue => e` still reports `e.message` as a
# receiverless send.
_PARAM_BINDING_NODES = frozenset({"block_parameters", "exception_variable", "lambda_parameters"})

# Parameter wrappers whose bound name sits on a `name` field rather than
# being the node itself; reading only bare identifiers missed all of them.
_PARAM_WRAPPERS = frozenset(
    {
        "keyword_parameter",
        "optional_parameter",
        "hash_splat_parameter",
        "splat_parameter",
        "block_parameter",
    }
)

# Modifier forms whose condition is traversed before their body.
# JSX presence is a TypeScript-only ParsedFile field; the set is empty for
# every other language so the check costs one frozenset miss.
_JSX_NODES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})

# Clause kinds a recursive AST nests but the grammar lists flat.
_CHAINED_NESTING_NODES = frozenset({"elif_clause", "rescue"})

# A block under one of these parents opens no nesting level: Prism carries a
# lambda literal's body inline rather than as its own frame.
_NESTING_EXCLUDED_UNDER = {("block", "lambda"), ("do_block", "lambda")}

_CONDITION_FIRST_NODES = frozenset(
    {"if_modifier", "unless_modifier", "while_modifier", "until_modifier"}
)


class NodeBudgetExceeded(Exception):
    """The walk hit the node cap and the file should be skipped.

    Mirrors the dump scripts' MAX_AST_NODES abort. A file this large is
    pathological rather than merely big -- MAX_FILE_SIZE is the real DoS bound
    -- so the file is skipped rather than half-reported, which would feed
    downstream a truncated shape it cannot distinguish from a genuine one.
    """


class LanguageTables(Protocol):
    """The per-language data the walker is driven by.

    Everything here is data or a small callable that inspects ONE node and its
    immediate fields. No member walks a subtree: that invariant is what keeps
    the recursion constraint enforceable in a single place, and it is the reason
    a new language costs a table module rather than a new traversal.
    """

    language: str

    TOP_LEVEL_KINDS: dict[str, str]
    """Tree-sitter node type -> the kind string the dump script emitted."""

    FUNCTION_NODES: frozenset[str]
    CLASS_NODES: frozenset[str]
    BRANCH_NODES: frozenset[str]
    NESTING_NODES: frozenset[str]
    SKIP_SUBTREE_NODES: frozenset[str]
    """Subtrees never descended into (comments, string bodies)."""

    call_site_carries_nesting: bool
    """Whether call rows carry a ``nesting`` list; Prism emits it, ts_dump does not."""

    def class_shape(self, node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
        """Shape row for a class/module node, or None to skip it."""
        ...

    def class_body_calls(
        self, node: Any, text: Callable[[Any], str], class_name: str
    ) -> list[dict[str, Any]]:
        """Receiverless DSL macros in a class body (Rails ``has_many``)."""
        ...

    def callable_signature(
        self,
        node: Any,
        text: Callable[[Any], str],
        class_ctx: tuple[str, str, str | None, tuple[str, ...]] | None,
    ) -> dict[str, Any] | None:
        """Signature row for a function-like node, or None to skip it."""
        ...

    def call_site(self, node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
        """Call row for a call node, without ``caller`` (the walker fills it)."""
        ...

    def import_specifier(
        self, node: Any, text: Callable[[Any], str]
    ) -> list[tuple[str, str]] | None:
        """(module, kind) rows contributed by an import node."""
        ...


@dataclass
class WalkResult:
    """Everything one pass produces, pre-translation."""

    top_level_node_kinds: list[str] = field(default_factory=list)
    top_level_nodes: list[str] = field(default_factory=list)
    """Raw tree-sitter rule name per top-level kind, positionally aligned.

    The export fields are counted from the statements themselves rather than
    from the translated kinds: Prism's named_export_count is a count of
    class/module/def, not the length of top_level_node_kinds, and the two
    diverge on any file with a top-level require.
    """
    function_scopes: list[dict[str, Any]] = field(default_factory=list)
    callable_signatures: list[dict[str, Any]] = field(default_factory=list)
    class_shapes: list[dict[str, Any]] = field(default_factory=list)
    class_body_calls: list[dict[str, Any]] = field(default_factory=list)
    class_property_types: list[dict[str, Any]] = field(default_factory=list)
    """Declared instance-property types, per class that has any.

    Collected here rather than from the root's children because a class nested
    inside a function -- the HOC/factory shape a React codebase is full of --
    is invisible at file level, and the dumper walks the whole tree.
    """
    call_sites: list[dict[str, Any]] = field(default_factory=list)
    import_specifiers: list[tuple[str, str]] = field(default_factory=list)
    import_symbols: list[dict[str, Any]] = field(default_factory=list)
    namespace_imports: list[dict[str, Any]] = field(default_factory=list)
    """Import BINDING rows, collected per node rather than from the root.

    A function-local `import x` and one inside `if TYPE_CHECKING:` bind names
    just as a top-level import does, and libcst's visitor records them
    wherever they appear. Scanning only the root's children missed every
    nested import in a real repo -- 4 distinct files in flaskbb alone.
    """
    node_count: int = 0
    max_depth: int = 0
    call_sites_truncated: bool = False
    call_sites_seen: int = 0
    """Every call site encountered, including those past the cap.

    The emitted list truncates, but the dumpers report the honest total
    alongside the truncation flag -- deriving the total from len(call_sites)
    silently reports the cap instead on any hub module that exceeds it.
    """
    has_jsx: bool = False


class _Locals:
    """Local-variable bindings for one lexical scope, chained to its enclosing one.

    A flat set per function was wrong for Ruby in both directions. A block's
    `|params|` and any name first assigned inside it are scoped to that block,
    so writing them into the function's set leaves them bound after the block
    closes and every later receiverless send by that name reads as a variable
    instead of a call -- the single largest source of missing call sites on a
    block-heavy repo. Meanwhile a block genuinely CLOSES OVER its enclosing
    scope, so an independent set per block would lose the outer bindings.

    Chaining gets both, and gets rebinding for free: `add` always writes to the
    innermost scope, so a name already visible further out stays bound after the
    block (it was a rebind), while a genuinely new one disappears with the scope
    that introduced it. A method opens a scope with NO parent, because a Ruby
    method is not a closure and cannot see the locals around it.
    """

    __slots__ = ("names", "parent")

    def __init__(self, parent: _Locals | None = None, seed: Any = None) -> None:
        self.names: set[str] = set(seed or ())
        self.parent = parent

    def add(self, name: str) -> None:
        self.names.add(name)

    def __contains__(self, name: object) -> bool:
        scope: _Locals | None = self
        while scope is not None:
            if name in scope.names:
                return True
            scope = scope.parent
        return False


@dataclass
class _Frame:
    """A function's accumulation state.

    Held by reference in ``function_scopes`` from the moment the function node
    is reached, so the metrics land in source order without a completion pass:
    a DFS that pushes children in reverse reaches function nodes in source
    order, and later mutation updates the entry already in the list.
    """

    name: str
    scope: dict[str, Any]
    class_name: str | None = None
    class_path: str | None = None
    base_class: str | None = None
    locals: set[str] = field(default_factory=set)
    """Names bound in this scope: parameters plus assignment targets seen so far.

    Ruby needs this and the other languages do not. Prism resolves local
    variables while parsing, so a bare `params` is a CallNode while a bare
    `orders` (assigned above) is a variable read; tree-sitter reports
    `identifier` for both and leaves the distinction to the consumer. Populated
    in source order as the walk proceeds, which is the same order the parser
    binds them in.
    """


def walk(
    root: Any,
    src: bytes,
    tables: LanguageTables,
    *,
    max_nodes: int,
    max_call_sites: int,
    max_callable_signatures: int,
) -> WalkResult:
    """Traverse ``root`` once, filling a WalkResult.

    Raises NodeBudgetExceeded past ``max_nodes``. Caps on call sites and
    signatures truncate in place instead of raising: those are honest partial
    results the dump scripts also produce, and the truncation flag travels with
    them.
    """
    result = WalkResult()

    def text(node: Any) -> str:
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    # Top-level kinds come off the root's direct children, before the walk, so
    # the ordering is the source's rather than the traversal's. A table may
    # refine the dict lookup where one grammar rule covers several dumper kinds
    # (Ruby's operator_assignment is IndexOrWriteNode or
    # LocalVariableOperatorWriteNode depending on its target).
    refine = getattr(tables, "refine_top_level_kind", None)
    for child in root.named_children:
        mapped = tables.TOP_LEVEL_KINDS.get(child.type)
        if mapped is None:
            continue
        if refine is not None:
            mapped = refine(child, text, mapped)
        result.top_level_node_kinds.append(mapped)
        result.top_level_nodes.append(child.type)

    # (node, depth_in_frame, frame, class_context). class_context is the
    # enclosing (name, qualified_path, base) so a method can name its class
    # without a second pass.
    module_scope = _Locals()
    stack: list[
        tuple[
            Any,
            int,
            _Frame | None,
            tuple[str, str, str | None, tuple[str, ...]] | None,
            _Locals,
        ]
    ] = [(root, 0, None, None, module_scope)]

    # Names bound outside any function. Class bodies and the file top level
    # share it, matching Prism's single module-level scope for `<module>`.
    while stack:
        node, depth, frame, class_ctx, locals_scope = stack.pop()

        result.node_count += 1
        if result.node_count > max_nodes:
            raise NodeBudgetExceeded(f"file exceeds {max_nodes} AST nodes at depth {depth}")
        if depth > result.max_depth:
            result.max_depth = depth

        node_type = node.type

        if node_type in tables.SKIP_SUBTREE_NODES:
            continue

        if node_type in _JSX_NODES and node_type in getattr(tables, "JSX_NODES", frozenset()):
            result.has_jsx = True

        child_depth = depth
        child_frame = frame
        child_class_ctx = class_ctx
        child_locals = locals_scope
        own_decorators: list[Any] = []

        # ts_dump pushes a NULL class-name sentinel for an object literal (and an
        # anonymous class), so a member or `this_prop` call inside one carries no
        # enclosing class rather than inheriting the lexically-enclosing one. The
        # named-class branch below re-establishes real context.
        if node_type in getattr(tables, "CLASS_CONTEXT_RESET_NODES", frozenset()):
            child_class_ctx = None

        if node_type == "singleton_class" and class_ctx is not None:
            # `class << self` opens no new class: its defs are SINGLETON methods
            # of the enclosing one, so the context is inherited with the flag
            # flipped rather than replaced. class_shape returns None here (there
            # is no `name` field), which would otherwise leave the flag unset.
            child_class_ctx = (*class_ctx[:4], True)

        factory = getattr(tables, "dynamic_class_frame", None)
        if factory is not None:
            factory_name = factory(node, text)
            if factory_name is not None:
                # A dynamic class factory names a class without a class node, so
                # the frame is opened from the assignment rather than a shape.
                sep = getattr(tables, "qualified_name_separator", "::")
                qualified_factory = (
                    factory_name if class_ctx is None else f"{class_ctx[1]}{sep}{factory_name}"
                )
                child_class_ctx = (
                    factory_name,
                    qualified_factory,
                    None,
                    (class_ctx[3] if class_ctx else ()) + (factory_name,),
                    False,
                )

        if node_type in tables.CLASS_NODES:
            shape = tables.class_shape(node, text)
            if shape is not None:
                qualified = shape["name"]
                if class_ctx is not None:
                    sep = getattr(tables, "qualified_name_separator", "::")
                    qualified = f"{class_ctx[1]}{sep}{shape['name']}"
                    # Only a NESTED definition carries `qualified`; a top-level
                    # one would just repeat `name`, and the dumpers omit it there.
                    if getattr(tables, "class_shape_carries_qualified", True):
                        shape["qualified"] = qualified
                if len(result.class_shapes) < max_callable_signatures:
                    result.class_shapes.append(shape)
                nesting = (class_ctx[3] if class_ctx else ()) + (shape["name"],)
                # A table may distinguish the heritage DISPLAY string from the
                # raw base a method reports; only the raw one enters the context.
                context_base = shape.pop("_context_base", None)
                if context_base is None:
                    context_base = shape.get("extends")
                # `class << self` makes every def inside it a singleton method,
                # a distinction the def's own shape does not carry.
                child_class_ctx = (
                    shape["name"],
                    qualified,
                    context_base,
                    nesting,
                    node.type == "singleton_class",
                )
                for macro in tables.class_body_calls(node, text, shape["name"]):
                    result.class_body_calls.append(macro)
                prop_types = getattr(tables, "class_property_types", None)
                if prop_types is not None:
                    props = prop_types(node, text)
                    if props:
                        result.class_property_types.append({"class": shape["name"], "props": props})

        elif node_type in tables.FUNCTION_NODES:
            sig = tables.callable_signature(node, text, class_ctx)
            if sig is not None and getattr(tables, "end_line_excludes_comments", False):
                sig["end_line"] = _last_statement_line(node, sig["end_line"])
            if sig is not None:
                # An anonymous function still opens a body-shape frame and owns
                # the calls inside it, but has no contract worth recording --
                # ts_dump attributes those calls to a `<anonymous>` caller.
                anonymous = sig.pop("_anonymous", False)
                if not anonymous and len(result.callable_signatures) < max_callable_signatures:
                    result.callable_signatures.append(sig)
                # param_count is NOT len(params). Ruby counts a block parameter
                # toward the count while omitting it from the shape list, so a
                # table that knows its language supplies the number and the
                # length is only a fallback.
                param_count = sig.pop("_param_count", None)
                if param_count is None:
                    param_count = len(sig.get("params") or [])
                scope = {
                    "start_line": sig["start_line"],
                    "end_line": sig["end_line"],
                    "line_span": sig["end_line"] - sig["start_line"] + 1,
                    "max_depth": 0,
                    "branch_count": 0,
                    "param_count": param_count,
                }
                # Sorted into COMPLETION order once the walk finishes: the
                # dumpers emit a frame when it closes, so a nested arrow is
                # reported before the function containing it.
                scope["_end_byte"] = node.end_byte
                scope["_start_byte"] = node.start_byte
                sig["_end_byte"] = node.end_byte
                result.function_scopes.append(scope)
                child_frame = _Frame(
                    name=sig["name"],
                    scope=scope,
                    class_name=class_ctx[0] if class_ctx else None,
                    class_path=class_ctx[1] if class_ctx else None,
                    base_class=class_ctx[2] if class_ctx else None,
                    locals=set(
                        sig.pop("_local_names", None)
                        or [p["name"] for p in (sig.get("params") or [])]
                    ),
                )
                # A method is not a closure: it cannot see the locals around it,
                # so its scope has no parent and the seeded bindings are all it
                # starts with.
                child_locals = _Locals(seed=child_frame.locals)
                if node_type in getattr(tables, "CLASS_CONTEXT_OPAQUE_NODES", frozenset()):
                    # A plain function declared inside a method is not a member of
                    # that class; ts_dump's classStack does not follow it in.
                    child_class_ctx = None
                extra = getattr(tables, "implicit_nesting_depth", None)
                if extra is not None:
                    # A `def ... rescue ... end` is a BeginNode wrapping a
                    # RescueNode in Prism (two levels) but a single `rescue`
                    # child of the body here, so the missing level is added up
                    # front rather than inferred at the rescue.
                    scope["max_depth"] = extra(node)
                # A function body's depth is measured from the function, not
                # from the file, so each frame restarts the count -- at the
                # implicit baseline, not necessarily zero, so a nesting node
                # inside a `def ... rescue` body lands one level deeper.
                child_depth = scope["max_depth"]
                # A decorator is a CHILD of what it decorates in the reference
                # AST but a preceding SIBLING in this grammar, so `@Query()` on
                # a resolver method was attributed to `<module>` instead of the
                # method. Walk them here, inside the frame that owns them; the
                # class_body push below skips them so they are reached once.
                sib = node.prev_named_sibling
                while sib is not None and sib.type in ("decorator", "comment"):
                    if sib.type == "decorator":
                        own_decorators.append(sib)
                    sib = sib.prev_named_sibling

        else:
            if node_type in getattr(tables, "BLOCK_SCOPE_NODES", frozenset()):
                # A block closes over its enclosing scope but binds its own
                # `|params|` and first assignments locally, so both must end
                # when the block does.
                child_locals = _Locals(parent=locals_scope)
            if frame is not None:
                if node_type in tables.BRANCH_NODES:
                    frame.scope["branch_count"] += 1
                if node_type == "else_clause":
                    # `else` is the orelse of the innermost elif-If, so its body
                    # sits at that elif's depth -- N preceding elifs, no +1.
                    child_depth = depth + _preceding_chain_length(node, "elif_clause")
                elif node_type in tables.NESTING_NODES and not _excluded_nesting(node):
                    child_depth = depth + 1
                    if node_type in getattr(tables, "BLOCK_SCOPE_NODES", frozenset()):
                        # A block whose body carries an inline rescue is a Block
                        # wrapping an implicit Begin in Prism -- the same missing
                        # level a `def ... rescue` body has, counted the same way.
                        extra = getattr(tables, "implicit_nesting_depth", None)
                        if extra is not None:
                            child_depth += extra(node)
                    if node_type in _CHAINED_NESTING_NODES:
                        # libcst models `elif` as an If nested in the previous
                        # If's orelse, so an if/elif/elif chain is three levels
                        # deep there while the grammar keeps the clauses flat.
                        child_depth += _preceding_chain_length(node, node.type)
                    if child_depth > frame.scope["max_depth"]:
                        frame.scope["max_depth"] = child_depth
            elif node_type in tables.NESTING_NODES:
                child_depth = depth + 1

            # Bind assignment targets before anything below reads the scope, so
            # a name is a local only for uses that come AFTER its assignment --
            # the same order the parser binds them in.
            scope_locals = locals_scope
            if node_type in _ASSIGNMENT_NODES:
                target = node.child_by_field_name("left")
                if target is not None and target.type == "identifier":
                    scope_locals.add(text(target))
                elif target is not None and target.type == "left_assignment_list":
                    # `a, b = ...` binds every plain target; Prism's MultiWriteNode
                    # makes each a target node, so a later read is a variable read.
                    for tgt in target.named_children:
                        if tgt.type == "identifier":
                            scope_locals.add(text(tgt))
                        elif tgt.type == "rest_assignment":
                            # `_, *rest = ...` binds `rest` through a wrapper
                            # node; Prism's MultiWriteNode binds every target.
                            for inner in tgt.named_children:
                                if inner.type == "identifier":
                                    scope_locals.add(text(inner))
            elif node_type in _PARAM_BINDING_NODES:
                # A block's parameters bind into the ENCLOSING scope for our
                # purposes: `orders.map { |o| o.id }` makes `o` a variable, so
                # the `o` receiver must not read as a receiverless send.
                for param in node.named_children:
                    if param.type == "identifier":
                        scope_locals.add(text(param))
                    elif param.type == "destructured_parameter":
                        # `|(source, target), date|` binds source AND target,
                        # and `|((a, b), c), d|` binds all four -- destructuring
                        # nests, so binding one level deep leaves the inner
                        # names reading as receiverless sends.
                        pending = list(param.named_children)
                        while pending:
                            inner = pending.pop()
                            if inner.type == "identifier":
                                scope_locals.add(text(inner))
                            elif inner.type == "destructured_parameter":
                                pending.extend(inner.named_children)
                    elif param.type in _PARAM_WRAPPERS:
                        bound = param.child_by_field_name("name")
                        if bound is not None:
                            scope_locals.add(text(bound))

            call = tables.call_site(node, text)
            if call is None:
                bare = getattr(tables, "bare_call_from_identifier", None)
                if bare is not None:
                    # Also applies at module scope: a class-body macro like
                    # `primary_abstract_class` is a bare send with caller
                    # `<module>`, not a method-local one.
                    call = bare(node, text, scope_locals)
            if call is not None:
                result.call_sites_seen += 1
                if len(result.call_sites) < max_call_sites:
                    call["caller"] = frame.name if frame is not None else "<module>"
                    # Only a CONSTANT receiver carries lexical nesting: the list
                    # exists so a constant can be resolved outward from its call
                    # site, which is meaningless for a bare or member send.
                    if (
                        tables.call_site_carries_nesting
                        and class_ctx is not None
                        and call.get("kind") == "constant"
                    ):
                        # The FULL lexical stack, outermost first -- Prism
                        # resolves a constant by walking outward, so a
                        # `module Orders / class CancelOrder` yields both.
                        call["nesting"] = list(class_ctx[3])
                    if call.get("kind") == "this_prop":
                        # Scoped to the CALLING class: a sibling class may
                        # declare the same property with a different type.
                        call["enclosing_class"] = class_ctx[0] if class_ctx else None
                    result.call_sites.append(call)
                else:
                    result.call_sites_truncated = True

            spec = tables.import_specifier(node, text)
            if spec is not None:
                result.import_specifiers.extend(spec)

            binder = getattr(tables, "import_bindings", None)
            if binder is not None:
                symbols, namespaces = binder(node, text)
                result.import_symbols.extend(symbols)
                result.namespace_imports.extend(namespaces)

        # NAMED children only. Anonymous tokens are the grammar's keywords and
        # punctuation, and an `if_modifier` node contains a literal `if` token
        # whose type collides with the `if` rule -- walking it scored every
        # modifier-if twice, doubling branch_count and max_depth. Nothing below
        # reads an anonymous child (field lookups resolve them directly), so
        # skipping them is both correct and cheaper.
        #
        # Reversed so the pop order matches source order, which is what keeps
        # function_scopes and call_sites in the sequence the dump scripts emit.
        if node_type in _CONDITION_FIRST_NODES:
            # A modifier form (`return x unless y`) is an IfNode to Prism, whose
            # traversal reads the predicate BEFORE the body -- the opposite of
            # source order. Rows are otherwise identical, so getting this wrong
            # produces a correct set in the wrong sequence, which still fails an
            # order-sensitive comparison downstream.
            condition = node.child_by_field_name("condition")
            body = node.child_by_field_name("body")
            if condition is not None and body is not None:
                stack.append((body, child_depth, child_frame, child_class_ctx, child_locals))
                stack.append((condition, child_depth, child_frame, child_class_ctx, child_locals))
                continue

        for child in reversed(node.named_children):
            if child.type == "decorator" and node_type == "class_body":
                # Claimed by the member it decorates, below. Only MEMBER
                # decorators are re-homed: a class's own decorators sit under
                # `export_statement` and belong to the enclosing scope exactly
                # where they are, so skipping those dropped every `@Injectable`
                # and `@Entity` call outright.
                continue
            stack.append((child, child_depth, child_frame, child_class_ctx, child_locals))

        # Pushed AFTER the body so they pop BEFORE it: a decorator precedes its
        # member in source, and call rows are compared positionally. The list is
        # already nearest-first, which pushes to source order on a LIFO stack.
        for dec in own_decorators:
            stack.append((dec, child_depth, child_frame, child_class_ctx, child_locals))

    # Only the SCOPES are completion-ordered. The dumpers record a signature on
    # entering a function but a body-shape frame on leaving it, so the two lists
    # genuinely disagree about sequence for a nested function: signatures stay in
    # source order (the DFS already produces that) while scopes invert.
    _order_by_completion(result.function_scopes)
    for sig in result.callable_signatures:
        sig.pop("_end_byte", None)
    return result


def _excluded_nesting(node: Any) -> bool:
    """True when a node opens no nesting level because of what encloses it."""
    parent = node.parent
    return parent is not None and (node.type, parent.type) in _NESTING_EXCLUDED_UNDER


def _preceding_chain_length(node: Any, kind: str) -> int:
    """How many sibling clauses of the same kind precede ``node``.

    Used to restore the nesting an `elif` chain has in a recursive AST but not
    in the grammar's flat clause list.
    """
    parent = node.parent
    if parent is None:
        return 0
    seen = 0
    for child in parent.named_children:
        if child.id == node.id:
            return seen
        if child.type == kind:
            seen += 1
    return seen


def error_nodes(root: Any, max_nodes: int) -> list[Any]:
    """Every ERROR or MISSING node under ``root``, found iteratively.

    Lives here because this module owns traversal: the recursion constraint is
    only enforceable if there is nowhere else for a walk to grow. Pruned by
    `has_error`, so a clean subtree costs one check rather than a descent, and
    bounded by the same node budget the main walk uses.
    """
    found: list[Any] = []
    stack = [root]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > max_nodes:
            break
        if node.type == "ERROR" or node.is_missing:
            found.append(node)
            # Whatever the recovery hung underneath is noise, not a second
            # independent defect.
            continue
        if not node.has_error:
            continue
        # Anonymous children included: a MISSING node is usually a punctuation
        # token, which never appears among named children.
        stack.extend(node.children)
    return found


def _last_statement_line(node: Any, fallback: int) -> int:
    """The function's last non-comment line.

    tree-sitter folds a trailing comment into whatever block it trails, at
    EVERY nesting level, so a function whose last two lines are commented-out
    code reports an end 2 lines past where libcst's FunctionDef ends. Skipping
    comments only among the body's own children is not enough: when the body
    ends in a compound statement, a comment trailing THAT statement's inner
    block still stretches the enclosing statement's end, and the walk back
    lands on the inflated value.

    So follow the subtree's right spine instead, over named and anonymous
    children alike, skipping only comments. Anonymous is the load-bearing
    half -- libcst ends a node at its last real TOKEN, and a closing `)` is a
    token, so a named-only descent would stop short at the final keyword
    argument and report an end one line early on any multi-line call.
    """
    body = node.child_by_field_name("body")
    if body is None:
        # A decorated definition wraps the real one; the body hangs off the
        # inner node, so the wrapper alone yields nothing to walk back over.
        inner = node.child_by_field_name("definition")
        if inner is not None:
            body = inner.child_by_field_name("body")
    if body is None:
        return fallback
    current = body
    while True:
        last = None
        for child in current.children:
            if child.type != "comment":
                last = child
        if last is None:
            return current.end_point[0] + 1
        if not last.children:
            return last.end_point[0] + 1
        current = last


def _order_by_completion(rows: list[dict[str, Any]]) -> None:
    """Sort frame rows into the order their frames CLOSED, then drop the key.

    A recursive dumper appends a frame on the way OUT, so a nested arrow is
    reported before the function containing it. An iterative walk reaches them
    in the opposite order, and the difference shows up in nothing except the
    sequence -- every field of every row already matches.
    """
    rows.sort(key=lambda r: (r.get("_end_byte", 0), -r.get("_start_byte", 0)))
    for row in rows:
        row.pop("_end_byte", None)
        row.pop("_start_byte", None)
