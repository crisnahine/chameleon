"""A language's tables, built from a declarative spec instead of hand-written.

The three original table modules (python, ruby, typescript) are 700-1100 lines
each, because each reproduces a bespoke dumper byte-for-byte. That cost is
justified where a dumper already existed and parity is the contract; it is not
justified for a language chameleon has never parsed, where there is no prior
output to match and the only requirement is that the facts be TRUE.

So a new language is a spec: which node kinds are functions, which are classes,
which fields hold a name or a parameter list, how a call and an import are
shaped. `build(spec)` turns that into an object satisfying the same table
contract the walker calls, so nothing downstream knows the difference.

The generic implementations are deliberately CONSERVATIVE. Where a shape is
ambiguous they emit nothing rather than guess: a caller cannot tell a wrong
fact from a right one, and this engine's whole value is that its facts are
checkable. An absent signature costs a little context; a fabricated one costs
trust in every signature.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

# A parameter list longer than this is a generated or pathological declaration;
# the signature is not useful and the memory is not free.
_MAX_PARAMS = 64

# Deepest class nesting the enclosing-class walk will report. Beyond this the
# path string stops being readable and stops being useful.
_MAX_CLASS_DEPTH = 8


@dataclass(frozen=True)
class LanguageSpec:
    """Everything the generic tables need to describe one language.

    Node-kind names are tree-sitter grammar node types, and field names are that
    grammar's own field names -- both are stable per grammar version and are
    what the pinned wheels expose.
    """

    name: str
    extensions: tuple[str, ...]
    top_level_kinds: dict[str, str]
    function_nodes: tuple[str, ...] = ()
    class_nodes: tuple[str, ...] = ()
    branch_nodes: tuple[str, ...] = ()
    nesting_nodes: tuple[str, ...] = ()
    skip_subtree_nodes: tuple[str, ...] = ("comment",)

    # Grammar field names. Absent means the language has no such field, and the
    # corresponding fact is simply not emitted.
    name_field: str = "name"
    parameters_field: str = "parameters"
    body_field: str = "body"
    return_type_field: str | None = None
    call_function_field: str = "function"
    member_object_field: str | None = None
    member_property_field: str | None = None

    # Call shapes.
    call_nodes: tuple[str, ...] = ()
    member_nodes: tuple[str, ...] = ()
    # Some grammars hang the receiver off the CALL node rather than off a
    # separate member node (Java's `method_invocation`, PHP's
    # `member_call_expression`), and PHP uses two different field names for the
    # instance and static forms -- hence a tuple, tried in order.
    call_receiver_fields: tuple[str, ...] = ()
    # Kotlin's grammar names neither side of a navigation, so the receiver and
    # the method are simply its first two named children.
    member_positional: bool = False

    # Import shapes.
    import_nodes: tuple[str, ...] = ()
    import_descend_nodes: tuple[str, ...] = ()
    import_module_field: str | None = None

    # Class shapes.
    class_bases_field: str | None = None
    class_base_nodes: tuple[str, ...] = ()

    # Parameter node kinds that count as one declared parameter.
    param_nodes: tuple[str, ...] = ()
    param_rest_nodes: tuple[str, ...] = ()
    param_name_field: str | None = None

    # Node kinds whose presence at top level marks an exported declaration. An
    # empty tuple means the language has no export concept and the counts fall
    # back to "declarations defined here", the way Python's do.
    export_marker_nodes: tuple[str, ...] = ()

    # Receiver names that mean "this object" rather than a named collaborator.
    self_names: frozenset[str] = field(default_factory=frozenset)


def _text_of(node: Any, text: Callable[[Any], str]) -> str:
    try:
        return text(node)
    except Exception:
        return ""


def _field(node: Any, name: str | None) -> Any:
    if not name or node is None:
        return None
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _named_children(node: Any) -> list[Any]:
    if node is None:
        return []
    try:
        return list(node.named_children)
    except Exception:
        return []


def _declared_name(node: Any, text: Callable[[Any], str], spec: LanguageSpec) -> str | None:
    """The declared name of a definition node, or None when it is anonymous.

    An anonymous function (a Go `func literal`, a Rust closure) genuinely has no
    name, and inventing one would put a symbol in the index that no caller can
    ever reference.
    """
    target = _field(node, spec.name_field)
    if target is None:
        return None
    value = _text_of(target, text).strip()
    return value or None


def build(spec: LanguageSpec) -> SimpleNamespace:
    """The tables object for `spec`, satisfying the walker's table contract."""

    def class_shape(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
        name = _declared_name(node, text, spec)
        if name is None:
            return None

        bases: list[str] = []
        holder = _field(node, spec.class_bases_field)
        candidates = _named_children(holder) if holder is not None else []
        if not candidates and spec.class_base_nodes:
            # Some grammars hang the supertype list off the node directly rather
            # than behind a field (Rust's impl/trait items, Java's `superclass`).
            candidates = [c for c in _named_children(node) if c.type in spec.class_base_nodes]
        for base in candidates:
            value = _text_of(base, text).strip()
            if value:
                bases.append(value)

        return {
            "name": name,
            "start_line": node.start_point[0] + 1,
            "bases": bases,
            "extends": bases[0] if bases else None,
            "decorators": [],
        }

    def class_body_calls(
        node: Any, text: Callable[[Any], str], class_name: str
    ) -> list[dict[str, Any]]:
        """No generic class-body DSL concept.

        Ruby's `has_many`-style receiverless macro is a real language feature
        there; nothing in the spec-driven set has an equivalent whose meaning
        could be read off the syntax alone, so nothing is claimed.
        """
        return []

    def _params(node: Any, text: Callable[[Any], str]) -> list[dict[str, Any]]:
        holder = _field(node, spec.parameters_field)
        if holder is None:
            return []
        out: list[dict[str, Any]] = []
        for param in _named_children(holder):
            if len(out) >= _MAX_PARAMS:
                break
            kind = "positional"
            if param.type in spec.param_rest_nodes:
                kind = "rest"
            elif spec.param_nodes and param.type not in spec.param_nodes:
                continue
            named = _field(param, spec.param_name_field)
            label = _text_of(named if named is not None else param, text).strip()
            if not label:
                continue
            out.append({"name": label, "optional": False, "kind": kind})
        return out

    def callable_signature(
        node: Any,
        text: Callable[[Any], str],
        class_ctx: tuple[str, str, str | None, tuple[str, ...]] | None,
    ) -> dict[str, Any] | None:
        name = _declared_name(node, text, spec)
        if name is None:
            return None
        sig: dict[str, Any] = {
            "name": name,
            "kind": "method" if class_ctx is not None else "function",
            "params": _params(node, text),
            "is_default_export": False,
            "is_async": False,
            "enclosing_class": class_ctx[0] if class_ctx else None,
            "enclosing_class_path": class_ctx[1] if class_ctx else None,
            "base_class": class_ctx[2] if class_ctx else None,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "decorators": [],
        }
        returns = _field(node, spec.return_type_field)
        if returns is not None:
            declared = _text_of(returns, text).strip()
            if declared:
                sig["return_type"] = declared
        return sig

    def call_site(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
        """One call row, or None when the callee cannot be named.

        Only a bare identifier callee and a single-identifier receiver are
        recorded. A chained call, a call on a subscript, or a call through an
        expression yields NO row, because the index could not resolve it and a
        half-resolved edge is worse than an absent one.
        """
        if node.type not in spec.call_nodes:
            return None
        line = node.start_point[0] + 1

        def _row(method: str, receiver: str | None) -> dict[str, Any] | None:
            if not method:
                return None
            if receiver is None:
                return {"name": method, "receiver": None, "kind": "bare", "line": line}
            if not receiver:
                return None
            if receiver in spec.self_names:
                return {"name": method, "receiver": "self", "kind": "self", "line": line}
            return {"name": method, "receiver": receiver, "kind": "member", "line": line}

        # Shape 1: the receiver hangs off the call node itself.
        if spec.call_receiver_fields:
            func = _field(node, spec.call_function_field)
            if func is None or func.type != "identifier":
                return None
            recv = next(
                (r for f in spec.call_receiver_fields if (r := _field(node, f)) is not None),
                None,
            )
            if recv is None:
                return _row(_text_of(func, text).strip(), None)
            if recv.type != "identifier":
                # A chained or computed receiver cannot be index-resolved, so no
                # row at all rather than one naming a receiver that is an
                # expression.
                return None
            return _row(_text_of(func, text).strip(), _text_of(recv, text).strip())

        func = _field(node, spec.call_function_field)
        if func is None:
            return None

        if func.type == "identifier":
            return _row(_text_of(func, text).strip(), None)

        if func.type in spec.member_nodes:
            if spec.member_positional:
                parts = _named_children(func)
                if len(parts) < 2 or parts[0].type != "simple_identifier":
                    return None
                recv, prop = parts[0], parts[-1]
            else:
                prop = _field(func, spec.member_property_field)
                recv = _field(func, spec.member_object_field)
                if prop is None or recv is None or recv.type != "identifier":
                    return None
            return _row(_text_of(prop, text).strip(), _text_of(recv, text).strip())
        return None

    def import_specifier(node: Any, text: Callable[[Any], str]) -> list[tuple[str, str]] | None:
        """(module, kind) rows for one import statement.

        Everything is reported as `namespace`: the spec-driven languages bind a
        whole package or module, and claiming `named` would assert a
        symbol-level binding this generic reader has not actually resolved.
        """
        if node.type not in spec.import_nodes:
            return None
        targets: list[Any] = [node]
        for descend in spec.import_descend_nodes:
            expanded: list[Any] = []
            for target in targets:
                nested = [c for c in _named_children(target) if c.type == descend]
                expanded.extend(nested or [target])
            targets = expanded

        out: list[tuple[str, str]] = []
        for target in targets:
            module = _field(target, spec.import_module_field)
            if module is None:
                # Most grammars leave the path unnamed as the statement's first
                # named child (Java's scoped_identifier, C#'s qualified_name,
                # PHP's namespace_use_clause). Taking the STATEMENT's text here
                # instead would record "import java.util.List;" as the module,
                # and every downstream consumer keys on the import ROOT.
                children = _named_children(target)
                module = children[0] if children else None
            value = _text_of(module if module is not None else target, text).strip()
            value = value.strip('"').strip("'").rstrip(";").strip()
            if value:
                out.append((value, "namespace"))
        return out or None

    def export_fields(top_level_kinds: list[str]) -> tuple[str | None, int]:
        """(default export kind, named export count) from the top-level kinds.

        No language in the spec-driven set has JavaScript's single default
        export, so the default is always None and the count is the number of
        top-level declarations -- the same repurposing Python's tables make,
        and what the cluster signature actually consumes.
        """
        declarations = [k for k in top_level_kinds if k]
        return None, len(declarations)

    def refine_top_level_kind(node: Any, text: Callable[[Any], str], mapped: str) -> str:
        return mapped

    return SimpleNamespace(
        language=spec.name,
        TOP_LEVEL_KINDS=dict(spec.top_level_kinds),
        FUNCTION_NODES=frozenset(spec.function_nodes),
        CLASS_NODES=frozenset(spec.class_nodes),
        BRANCH_NODES=frozenset(spec.branch_nodes),
        NESTING_NODES=frozenset(spec.nesting_nodes),
        SKIP_SUBTREE_NODES=frozenset(spec.skip_subtree_nodes),
        class_shape=class_shape,
        class_body_calls=class_body_calls,
        callable_signature=callable_signature,
        call_site=call_site,
        import_specifier=import_specifier,
        export_fields=export_fields,
        refine_top_level_kind=refine_top_level_kind,
        call_site_carries_nesting=False,
        end_line_excludes_comments=False,
    )


def build_all(specs: Iterable[LanguageSpec]) -> dict[str, SimpleNamespace]:
    """Every spec's tables, keyed by language name."""
    return {spec.name: build(spec) for spec in specs}
