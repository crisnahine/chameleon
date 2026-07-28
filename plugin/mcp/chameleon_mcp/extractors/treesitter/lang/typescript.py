"""TypeScript / JavaScript kind tables, matching what ts_dump.mjs emits.

TypeScript is the only one of the three with REAL exports, so
``default_export_kind`` and ``named_export_count`` carry their literal meaning
here rather than the repurposed proxies Ruby and Python use. It is also the
only one whose top-level statements are wrapped: `export function f() {}` is an
``export_statement`` in the grammar, but ts.SyntaxKind reports the declaration
it wraps, so the wrapper is unwrapped at the boundary.

One grammar covers `.ts`/`.mts`/`.cts`, a second covers `.tsx`, and a third
covers `.js`/`.jsx`/`.mjs`/`.cjs` -- but their node vocabularies overlap enough
that one table drives all three.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

language = "typescript"

# tree-sitter rule -> ts.SyntaxKind name. `FirstStatement` is not a typo:
# ts.SyntaxKind is a numeric enum carrying range-marker ALIASES, and
# `ts.SyntaxKind[kind]` reverse-lookup returns the first name registered for
# that number -- VariableStatement's number is also FirstStatement's, so the
# dumper has always emitted the alias and every committed profile is keyed on it.
TOP_LEVEL_KINDS: dict[str, str] = {
    "function_declaration": "FunctionDeclaration",
    # A bodyless overload signature; the TS compiler reports every overload
    # as a FunctionDeclaration and counts each one.
    "function_signature": "FunctionDeclaration",
    "generator_function_declaration": "FunctionDeclaration",
    "class_declaration": "ClassDeclaration",
    "abstract_class_declaration": "ClassDeclaration",
    "interface_declaration": "InterfaceDeclaration",
    "type_alias_declaration": "TypeAliasDeclaration",
    "enum_declaration": "EnumDeclaration",
    "module": "ModuleDeclaration",
    "internal_module": "ModuleDeclaration",
    "ambient_declaration": "ModuleDeclaration",
    "lexical_declaration": "FirstStatement",
    "variable_declaration": "FirstStatement",
    "import_statement": "ImportDeclaration",
    "export_statement": "ExportDeclaration",
    # A bare `{ ... }` at top level is a block statement, not an object.
    "statement_block": "Block",
    "expression_statement": "ExpressionStatement",
    "if_statement": "IfStatement",
    "for_statement": "ForStatement",
    "for_in_statement": "ForInStatement",
    "while_statement": "WhileStatement",
    "do_statement": "DoStatement",
    "switch_statement": "SwitchStatement",
    "try_statement": "TryStatement",
    "return_statement": "ReturnStatement",
    "throw_statement": "ThrowStatement",
    "break_statement": "BreakStatement",
    "continue_statement": "ContinueStatement",
    "labeled_statement": "LabeledStatement",
    "empty_statement": "EmptyStatement",
    "comment": "",
}

FUNCTION_NODES = frozenset(
    {
        "function_declaration",
        "function_signature",
        "generator_function_declaration",
        "generator_function",
        "function_expression",
        "arrow_function",
        "method_definition",
        "method_signature",
        # `abstract m(): T;` is its own rule, not a method_signature -- omitting
        # it made an abstract class report no members at all, which is most of
        # what an abstract class IS.
        "abstract_method_signature",
    }
)
CLASS_NODES = frozenset({"class_declaration", "abstract_class_declaration", "class"})

# ts_dump pushes a NULL class sentinel for an object literal and an anonymous
# class, so members inside one report no enclosing class. A NAMED class is
# re-established immediately afterwards by the CLASS_NODES branch.
CLASS_CONTEXT_RESET_NODES = frozenset({"object", "class"})

# A plain function declared inside a method is not a class member.
CLASS_CONTEXT_OPAQUE_NODES = frozenset(
    {"function_declaration", "function_expression", "generator_function"}
)

# ts_dump isBranchNode: If, For, ForIn, ForOf, While, Do, CaseClause,
# CatchClause, Conditional. The grammar folds for-of into for_in_statement.
BRANCH_NODES = frozenset(
    {
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "switch_case",
        "catch_clause",
        "ternary_expression",
    }
)

# ts_dump isNestingNode: the branch set minus CaseClause/Conditional, plus
# Switch and Try. A case clause scores a decision point at the switch's indent.
NESTING_NODES = frozenset(
    {
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
        "catch_clause",
    }
)

SKIP_SUBTREE_NODES = frozenset({"comment", "string_fragment"})

# Only Prism attaches a lexical nesting list.
call_site_carries_nesting = False

JSX_NODES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})

# Statements that carry an `export` modifier in ts.SyntaxKind terms.
_NAMED_EXPORTABLE = frozenset(
    {
        "FirstStatement",
        "FunctionDeclaration",
        "ClassDeclaration",
        "EnumDeclaration",
        "InterfaceDeclaration",
        "TypeAliasDeclaration",
        "ModuleDeclaration",
    }
)


def _unwrap_export(node: Any) -> tuple[Any, bool, bool]:
    """Return (inner declaration, is_export, is_default) for a top-level node.

    ts.SyntaxKind reports the DECLARATION for `export function f() {}` because
    `export` is a modifier there, while the grammar wraps it in an
    `export_statement`. Everything downstream keys on the declaration kind, so
    the wrapper is peeled here and its two modifier facts returned alongside.
    """
    if node.type != "export_statement":
        return node, False, False
    is_default = any(c.type == "default" for c in node.children)
    inner = node.child_by_field_name("declaration")
    # `export declare class X {}` wraps the real declaration; the TS compiler
    # treats `declare` as a modifier and reports the inner kind and name.
    if inner is not None and inner.type == "ambient_declaration" and inner.named_children:
        inner = inner.named_children[0]
    return (inner if inner is not None else node), True, is_default


def refine_top_level_kind(node: Any, text: Callable[[Any], str], mapped: str) -> str:
    """Report the wrapped declaration's kind, not the export wrapper's."""
    if node.type == "export_statement" and any(c.type == "=" for c in node.children):
        # `export = x`, the TypeScript export-assignment form. It shares the
        # `export_statement` rule with every other export, and the `=` token is
        # anonymous, so nothing in the named children distinguishes it.
        return "ExportAssignment"

    if node.type == "ambient_declaration":
        # `declare` is a MODIFIER to the compiler, so `declare class C {}` is a
        # ClassDeclaration and `declare function f()` a FunctionDeclaration --
        # only the genuinely module-shaped forms (`declare module`, `declare
        # namespace`, `declare global`) are ModuleDeclaration. Reporting the
        # wrapper's kind for all of them mislabeled every ambient class in a
        # hand-written .d.ts.
        inner = node.named_children[0] if node.named_children else None
        if inner is None or inner.type == "statement_block":
            # `declare global { ... }` wraps a bare block, which is a MODULE to
            # the compiler -- the block mapping below is for a free-standing
            # `{ ... }` statement and must not win here.
            return mapped
        return TOP_LEVEL_KINDS.get(inner.type, mapped)

    if node.type == "empty_statement":
        # A bodyless shorthand ambient module (`declare module "x";`) leaves its
        # terminating `;` as a separate statement here, while ts.SyntaxKind folds
        # it into the ModuleDeclaration. "" is dropped by the extractor's filter.
        prev = node.prev_named_sibling
        if (
            prev is not None
            and prev.type == "ambient_declaration"
            and prev.named_children
            and prev.named_children[0].child_by_field_name("body") is None
        ):
            return ""
        return mapped

    if node.type == "for_in_statement":
        # The grammar folds `for...of` into for_in_statement, distinguished only
        # by the operator; ts.SyntaxKind has a separate ForOfStatement.
        operator = node.child_by_field_name("operator")
        if operator is not None and operator.type == "of":
            return "ForOfStatement"
        return mapped

    if node.type != "export_statement":
        return mapped
    inner, _is_export, _is_default = _unwrap_export(node)
    if inner is node:
        # `export default <expr>` carries a `value` and is an ExportAssignment;
        # a bare `export { a, b }` / `export * from "m"` is an ExportDeclaration.
        if node.child_by_field_name("value") is not None:
            return "ExportAssignment"
        return "ExportDeclaration"
    return TOP_LEVEL_KINDS.get(inner.type, mapped)


def export_fields(top_level_kinds: list[str]) -> tuple[str | None, int]:
    """Unused for TypeScript: the export facts need node access, not kinds.

    ts_dump reads real `export` / `export default` modifiers and counts the
    bindings inside `export { a, b }` and `export const a, b`, none of which is
    recoverable from a flat kind list. ``export_facts`` does the real work; this
    exists so the tables satisfy the same protocol as the other languages.
    """
    return None, 0


def export_facts(root: Any, text: Callable[[Any], str]) -> tuple[str | None, int]:
    """Real (default_export_kind, named_export_count) from the top-level nodes.

    `export default class {}` reports the CLASS kind, and `export default foo`
    reports the kind of the expression it exports. The count sums direct
    `export` declarations (one per binding for a variable statement, since
    `export const a = 1, b = 2` exports two) plus the elements of every
    `export { ... }` clause.
    """
    default_kind: str | None = None
    named = 0

    for stmt in root.named_children:
        if stmt.type != "export_statement":
            continue
        inner, _is_export, is_default = _unwrap_export(stmt)

        if is_default and default_kind is None:
            if inner is not stmt:
                default_kind = TOP_LEVEL_KINDS.get(inner.type, "Unknown")
            else:
                value = stmt.child_by_field_name("value")
                if value is not None:
                    default_kind = _EXPRESSION_KINDS.get(value.type, "Identifier")
            continue

        if inner is not stmt:
            kind = TOP_LEVEL_KINDS.get(inner.type)
            if kind in _NAMED_EXPORTABLE:
                if kind == "FirstStatement":
                    named += (
                        sum(1 for c in inner.named_children if c.type == "variable_declarator") or 1
                    )
                else:
                    named += 1
            continue

        for child in stmt.named_children:
            if child.type == "export_clause":
                named += sum(1 for c in child.named_children if c.type == "export_specifier")

    return default_kind, named


_EXPRESSION_KINDS = {
    "identifier": "Identifier",
    "class": "ClassExpression",
    "function_expression": "FunctionExpression",
    "arrow_function": "ArrowFunction",
    "object": "ObjectLiteralExpression",
    "array": "ArrayLiteralExpression",
    "call_expression": "CallExpression",
    "new_expression": "NewExpression",
    "member_expression": "PropertyAccessExpression",
    "satisfies_expression": "SatisfiesExpression",
    "subscript_expression": "ElementAccessExpression",
    "parenthesized_expression": "ParenthesizedExpression",
    "binary_expression": "BinaryExpression",
    "ternary_expression": "ConditionalExpression",
    "await_expression": "AwaitExpression",
    "as_expression": "AsExpression",
    "string": "StringLiteral",
    "true": "TrueKeyword",
    "false": "FalseKeyword",
    "null": "NullKeyword",
    # Two SyntaxKind values the compiler reports under ALIAS names -- `number`
    # is FirstLiteralToken, not NumericLiteral, and a template literal is
    # FirstTemplateToken. Both were read off ts_dump rather than guessed.
    "number": "FirstLiteralToken",
    "template_string": "FirstTemplateToken",
}


def _name_of(node: Any, text: Callable[[Any], str]) -> str | None:
    name = node.child_by_field_name("name")
    return text(name) if name is not None else None


# Higher-order components whose call wraps a callable. The wrapped thing is
# already indexed as a callable, so the binding is not a plain value export.
_WRAPPER_CALLEES = frozenset({"forwardRef", "memo", "observer", "connect"})


def _is_wrapper_call(node: Any, text: Callable[[Any], str]) -> bool:
    if node is None or node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "identifier":
        return text(fn) in _WRAPPER_CALLEES
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return prop is not None and text(prop) in _WRAPPER_CALLEES
    return False


def _simple_type_name(type_node: Any, text: Callable[[Any], str]) -> str | None:
    """The bare identifier a type annotation names, or None.

    Only a plain `Foo` qualifies. A qualified name, a generic, a union, or an
    array yields None so the DI resolver never guesses a receiver type from a
    shape it cannot resolve deterministically.
    """
    if type_node is None:
        return None
    inner = type_node
    if inner.type == "type_annotation":
        inner = inner.named_children[0] if inner.named_children else None
    if inner is None:
        return None
    if inner.type == "type_identifier":
        return text(inner)
    if inner.type == "generic_type":
        return None
    return None


def known_parse_defect(node: Any, src: bytes) -> bool:
    """True when an ERROR/MISSING node is a GRAMMAR limitation, not bad syntax.

    The reference is the TypeScript compiler, which accepts all of the code
    below; tree-sitter's TS grammar does not, and its last grammar change was
    September 2024 with every issue here still open. Left uncorrected, chameleon
    reports phantom syntax errors on ordinary modern React and NestJS code --
    the styled-components form alone accounts for 349 of 352 flagged files in a
    21,904-file repo.

    Deliberately keyed on the exact recovery SHAPE each defect produces, not on
    "an error appeared near a template literal": a loose rule here hides real
    syntax errors, which is a far worse failure than over-reporting. Every entry
    was confirmed by parsing the construct and reading the node back, and each
    is re-checked whenever the grammar pin moves, since a fixed defect must stop
    matching rather than silently suppress something new.
    """
    parent = node.parent
    parent_type = parent.type if parent is not None else ""
    body = src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    # `styled.div<{ a?: boolean }>` -- a tagged template whose type argument is
    # structural (object, tuple, or function type). The grammar reads `>` plus
    # the backtick as a non-null assertion and reports its `!` missing. A named
    # type argument (`styled.div<Props>`) parses fine, which is why this went
    # unseen until a real styled-components codebase.
    if node.is_missing and node.type == "!" and parent_type == "non_null_expression":
        return True

    # A JSX attribute holding a URL query string: the `&` starts what the
    # grammar takes for an entity and the rest of the value errors out.
    # tree-sitter-typescript#320.
    if parent_type in ("string", "jsx_element", "jsx_fragment") and body.startswith("&"):
        return True

    # An escape the grammar rejects inside a template literal. Legal in source:
    # `String.raw` receives the RAW text, so `\u{200bCorp` never has to be a
    # valid escape, and the compiler accepts it.
    if parent_type == "template_string" and body.startswith("\\"):
        return True

    # Variance annotations, `interface Foo<in T, out U>`. The grammar's
    # `type_parameter` rule has no `in`/`out`; TypeScript 4.7 syntax.
    if parent_type == "type_parameters":
        before = src[max(0, node.start_byte - 6) : node.start_byte].decode("utf-8", "replace")
        if before.rstrip().endswith(("in", "out")):
            return True

    # `export type * as N from "./m"`. tree-sitter-typescript#348 (PR #358 open).
    if parent_type == "export_statement" and body == "type":
        after = src[node.end_byte : node.end_byte + 4].decode("utf-8", "replace")
        if after.lstrip().startswith("*"):
            return True

    # The import-type family: `import("m").T<A>` in a type annotation (#322),
    # `keyof import("m").T` (#352), and `f<typeof import("m")>()` in expression
    # position. All three leave recovery inside the enclosing statement, so the
    # statement itself has to carry an `import(` for this to fire.
    if parent_type in (
        "type_assertion",
        "expression_statement",
        "lexical_declaration",
        "parenthesized_expression",
        "arguments",
    ):
        # Checked against the error's own LINE and a few enclosing nodes rather
        # than one statement node. This misparse fragments the construct -- a
        # declaration splits into a truncated `lexical_declaration` plus a stray
        # `expression_statement` -- so no single node still spans the `import(`
        # that caused it, and in a multi-line call the `import(` sits on an
        # earlier line than the recovery.
        start = src.rfind(b"\n", 0, node.start_byte) + 1
        end = src.find(b"\n", node.end_byte)
        window = src[start : end if end != -1 else len(src)]
        ancestor = parent
        for _ in range(4):
            if ancestor is None:
                break
            span = src[ancestor.start_byte : ancestor.end_byte]
            if len(span) <= 400:
                window = span
            ancestor = ancestor.parent
        if "import(" in window.decode("utf-8", "replace").replace(" ", ""):
            return True

    return False


def class_property_types(node: Any, text: Callable[[Any], str]) -> dict[str, str]:
    """Instance property -> declared type, for the dependency-injection grade.

    Covers both spellings: a `private x: Foo` field declaration and a
    constructor PARAMETER PROPERTY (`constructor(private x: Foo)`), which is the
    shape NestJS and Angular actually inject through.
    """
    out: dict[str, str] = {}
    body = node.child_by_field_name("body")
    if body is None:
        return out

    for member in body.named_children:
        if member.type == "public_field_definition":
            name = member.child_by_field_name("name")
            # `#x` is an ECMAScript private name, which the compiler models as a
            # PrivateIdentifier rather than a property name -- it can never be
            # the target of injection, so ts_dump records no row for it.
            if name is not None and name.type == "private_property_identifier":
                continue
            declared = _simple_type_name(member.child_by_field_name("type"), text)
            if name is not None and declared:
                out[text(name)] = declared
        elif member.type == "method_definition":
            method_name = member.child_by_field_name("name")
            if method_name is None or text(method_name) != "constructor":
                continue
            params = member.child_by_field_name("parameters")
            if params is None:
                continue
            for param in params.named_children:
                # A parameter property carries an accessibility or readonly
                # modifier; a plain parameter declares no field.
                if not any(
                    c.type in ("accessibility_modifier", "override_modifier", "readonly")
                    for c in param.children
                ):
                    continue
                pattern = param.child_by_field_name("pattern")
                declared = _simple_type_name(param.child_by_field_name("type"), text)
                if pattern is not None and declared:
                    out[text(pattern)] = declared
    return out


def _binding_names(node: Any, text: Callable[[Any], str], into: set[str]) -> None:
    """Every name a declarator's binding side introduces, patterns included."""
    if node is None:
        return
    if node.type == "identifier":
        into.add(text(node))
        return
    for child in node.named_children:
        if child.type in ("shorthand_property_identifier_pattern", "identifier"):
            into.add(text(child))
        elif child.type in ("object_pattern", "array_pattern", "pair_pattern"):
            _binding_names(child, text, into)


def file_extras(
    root: Any, text: Callable[[Any], str], path: Any, walked: Any = None
) -> dict[str, Any]:
    """The seven whole-file rows ts_dump emits beyond the core ParsedFile.

    All are cross-file-intelligence inputs: they feed the exports index, the
    reverse index, the phantom-symbol check, and the DI call-resolution grade.
    """
    import_symbols: list[dict[str, Any]] = []
    namespace_imports: list[dict[str, Any]] = []
    re_exports: list[dict[str, Any]] = []
    value_export_bindings: list[dict[str, Any]] = []
    names: set[str] = set()
    export_set_open = False

    for stmt in root.named_children:
        line = stmt.start_point[0] + 1

        if stmt.type == "import_statement":
            # `import type { T } from "m"` binds no VALUE, so it contributes no
            # symbol row: the reverse index tracks runtime edges, and a type-only
            # import cannot break at runtime when the export is removed.
            if any(c.type == "type" for c in stmt.children):
                continue
            source = stmt.child_by_field_name("source")
            module = text(source).strip("\"'`") if source is not None else ""
            for clause in stmt.named_children:
                if clause.type != "import_clause":
                    continue
                for part in clause.named_children:
                    if part.type == "namespace_import":
                        alias = part.named_children[0] if part.named_children else None
                        if alias is not None:
                            namespace_imports.append(
                                {"alias": text(alias), "module": module, "line": line}
                            )
                    elif part.type == "named_imports":
                        for spec in part.named_children:
                            if spec.type != "import_specifier":
                                continue
                            # The per-specifier form, `import { type T, v }`:
                            # only T is type-only, so the skip is per row rather
                            # than per statement.
                            if any(c.type == "type" for c in spec.children):
                                continue
                            name_node = spec.child_by_field_name("name")
                            alias_node = spec.child_by_field_name("alias")
                            if name_node is None:
                                continue
                            # `import { default as X }` binds the module's
                            # DEFAULT export through named-import syntax, and
                            # the row is keyed on the source-exported name --
                            # which for a default is not a name the reverse
                            # index can resolve. The dumper drops it for that
                            # reason; a row here would have no counterpart.
                            if text(name_node) == "default":
                                continue
                            import_symbols.append(
                                {
                                    "name": text(name_node),
                                    "local": text(alias_node)
                                    if alias_node is not None
                                    else text(name_node),
                                    "module": module,
                                    "line": line,
                                }
                            )
            continue

        if stmt.type != "export_statement":
            continue

        inner, _is_export, is_default = _unwrap_export(stmt)

        source = stmt.child_by_field_name("source")
        clause = next((c for c in stmt.named_children if c.type == "export_clause"), None)

        if source is not None and clause is None:
            # `export * from "./m"` re-exports an unknowable set, so the file's
            # own export set has to be treated as open.
            export_set_open = True
            ns = next((c for c in stmt.named_children if c.type == "namespace_export"), None)
            if ns is not None:
                export_set_open = False
                alias = ns.named_children[0] if ns.named_children else None
                if alias is not None:
                    names.add(text(alias))
            continue

        if clause is not None:
            module = text(source).strip("\"'`") if source is not None else None
            for spec in clause.named_children:
                if spec.type != "export_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                alias_node = spec.child_by_field_name("alias")
                if name_node is None:
                    continue
                # `name` is the source module's name and `alias` is what a
                # consumer imports; with no alias the two coincide.
                exported = text(alias_node) if alias_node is not None else text(name_node)
                origin = text(name_node)
                names.add(exported)
                type_only = any(c.type == "type" for c in stmt.children) or any(
                    c.type == "type" for c in spec.children
                )
                if module and origin != "default" and not type_only:
                    re_exports.append(
                        {
                            "exported": exported,
                            "origin": origin,
                            "module": module,
                            "line": line,
                        }
                    )
            continue

        if is_default or inner is stmt:
            continue

        kind = TOP_LEVEL_KINDS.get(inner.type)
        if kind == "FirstStatement":
            for decl in inner.named_children:
                if decl.type != "variable_declarator":
                    continue
                _binding_names(decl.child_by_field_name("name"), text, names)
                target = decl.child_by_field_name("name")
                if target is None or target.type != "identifier":
                    continue
                init = decl.child_by_field_name("value")
                if init is not None and (
                    init.type in ("arrow_function", "function_expression", "class")
                    or _is_wrapper_call(init, text)
                ):
                    # Already indexed as a callable or a class; recording it here
                    # too would double-count the same binding.
                    continue
                value_export_bindings.append(
                    {"name": text(target), "line": target.start_point[0] + 1}
                )
        elif kind in _NAMED_EXPORTABLE:
            exported = _name_of(inner, text)
            if exported:
                names.add(exported)

    return {
        "import_symbols": import_symbols,
        "namespace_imports": namespace_imports,
        "named_export_names": sorted(names),
        "export_set_open": export_set_open,
        "re_exports": re_exports,
        "value_export_bindings": value_export_bindings,
    }


def _heritage_name(node: Any, text: Callable[[Any], str]) -> str:
    """The bare NAME of a heritage entry.

    ts_dump reads `t.expression` and ignores `t.typeArguments`, so a qualified
    base reduces to its trailing property (`React.Component` -> `Component`)
    and a generic drops its arguments (`DeltaContainer<AppState>` ->
    `DeltaContainer`). Keeping either would make every parameterized or
    namespaced base its own distinct string.
    """
    current = node
    while True:
        if current.type == "generic_type":
            nxt = current.child_by_field_name("name")
        elif current.type in ("member_expression", "nested_type_identifier"):
            nxt = current.child_by_field_name("property") or current.child_by_field_name("name")
        elif current.type == "call_expression":
            # A mixin base, `extends AuthGuard('google-apis')`. ts_dump reads
            # `t.expression`, so the base is the CALLEE; keeping the call text
            # would make every argument list its own distinct base string (and
            # drag embedded newlines into it).
            nxt = current.child_by_field_name("function")
        else:
            return text(current)
        if nxt is None:
            return text(current)
        current = nxt


def _decorator_name(node: Any, text: Callable[[Any], str]) -> str:
    """A decorator's callee NAME, matching ts_dump's `decoratorName`.

    `@Injectable()` is a call whose expression is the identifier; `@Injectable`
    is the identifier itself. Either way only the name is recorded -- keeping
    the argument list would make every parameterized `@Module({...})` a unique
    string, which the framework-role consumers match against exactly.
    """
    inner = node.named_children[0] if node.named_children else None
    if inner is not None and inner.type == "call_expression":
        callee = inner.child_by_field_name("function")
        return text(callee).strip() if callee is not None else text(inner).strip()
    return text(inner).strip() if inner is not None else text(node).lstrip("@").strip()


def _decorators_of(node: Any, text: Callable[[Any], str]) -> list[Any]:
    """Decorator nodes attached to ``node``.

    The grammar makes a decorator a SIBLING, never a child: a class's sits
    under the enclosing `export_statement` and a method's under `class_body`,
    both immediately before the thing they decorate. Reading `named_children`
    therefore found nothing on the two shapes that matter most -- `@Module`,
    `@Injectable`, `@Controller` on an exported class is the whole of NestJS.
    """
    found = [c for c in node.named_children if c.type == "decorator"]
    preceding = []
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type in ("decorator", "comment"):
        # Comments are NAMED nodes, so a doc comment written between two stacked
        # decorators ends the walk early and drops every decorator above it --
        # on a real entity that meant reporting 1 of 5.
        if sibling.type == "decorator":
            preceding.append(sibling)
        sibling = sibling.prev_named_sibling
    # The sibling walk runs backwards, so `@A @B class C` would report B then A.
    # Stacked decorators are order-significant to the reader and the row is
    # compared positionally, so restore source order.
    preceding.reverse()
    return preceding + found


def class_shape(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Shape row for a class: name, extends, implements, decorators."""
    name = _name_of(node, text)
    if name is None:
        return None

    extends: str | None = None
    implements: list[str] = []
    heritage = node.child_by_field_name("heritage")
    clauses = (
        [heritage]
        if heritage is not None
        else [c for c in node.named_children if c.type == "class_heritage"]
    )
    for clause in clauses:
        if clause is None:
            continue
        for child in clause.named_children:
            if child.type == "extends_clause":
                for value in child.named_children:
                    if value.type == "type_arguments":
                        continue
                    extends = _heritage_name(value, text)
                    break
            elif child.type == "implements_clause":
                implements.extend(
                    _heritage_name(v, text)
                    for v in child.named_children
                    if v.type != "type_arguments"
                )
            elif extends is None and child.type != "type_arguments":
                # `.js`/`.jsx` are parsed by the JAVASCRIPT grammar, which has no
                # `extends_clause` -- it hangs the base expression directly off
                # `class_heritage` (the `extends` keyword is an anonymous token).
                # Reading only the TypeScript shape silently dropped the base
                # class of every ES6 class in a .js file, which is most of a
                # pre-TS React codebase.
                extends = _heritage_name(child, text)

    decorators = [_decorator_name(c, text) for c in _decorators_of(node, text)]

    # Anchored to the NAME identifier, not the node: a decorated class that is
    # not exported carries its decorator as a CHILD, so `node.start_point` lands
    # on the `@Decorator` line. ts_dump anchors to the name for exactly this
    # reason -- so the line agrees with the Python dumper's `class` line for the
    # same shape -- and an exported class only matched by luck, its decorator
    # being a sibling under `export_statement` rather than a child.
    name_node = node.child_by_field_name("name")
    shape: dict[str, Any] = {
        "name": name,
        "start_line": (name_node or node).start_point[0] + 1,
        "decorators": decorators,
        "extends": extends,
        "implements": implements,
    }
    return shape


def class_body_calls(
    node: Any, text: Callable[[Any], str], class_name: str
) -> list[dict[str, Any]]:
    """TypeScript has no receiverless class-body DSL; Ruby-exclusive."""
    return []


_PARAM_KINDS = {
    "required_parameter": ("positional", False),
    "optional_parameter": ("optional", True),
    "rest_pattern": ("rest", True),
    "identifier": ("positional", False),
}


def _params(node: Any, text: Callable[[Any], str]) -> list[dict[str, Any]]:
    plist = node.child_by_field_name("parameters")
    if plist is None:
        # A single unparenthesized arrow parameter (`data => data.x`) hangs off
        # a singular `parameter` field with no list wrapper around it.
        single = node.child_by_field_name("parameter")
        if single is not None:
            return [{"name": text(single), "optional": False, "kind": "positional"}]
        return []

    out: list[dict[str, Any]] = []
    for param in plist.named_children:
        if param.type == "comment":
            continue
        # An untyped default (`object = {}`) is an assignment_pattern with
        # left/right fields, not a required_parameter with pattern/value.
        pattern = param.child_by_field_name("pattern") or param.child_by_field_name("left") or param
        kind = "positional"
        # Droppable either way it is spelled: `x?: T` marks the parameter
        # optional in the grammar, `x: T = v` carries a default value instead.
        # Both are callable without the argument, and both stay `positional` --
        # only the droppability flag changes.
        optional = (
            param.type in ("optional_parameter", "assignment_pattern")
            or param.child_by_field_name("value") is not None
        )
        name = text(pattern)
        if pattern.type in ("object_pattern", "array_pattern"):
            # A binding pattern has no single name, so ts_dump reports the
            # PLACEHOLDER shape rather than the destructuring source: every
            # `{ a, b }` param would otherwise be a unique string, and the
            # consensus-param comparison groups on this value.
            kind = "destructured"
            # ts_dump reports the same placeholder for BOTH binding-pattern
            # kinds; there is no separate array spelling.
            name = "{}"
        elif pattern.type == "rest_pattern":
            # The binding NAME is the identifier, not the spread syntax: TS
            # reports `...values` as `values`.
            kind, optional = "rest", True
            inner = pattern.named_children[0] if pattern.named_children else None
            name = text(inner) if inner is not None else name.lstrip(".")
        shape: dict[str, Any] = {"name": name, "optional": optional, "kind": kind}
        annotation = param.child_by_field_name("type")
        if annotation is not None:
            shape["type"] = text(annotation).lstrip(":").strip()
        out.append(shape)
    return out


_METHOD_KINDS = {
    "method_definition": "method",
    "method_signature": "method",
    "abstract_method_signature": "method",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_expression": "function",
    "generator_function": "function",
    # An arrow bound to a name is just a function to ts_dump; there is no
    # separate arrow kind in the taxonomy the class-contract consumer reads.
    "arrow_function": "function",
}


def callable_signature(
    node: Any,
    text: Callable[[Any], str],
    class_ctx: tuple[str, str, str | None, tuple[str, ...]] | None,
) -> dict[str, Any] | None:
    """Signature row for any function-like node."""
    if node.type == "method_signature" and (
        node.parent is None or node.parent.type != "class_body"
    ):
        # An interface or type-literal member declares a shape, not a body: no
        # frame, no signature row.
        return None

    anonymous = False
    name_node = node.child_by_field_name("name")
    if name_node is not None and name_node.type == "computed_property_name":
        # `*[Symbol.iterator]() {}`: ts_dump's callableNameOf returns null for a
        # ComputedPropertyName, so the callable -- and its enclosed calls'
        # caller -- is <anonymous>, with no signature row.
        name = None
    elif name_node is not None and name_node.type == "string":
        # A quoted member name (`"f64-rem"(x, y) {}`) reports its UNQUOTED
        # value; the grammar's text includes the quote characters.
        name = text(name_node).strip("\"'`")
    else:
        name = _name_of(node, text)
    if name is None:
        # An arrow bound to a declarator takes that name (`const f = () => {}`
        # is `f`). A genuinely anonymous one -- a callback argument -- still
        # opens a body-shape frame and owns the calls inside it, so it is
        # reported as `<anonymous>` rather than dropped.
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator":
            name = _name_of(parent, text)
        elif parent is not None and parent.type == "arguments":
            # `const X = forwardRef((props, ref) => {})`: ts_dump walks up
            # through wrapper calls to the enclosing declarator and names the
            # arrow `X`, so its body's calls are attributed to X, not <anonymous>.
            anc = parent.parent
            while anc is not None and (
                _is_wrapper_call(anc, text) or anc.type == "parenthesized_expression"
            ):
                anc = anc.parent
            if anc is not None and anc.type == "variable_declarator":
                name = _name_of(anc, text)
        if name is None:
            name = "<anonymous>"
            anonymous = True

    kind = _METHOD_KINDS.get(node.type, "function")
    if node.type in ("method_definition", "method_signature", "abstract_method_signature"):
        # `constructor`, `get` and `set` are each their own ts.SyntaxKind, not
        # methods that happen to be spelled that way.
        if name == "constructor":
            kind = "constructor"
        elif any(c.type == "get" for c in node.children):
            kind = "getter"
        elif any(c.type == "set" for c in node.children):
            kind = "setter"
        else:
            kind = "method"

    decorator_nodes = _decorators_of(node, text)
    decorators = [_decorator_name(c, text) for c in decorator_nodes]

    # `enclosing_class` is always present (null at top level), but the other two
    # are OMITTED entirely outside a class rather than emitted as null -- a
    # top-level function row carries exactly one of the three.
    sig: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "params": _params(node, text),
        **(
            {"return_type": text(_ret).lstrip(":").strip()}
            if (_ret := node.child_by_field_name("return_type")) is not None
            else {}
        ),
        "is_default_export": _is_default_exported(node),
        # A decorator is part of the decorated node in the TS AST, so
        # `node.getStart()` -- what ts_dump reports for a callable -- lands on
        # the first decorator, not on the method name. (A CLASS is deliberately
        # the other way: ts_dump anchors it to the name identifier so the line
        # agrees with the Python dumper's `class` line, which is why
        # class_shape is left alone here.)
        "start_line": (
            min(d.start_point[0] for d in decorator_nodes) + 1
            if decorator_nodes
            else node.start_point[0] + 1
        ),
        "end_line": node.end_point[0] + 1,
        "enclosing_class": class_ctx[0] if (class_ctx and kind != "function") else None,
    }
    # ts_dump gates all three class fields on isMethod: a plain function
    # nested in a method is not a member of that class.
    if class_ctx is not None and kind != "function":
        sig["enclosing_class_path"] = class_ctx[1]
        if class_ctx[2] is not None:
            sig["base_class"] = class_ctx[2]
    if decorators:
        sig["decorators"] = decorators
    if anonymous:
        sig["_anonymous"] = True
    return sig


def _is_default_exported(node: Any) -> bool:
    """True when this declaration is the file's `export default`."""
    parent = node.parent
    return (
        parent is not None
        and parent.type == "export_statement"
        and any(c.type == "default" for c in parent.children)
    )


def _unwrap_receiver(node: Any) -> Any:
    """See through non-null assertions and parentheses on a call receiver.

    `this.foo!.m()` and `(this.foo).m()` both dispatch on `this.foo`; leaving
    the wrapper in place drops the call edge asymmetrically from otherwise
    identical code.
    """
    while node is not None and node.type in ("non_null_expression", "parenthesized_expression"):
        inner = node.child_by_field_name("expression")
        if inner is None:
            inner = node.named_children[0] if node.named_children else None
        if inner is None:
            return node
        node = inner
    return node


def call_site(node: Any, text: Callable[[Any], str]) -> dict[str, Any] | None:
    """Call row for a call or construction.

    Only depth-1 chains carry a resolvable receiver. A deeper chain
    (`api.utils.helper()`) dispatches through a property of a property, so the
    receiver's export set proves nothing about the callee and the site is
    dropped rather than emitted with a null receiver -- which would be
    byte-identical to a genuinely receiverless call and let the builder
    fabricate an import edge.
    """
    if node.type not in ("call_expression", "new_expression"):
        return None
    is_new = node.type == "new_expression"
    if not is_new:
        # A tagged template (``tag`...` ``) parses as a call_expression whose
        # `arguments` field is a template_string. The TS compiler models it as a
        # TaggedTemplateExpression, which ts_dump never records, so `it.each`...``
        # would otherwise be a phantom member call.
        tagged = node.child_by_field_name("arguments")
        if tagged is not None and tagged.type == "template_string":
            return None
    callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
    # `await f<T>(...)` parses as call(function: await_expression), inverting the
    # usual await(call) nesting; the TS compiler sees the bare identifier.
    while callee is not None and callee.type == "await_expression":
        callee = callee.named_children[0] if callee.named_children else None
    if callee is None:
        return None
    line = node.start_point[0] + 1

    if callee.type == "identifier":
        return {
            "name": text(callee),
            "receiver": None,
            "kind": "new" if is_new else "bare",
            "line": line,
        }

    if callee.type == "member_expression":
        prop = callee.child_by_field_name("property")
        if prop is None:
            return None
        name = text(prop)
        recv = _unwrap_receiver(callee.child_by_field_name("object"))
        if recv is None:
            return None

        if recv.type == "this":
            return {"name": name, "receiver": "this", "kind": "this", "line": line}
        if recv.type == "super":
            return {"name": name, "receiver": "super", "kind": "super", "line": line}

        # `this.<prop>.<method>()` -- the dependency-injection shape. Only plain
        # calls: `new this.x.Foo()` is not a construction worth resolving.
        if (
            not is_new
            and recv.type == "member_expression"
            and (inner_obj := recv.child_by_field_name("object")) is not None
            and inner_obj.type == "this"
            and (inner_prop := recv.child_by_field_name("property")) is not None
            and inner_prop.type == "property_identifier"
        ):
            return {
                "name": name,
                "receiver": text(inner_prop),
                "kind": "this_prop",
                "line": line,
            }

        if recv.type != "identifier":
            return None
        return {
            "name": name,
            "receiver": text(recv),
            "kind": "new" if is_new else "member",
            "line": line,
        }

    return None


def import_specifier(node: Any, text: Callable[[Any], str]) -> list[tuple[str, str]] | None:
    """(module, kind) rows where kind is default | named | namespace."""
    if node.type != "import_statement":
        return None
    # ts_dump iterates sourceFile.statements only, so an import nested inside a
    # `declare module "x" { ... }` block is never recorded. The walker calls this
    # per node (Python needs function-local imports), hence the explicit guard.
    if node.parent is None or node.parent.type != "program":
        return None
    source = node.child_by_field_name("source")
    if source is None:
        return None
    module = text(source).strip("\"'`")
    if not module:
        return None

    # ONE row per statement, priority default > named > namespace -- ts_dump
    # calls importKindFor once on the clause. `import React, { useState }` is a
    # single ('react','default'), not one row per binding form.
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is None:
        # A bare side-effect import (`import "./styles.css"`) still binds it.
        return [(module, "namespace")]
    kinds = {part.type for part in clause.named_children}
    if "identifier" in kinds:
        return [(module, "default")]
    if "named_imports" in kinds:
        return [(module, "named")]
    return [(module, "namespace")]
