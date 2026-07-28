"""Ruby kind tables, matching what prism_dump.rb emits.

Every mapping here is a fidelity contract with the Prism dumper, not a fresh
modelling decision: downstream cluster signatures, ast_query witnesses, and
kind labels are all keyed on Prism's vocabulary, so a table that "improves" a
name silently re-clusters every committed Ruby profile. Where a choice looks
arbitrary it is copied deliberately, and the Prism rule it mirrors is named.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

language = "ruby"

# Prism reports a node's class name; tree-sitter reports a grammar rule. Only
# kinds Prism actually surfaces at top level are mapped -- an unmapped node
# contributes nothing, which is what keeps named_export_count honest (an early
# probe leaked raw rule names like "if" into the count by falling back to
# node.type).
TOP_LEVEL_KINDS: dict[str, str] = {
    "class": "ClassNode",
    "module": "ModuleNode",
    "method": "DefNode",
    "singleton_method": "DefNode",
    "call": "CallNode",
    "assignment": "LocalVariableWriteNode",
    "operator_assignment": "LocalVariableOperatorWriteNode",
    "constant": "ConstantReadNode",
    # A receiverless zero-argument macro written as a bare word
    # (`nakayoshi_fork`) has nothing to parse but an identifier; Prism
    # resolves the unbound name to a send.
    "identifier": "CallNode",
    "if": "IfNode",
    "unless": "UnlessNode",
    "while": "WhileNode",
    "until": "UntilNode",
    "case": "CaseNode",
    "begin": "BeginNode",
    "if_modifier": "IfNode",
    "unless_modifier": "UnlessNode",
    "while_modifier": "WhileNode",
    "until_modifier": "UntilNode",
    # Prism models an operator send as a CallNode; `&&`/`||` are their own
    # node types, split out in refine_top_level_kind.
    "binary": "CallNode",
    "comment": "",
}

FUNCTION_NODES = frozenset({"method", "singleton_method"})
CLASS_NODES = frozenset({"class", "module", "singleton_class"})

# Prism's is_branch_node?: the cyclomatic decision set minus boolean operators.
# Prism folds modifier forms (`x if y`) and `elsif` into IfNode, so both map
# onto the same decision point here. `when`/`in_clause` count individually so a
# long flat case reads as branchy rather than deep.
BRANCH_NODES = frozenset(
    {
        "if",
        "elsif",
        "if_modifier",
        "unless",
        "unless_modifier",
        "while",
        "while_modifier",
        "until",
        "until_modifier",
        "for",
        "when",
        "in_clause",
        "rescue",
        "conditional",
    }
)

# Prism's is_nesting_node?: branch nodes that also open a structural indent
# level. `when`/`in_clause` are deliberately absent -- they add a decision point
# but sit at the case's own indent. Blocks raise depth because deeply chained
# iteration inside a method is the nesting reviewers flag.
NESTING_NODES = frozenset(
    {
        "if",
        "elsif",
        "if_modifier",
        "unless",
        "unless_modifier",
        "while",
        "while_modifier",
        "until",
        "until_modifier",
        "for",
        "case",
        "case_match",
        "begin",
        "block",
        "do_block",
        "rescue",
        # A ternary is a Prism IfNode, so it opens an indent level as well as
        # scoring a decision point. Counting it only as a branch reports
        # `x.negative? ? "-" : ""` at depth 0 where Prism reports depth 1.
        "conditional",
    }
)

# Comments carry no structure, and descending string bodies would let
# interpolation produce call rows for text the program never executes.
# `heredoc_body` is NOT skipped: Prism descends its interpolations, so calls
# written inside `#{...}` in a heredoc are real call sites.
SKIP_SUBTREE_NODES = frozenset({"comment", "string_content", "heredoc_content"})

# Rules that open a local-variable scope of their own. Prism resolves locals
# while parsing, so it knows a block's `|params|` and any name first assigned
# inside the block stop existing at `end`; a name reused afterwards is a
# receiverless send again. `lambda` is absent deliberately -- its body is one of
# these two node types, so listing it would open a redundant second scope.
BLOCK_SCOPE_NODES = frozenset({"block", "do_block"})

# Prism attaches a lexical `nesting` list to constant-receiver call rows so a
# constant can be resolved outward from its call site.
call_site_carries_nesting = True

# tree-sitter parameter rule -> (prism kind, droppable). Prism reads
# optional/splat/keyword-with-default all as droppable.
_PARAM_KINDS: dict[str, tuple[str, bool]] = {
    "identifier": ("positional", False),
    "optional_parameter": ("optional", True),
    "splat_parameter": ("rest", True),
    "keyword_parameter": ("keyword", False),
    "hash_splat_parameter": ("keyword_rest", True),
    # Argument forwarding, `def m(...)`: Prism's ForwardingParameterNode
    # surfaces as a keyword rest, so it shares that shape rather than
    # falling through to the positional default.
    "forward_parameter": ("keyword_rest", True),
}


def _constant_text(node: Any, text: Callable[[Any], str]) -> str:
    """Flatten a constant or scope_resolution (``Api::V1::Foo``) to its source."""
    return text(node).strip()


_WRITE_PREFIX = {
    "element_reference": "Index",
    "call": "Call",
    "constant": "Constant",
    "scope_resolution": "ConstantPath",
    "instance_variable": "InstanceVariable",
    "class_variable": "ClassVariable",
    "global_variable": "GlobalVariable",
    "identifier": "LocalVariable",
}

# `||=` and `&&=` are distinct Prism writes, not generic operator writes.
_OPERATOR_WRITE_SUFFIX = {"||=": "OrWriteNode", "&&=": "AndWriteNode"}

_BOOLEAN_BINARY = {"&&": "AndNode", "and": "AndNode", "||": "OrNode", "or": "OrNode"}


# Receiver/method pairs whose block form defines a CLASS rather than iterating:
# `AccountRow = Data.define(:a) do ... end` names a class, so Prism pushes a
# nesting frame for the assigned constant and the defs inside belong to it.
_DYNAMIC_CLASS_FACTORIES = {"Data": "define", "Struct": "new", "Class": "new"}


def dynamic_class_frame(node: Any, text: Callable[[Any], str]) -> str | None:
    """The constant name a dynamic-class-factory assignment opens, or None."""
    if node.type != "assignment":
        return None
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or left.type not in ("constant", "scope_resolution"):
        return None
    if right is None or right.type != "call":
        return None
    receiver = right.child_by_field_name("receiver")
    method = right.child_by_field_name("method")
    block = right.child_by_field_name("block")
    if receiver is None or receiver.type != "constant" or method is None or block is None:
        return None
    if _DYNAMIC_CLASS_FACTORIES.get(text(receiver)) != text(method):
        return None
    return _constant_text(left, text)


def refine_top_level_kind(node: Any, text: Callable[[Any], str], mapped: str) -> str:
    """Split one grammar rule across the several kinds Prism reports.

    `operator_assignment` covers both `x ||= 1` (LocalVariableOperatorWriteNode)
    and `ENV["K"] ||= "v"` (IndexOrWriteNode); Prism names them by target, the
    grammar does not. The distinction reaches the cluster signature through
    top_level_node_kinds, so collapsing them re-buckets any file whose first
    statement is an ENV default -- rails_helper.rb, in practice.
    """
    if node.type == "binary":
        operator = node.child_by_field_name("operator")
        if operator is not None:
            return _BOOLEAN_BINARY.get(text(operator), "CallNode")
        return mapped

    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is None:
            return mapped
        # `x.y = z` and `x[y] = z` are SETTER SENDS to Prism, not writes.
        if left.type in ("call", "element_reference"):
            return "CallNode"
        if left.type == "left_assignment_list":
            return "MultiWriteNode"
        prefix = _WRITE_PREFIX.get(left.type)
        return f"{prefix}WriteNode" if prefix else mapped

    if node.type != "operator_assignment":
        return mapped
    left = node.child_by_field_name("left")
    if left is None:
        return mapped
    prefix = _WRITE_PREFIX.get(left.type)
    if prefix is None:
        return mapped
    operator = node.child_by_field_name("operator")
    suffix = _OPERATOR_WRITE_SUFFIX.get(text(operator) if operator else "", "OperatorWriteNode")
    return f"{prefix}{suffix}"


# Prism's is_top_level_export?: class, module, or def. Deliberately NOT the
# length of top_level_node_kinds -- a file with a top-level `require` has more
# statements than exports, and application.rb (require + module) is exactly that
# case.
_EXPORT_KINDS = frozenset({"ClassNode", "ModuleNode", "DefNode"})
_CLASS_OR_MODULE_KINDS = frozenset({"ClassNode", "ModuleNode"})


def export_fields(top_level_kinds: list[str]) -> tuple[str | None, int]:
    """Return (default_export_kind, named_export_count) from top-level kinds.

    Ruby has no export statement, so Prism repurposes both fields: the default
    is the SOLE top-level class or module (nil when there are zero or several),
    and the count is of definitions rather than statements.
    """
    classes = [k for k in top_level_kinds if k in _CLASS_OR_MODULE_KINDS]
    default = classes[0] if len(classes) == 1 else None
    return default, sum(1 for k in top_level_kinds if k in _EXPORT_KINDS)


def _params(node: Any, text: Callable[[Any], str]) -> tuple[list[dict[str, Any]], int]:
    """Return (shape rows, param_count).

    The two numbers genuinely differ: Prism's param_count_of counts a block
    parameter while param_shapes never emits one, so `def each(&blk)` is one
    parameter with zero shapes. Returning both keeps that asymmetry explicit
    rather than letting a caller infer the count from the list length.
    """
    plist = node.child_by_field_name("parameters")
    if plist is None:
        return [], 0

    shapes: list[dict[str, Any]] = []
    count = 0
    for param in plist.named_children:
        if param.type == "comment":
            continue
        if param.type == "block_parameter":
            count += 1
            continue

        entry = _PARAM_KINDS.get(param.type)
        if entry is None:
            # destructured_parameter and anything the grammar adds later: Prism
            # counts it as a required positional binding.
            shapes.append({"name": text(param), "optional": False, "kind": "positional"})
            count += 1
            continue

        kind, droppable = entry
        if kind == "keyword_rest":
            shapes.append({"name": "**", "optional": True, "kind": "keyword_rest"})
            count += 1
            continue

        name_node = param if param.type == "identifier" else param.child_by_field_name("name")
        name = text(name_node) if name_node is not None else "_"
        if kind == "keyword":
            # A keyword parameter is droppable only when it carries a default;
            # `kw:` is required, `kw: 2` is not.
            droppable = param.child_by_field_name("value") is not None
            name = name.rstrip(":")
        elif kind == "rest" and name_node is None:
            name = "*"

        shapes.append({"name": name, "optional": droppable, "kind": kind})
        count += 1

    return shapes, count


def class_shape(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Shape row for a class/module node.

    ``extends`` is always present, null for modules and for classes with no
    superclass. Prism writes the key unconditionally, and an omitted key is a
    different JSON document downstream.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _constant_text(name_node, text)
    if not name:
        return None

    if node.type == "module":
        return {
            "name": name,
            "kind": "module",
            "start_line": node.start_point[0] + 1,
            "extends": None,
        }

    extends = None
    super_node = node.child_by_field_name("superclass")
    if super_node is not None:
        # The `superclass` rule wraps the `<` token with the constant, so the
        # operator is stripped rather than carried into the name.
        for child in super_node.named_children:
            # Prism's constant_name is nil for a non-constant superclass
            # expression (`Struct.new(...)`, `BASES[0]`).
            if child.type in ("constant", "scope_resolution"):
                extends = _constant_text(child, text)
            break

    return {
        "name": name,
        "kind": "class",
        "start_line": node.start_point[0] + 1,
        "extends": extends,
    }


def class_body_calls(
    node: Any, text: Callable[[Any], str], class_name: str
) -> list[dict[str, Any]]:
    """Receiverless DSL macros declared directly in a class body.

    This is the Ruby-exclusive signal that makes a Rails model readable as a
    model: `belongs_to`, `has_many`, `validates`. Only direct body children
    count, and only lowercase-initial names, matching Prism's ``/\\A[a-z_]/``
    guard -- an uppercase bare call is a constant reference, not a macro.
    """
    # ClassNode only: Prism records these macros for classes, never for a
    # module body or a `class << self` singleton.
    if node.type != "class":
        return []
    body = node.child_by_field_name("body")
    if body is None:
        return []

    macros: list[dict[str, Any]] = []
    for child in body.children:
        # A macro with arguments parses as `call`; a bare one parses as a plain
        # `identifier`, because the grammar cannot tell `primary_abstract_class`
        # from a variable read without resolution. Prism sees a CallNode either
        # way, so both shapes have to be accepted or every no-argument macro
        # goes missing.
        if child.type == "identifier":
            name = text(child)
        elif child.type == "call" and child.child_by_field_name("receiver") is None:
            method = child.child_by_field_name("method")
            if method is None:
                continue
            name = text(method)
        else:
            continue
        if name[:1].islower() or name.startswith("_"):
            macros.append({"name": name, "class": class_name})
    return macros


def callable_signature(
    node: Any,
    text: Callable[[Any], str],
    class_ctx: tuple[str, str, str | None] | None,
) -> dict[str, Any] | None:
    """Signature row for a method or singleton method."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    shapes, count = _params(node, text)
    local_names: list[str] = []
    plist = node.child_by_field_name("parameters")
    if plist is not None:
        for param in plist.named_children:
            bound = param if param.type == "identifier" else param.child_by_field_name("name")
            if bound is not None and bound.type == "identifier":
                local_names.append(text(bound))

    sig: dict[str, Any] = {
        "name": text(name_node),
        "kind": _callable_kind(node, class_ctx),
        "params": shapes,
        "is_default_export": False,
        "enclosing_class": class_ctx[0] if class_ctx else None,
        "enclosing_class_path": class_ctx[1] if class_ctx else None,
        "base_class": class_ctx[2] if class_ctx else None,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "_param_count": count,
        # The REAL bindings, not the shape names: a keyword-rest
        # reports "**" as its shape but binds `kwargs`, and a block
        # param has no shape row at all. Uses of either must not
        # read as receiverless sends.
        "_local_names": local_names,
    }
    return sig


def _callable_kind(node: Any, class_ctx: Any) -> str:
    """`method` or `singleton_method`.

    Prism marks a def inside `class << self` as a singleton method: the receiver
    is the singleton class, not an instance. The grammar keeps the def shape
    identical, so the distinction comes from the enclosing context.
    """
    if node.type == "singleton_method":
        return "singleton_method"
    if class_ctx is not None and len(class_ctx) > 4 and class_ctx[4]:
        return "singleton_method"
    return "method"


def call_site(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Call row for a call node, minus the caller the walker fills in.

    Receiver naming follows Prism's ``recv.name``, which for a chained call is
    the receiver's METHOD name rather than its source text: in
    ``Order.recent.limit(5)`` the row for ``limit`` carries receiver ``recent``,
    not ``Order.recent``. Reading the whole receiver span instead is the single
    largest fidelity error available here -- it broke 96% of call rows on the
    first probe.
    """
    if node.type != "call":
        return None
    method = node.child_by_field_name("method")
    if method is None:
        return None

    # Prism models these as SuperNode / ForwardingSuperNode / YieldNode, so
    # no call row exists for them however they are written.
    if method.type in ("super", "yield"):
        return None

    name = text(method)
    # `config.foo = true` calls the SETTER `foo=`. The grammar models it as an
    # assignment whose left is a plain `config.foo` call, dropping the `=` that
    # is part of the method's actual name.
    parent = node.parent
    # `recv.foo += x` / `recv.foo ||= x` are Call{Operator,Or,And}WriteNodes to
    # Prism -- NOT CallNodes -- so the setter target contributes no row at all,
    # unlike the plain `recv.foo = x` send handled just below.
    if (
        parent is not None
        and parent.type == "operator_assignment"
        and (left := parent.child_by_field_name("left")) is not None
        and left.id == node.id
    ):
        return None
    if (
        parent is not None
        and parent.type == "assignment"
        and (left := parent.child_by_field_name("left")) is not None
        and left.id == node.id
    ):
        name = f"{name}="

    # Prism records identifier-named sends ONLY. Operator sends (+, <<, [], !)
    # can never be index-resolved and would crowd real calls out of the per-file
    # cap, so `params[:id]` contributes no row at all -- element_reference is
    # deliberately not handled here.
    if not name[:1].isalpha() and not name.startswith("_"):
        return None

    line = node.start_point[0] + 1
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return {"name": name, "receiver": None, "kind": "bare", "line": line}
    if recv.type == "self":
        return {"name": name, "receiver": "self", "kind": "self", "line": line}
    if recv.type == "constant" or (
        recv.type == "scope_resolution" and _is_static_constant_path(recv)
    ):
        return {
            "name": name,
            "receiver": _constant_text(recv, text),
            "kind": "constant",
            "line": line,
        }

    receiver_name = _receiver_name(recv, text)
    if receiver_name is None:
        # Prism falls through to nil when the receiver has no `.name` -- a
        # parenthesized expression, a literal, an array. `(abs % 100).to_s`
        # produces NO row for to_s, only one for the rjust chained off it.
        return None
    return {"name": name, "receiver": receiver_name, "kind": "member", "line": line}


def _is_static_constant_path(node: Any) -> bool:
    """True when a `::` path is rooted in constants all the way down.

    `Foo::Bar` resolves statically; `foo.bar::Baz` does not, and Prism
    yields a constant name only for the former.
    """
    scope = node.child_by_field_name("scope")
    if scope is None:
        return True  # `::Foo`, rooted at Object
    if scope.type == "constant":
        return True
    if scope.type == "scope_resolution":
        return _is_static_constant_path(scope)
    return False


def _receiver_name(recv: Any, text: Callable[[Any], str]) -> str | None:
    """The receiver's Prism ``.name``, or None when the node type has none.

    A chained call reports its METHOD name (`Order.recent.limit` -> `recent`),
    which is why reading the receiver's whole source span is wrong: it was the
    single largest fidelity error in the first probe, breaking 96% of call rows.
    """
    if recv.type == "call":
        inner = recv.child_by_field_name("method")
        # SuperNode and YieldNode carry no name, so a send chained off one has
        # no resolvable receiver -- the same reason a bare super is dropped
        # when it IS the call.
        if inner is None or inner.type in ("super", "yield"):
            return None
        return text(inner)
    if recv.type == "element_reference":
        return "[]"
    if recv.type == "scope_resolution":
        # A constant path receiver still has a Prism `.name` (its last segment),
        # so the send is recorded rather than dropped.
        last = recv.child_by_field_name("name")
        return text(last) if last is not None else None
    if recv.type == "global_variable":
        # `$1` and friends are NumberedReferenceReadNode to Prism, which carries
        # no name, so a send off one produces no row. A named global like
        # `$stdout` does have a name and still counts.
        name = text(recv)
        return None if name[1:].isdigit() else name
    if recv.type in ("identifier", "instance_variable", "class_variable"):
        return text(recv)
    return None


def implicit_nesting_depth(node: Any) -> int:
    """Depth a method body starts at, before its own nesting is counted.

    A `def ... rescue ... end` has an IMPLICIT begin: Prism models it as a
    BeginNode wrapping a RescueNode, two nesting levels, while the grammar hangs
    a single `rescue` off `body_statement`. Seeding the frame at 1 restores the
    level the concrete syntax leaves out. An explicit `begin ... rescue` already
    produces both nodes and needs no seed.
    """
    body = node.child_by_field_name("body")
    if body is None:
        return 0
    return 1 if any(c.type in ("rescue", "ensure") for c in body.children) else 0


# Prism keyword pseudo-variables: identifiers to the grammar, dedicated node
# types to Prism, so never call sites.
_PSEUDO_VARIABLES = frozenset({"__FILE__", "__LINE__", "__ENCODING__"})

# Identifiers that are never a receiverless send, whatever the scope says.
_IDENTIFIER_NON_CALL_PARENTS = frozenset(
    {
        "method",
        "singleton_method",
        # `alias new old` names two METHODS; Prism's AliasMethodNode holds
        # method names, so neither identifier is a send.
        "alias",
        # A setter def wraps its name in a `setter` node; the identifier
        # inside it is the method NAME, not a send.
        "setter",
        "call",
        "method_parameters",
        "block_parameters",
        "splat_parameter",
        "hash_splat_parameter",
        "block_parameter",
        "class",
        "module",
        # `rescue X => e` binds e; it is not a send.
        "exception_variable",
    }
)
# `&blk` is deliberately NOT listed. Passing a BOUND name along (`other(&blk)`
# inside `def m(&blk)`) is already suppressed by the locals check above, which
# runs first; an UNBOUND one is a real send -- `middleware.call(..., &yield_block)`
# against an RSpec `let(:yield_block)` is a CallNode to Prism, and excluding the
# position outright dropped every such row.


def _bare(node: Any, text: Callable[[Any], str]) -> dict[str, Any]:
    """A receiverless call row for an identifier in value position."""
    return {
        "name": text(node),
        "receiver": None,
        "kind": "bare",
        "line": node.start_point[0] + 1,
    }


def bare_call_from_identifier(
    node: Any, text: Callable[[Any], str], locals_: set[str]
) -> dict[str, Any] | None:
    """A bare `identifier` that is a method send rather than a variable read.

    Ruby has no syntax distinguishing the two: `params` is a receiverless call
    while `orders` is a local read, and only the binding set tells them apart.
    Prism resolves this while parsing, so a `params[:id]` argument produces a
    bare CallNode row that a purely syntactic reading would miss entirely.

    Conservative in the safe direction: an identifier is only promoted when it
    is NOT bound in scope and NOT sitting in a position where the grammar
    already gives it another meaning (a method name, a parameter, an assignment
    target, a hash key). An unbound name that is genuinely a variable would at
    worst add a row; a bound one is never promoted.
    """
    if node.type != "identifier":
        return None
    name = text(node)
    if name in locals_:
        return None
    # Prism models these as SourceFileNode / SourceLineNode / SourceEncodingNode,
    # never a CallNode, but the grammar reports them as ordinary identifiers.
    if name in _PSEUDO_VARIABLES:
        return None

    parent = node.parent
    if parent is not None:
        if parent.type in ("optional_parameter", "keyword_parameter"):
            # The parameter NAME is a binding; its DEFAULT VALUE is an ordinary
            # expression, so only the name field is suppressed.
            bound = parent.child_by_field_name("name")
            return None if bound is not None and bound.id == node.id else _bare(node, text)
        if parent.type == "call":
            # A call's METHOD name is already reported by call_site. Its
            # RECEIVER is a second send in its own right when nothing bound it:
            # `config.load_defaults` is two CallNodes to Prism, `config` and
            # `load_defaults`, so returning early here loses the outer one.
            method = parent.child_by_field_name("method")
            if method is not None and method.id == node.id:
                return None
            receiver = parent.child_by_field_name("receiver")
            if receiver is None or receiver.id != node.id:
                return None
        elif parent.type in ("method", "singleton_method"):
            # `def m = expr` hangs its body straight off the def node. That body
            # identifier IS a receiverless send (Prism emits a CallNode); the
            # method NAME, also a direct child, is not.
            body = parent.child_by_field_name("body")
            if body is None or body.id != node.id:
                return None
        elif parent.type in _IDENTIFIER_NON_CALL_PARENTS:
            return None
    return {"name": name, "receiver": None, "kind": "bare", "line": node.start_point[0] + 1}


def import_specifier(node: Any, text: Callable[[Any], str]) -> list[tuple[str, str]] | None:
    """(module, kind) rows for require / require_relative / autoload.

    Ruby has no import statement; these three receiverless calls are the only
    static module references Prism recognizes. Zeitwerk autoloading means most
    Rails files produce nothing here, which is why Ruby's import_specifiers is
    documented as partial rather than broken.
    """
    if node.type != "call" or node.child_by_field_name("receiver") is not None:
        return None
    method = node.child_by_field_name("method")
    if method is None:
        return None
    name = text(method)
    if name not in ("require", "require_relative", "autoload"):
        return None

    args = node.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return None

    def string_value(arg: Any) -> str | None:
        if arg.type != "string":
            return None
        return text(arg).strip("\"'")

    if name == "require":
        target = string_value(args.named_children[0])
        return [(target, "default")] if target else None
    if name == "require_relative":
        target = string_value(args.named_children[0])
        return [(target, "namespace")] if target else None

    if len(args.named_children) >= 2:
        target = string_value(args.named_children[1])
        return [(target, "named")] if target else None
    return None
