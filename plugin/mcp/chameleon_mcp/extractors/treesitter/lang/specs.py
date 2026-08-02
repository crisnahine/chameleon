"""Declarative language specs for the spec-driven tables.

Adding a language here is data, not code: node kinds and grammar field names,
which `spec_driven.build` turns into the same table object the walker calls for
Python, Ruby and TypeScript.

Every node kind and field name below is that grammar's own, taken from the
tree-sitter wheel this package pins. `tests/unit/test_spec_driven_lang.py`
validates each spec against its LOADED grammar, so a spec naming a node the
grammar does not have fails in CI rather than silently matching nothing -- which
is the failure mode a table of string constants invites.

These languages are EXTRACTION-grade, not full-parity: chameleon derives
archetypes, conventions, signatures, call sites and imports for them, and says
so honestly in `docs/language-support-matrix.md`. The lint rules that need a
per-language signal source stay scoped to the three first-class languages rather
than being listed active and firing on nothing.
"""

from __future__ import annotations

from typing import Final

from chameleon_mcp.extractors.treesitter.lang.spec_driven import LanguageSpec

GO: Final = LanguageSpec(
    name="go",
    extensions=(".go",),
    top_level_kinds={
        "function_declaration": "FuncDecl",
        "method_declaration": "FuncDecl",
        "type_declaration": "TypeDecl",
        "import_declaration": "ImportDecl",
        "var_declaration": "VarDecl",
        "const_declaration": "ConstDecl",
        "package_clause": "PackageClause",
        "comment": "",
    },
    function_nodes=("function_declaration", "method_declaration"),
    class_nodes=("type_spec",),
    branch_nodes=("if_statement", "for_statement", "expression_case", "type_case"),
    nesting_nodes=(
        "if_statement",
        "for_statement",
        "expression_switch_statement",
        "type_switch_statement",
        "select_statement",
    ),
    skip_subtree_nodes=("comment", "raw_string_literal"),
    return_type_field="result",
    member_object_field="operand",
    member_property_field="field",
    call_nodes=("call_expression",),
    member_nodes=("selector_expression",),
    import_nodes=("import_declaration",),
    # The path field lives on `import_spec`, so the descent has to reach the
    # specs themselves. A grouped `import ( ... )` nests them one level deeper
    # behind the parenthesized list; stopping at the list yields one target for
    # the whole block, and gofmt groups imports by default.
    import_descend_nodes=("import_spec_list", "import_spec"),
    import_module_field="path",
    param_nodes=("parameter_declaration",),
    param_rest_nodes=("variadic_parameter_declaration",),
    param_name_field="name",
)

RUST: Final = LanguageSpec(
    name="rust",
    extensions=(".rs",),
    top_level_kinds={
        "function_item": "Fn",
        "struct_item": "Struct",
        "enum_item": "Enum",
        "trait_item": "Trait",
        "impl_item": "Impl",
        "mod_item": "Mod",
        "use_declaration": "Use",
        "const_item": "Const",
        "static_item": "Static",
        "type_item": "TypeAlias",
        "macro_definition": "MacroDef",
        "line_comment": "",
        "block_comment": "",
    },
    function_nodes=("function_item",),
    class_nodes=("struct_item", "enum_item", "trait_item"),
    branch_nodes=(
        "if_expression",
        "match_arm",
        "while_expression",
        "for_expression",
        "loop_expression",
    ),
    nesting_nodes=(
        "if_expression",
        "match_expression",
        "while_expression",
        "for_expression",
        "loop_expression",
    ),
    skip_subtree_nodes=("line_comment", "block_comment", "string_content"),
    return_type_field="return_type",
    member_object_field="value",
    member_property_field="field",
    call_nodes=("call_expression",),
    member_nodes=("field_expression",),
    # `self` is its own node kind, not an identifier.
    receiver_nodes=("identifier", "self"),
    import_nodes=("use_declaration",),
    # Rust is the one spec language whose grammar names the imported path.
    import_module_field="argument",
    param_nodes=("parameter",),
    param_name_field="pattern",
    self_names=frozenset({"self"}),
)

JAVA: Final = LanguageSpec(
    name="java",
    extensions=(".java",),
    top_level_kinds={
        "class_declaration": "ClassDeclaration",
        "interface_declaration": "InterfaceDeclaration",
        "enum_declaration": "EnumDeclaration",
        "record_declaration": "RecordDeclaration",
        "import_declaration": "ImportDeclaration",
        "package_declaration": "PackageDeclaration",
        "line_comment": "",
        "block_comment": "",
    },
    function_nodes=("method_declaration", "constructor_declaration"),
    class_nodes=(
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
    ),
    branch_nodes=(
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_block_statement_group",
        "catch_clause",
        "ternary_expression",
    ),
    nesting_nodes=(
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_expression",
        "try_statement",
    ),
    skip_subtree_nodes=("line_comment", "block_comment"),
    return_type_field="type",
    class_bases_field="superclass",
    # `method_invocation` carries the callee name and the receiver as its own
    # fields; there is no separate member node to unwrap.
    call_function_field="name",
    call_receiver_fields=("object",),
    call_nodes=("method_invocation",),
    # `this` is its own node kind, not an identifier.
    receiver_nodes=("identifier", "this"),
    import_nodes=("import_declaration",),
    param_nodes=("formal_parameter",),
    param_rest_nodes=("spread_parameter",),
    param_name_field="name",
    self_names=frozenset({"this"}),
)

CSHARP: Final = LanguageSpec(
    name="csharp",
    extensions=(".cs",),
    top_level_kinds={
        "class_declaration": "ClassDeclaration",
        "interface_declaration": "InterfaceDeclaration",
        "struct_declaration": "StructDeclaration",
        "enum_declaration": "EnumDeclaration",
        "record_declaration": "RecordDeclaration",
        "namespace_declaration": "NamespaceDeclaration",
        "using_directive": "UsingDirective",
        "comment": "",
    },
    function_nodes=("method_declaration", "constructor_declaration", "local_function_statement"),
    class_nodes=(
        "class_declaration",
        "interface_declaration",
        "struct_declaration",
        "record_declaration",
    ),
    branch_nodes=(
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "switch_section",
        "catch_clause",
        "conditional_expression",
    ),
    nesting_nodes=(
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
    ),
    return_type_field="type",
    member_object_field="expression",
    member_property_field="name",
    call_nodes=("invocation_expression",),
    member_nodes=("member_access_expression",),
    # `this` is its own node kind, not an identifier.
    receiver_nodes=("identifier", "this"),
    import_nodes=("using_directive",),
    param_nodes=("parameter",),
    param_name_field="name",
    self_names=frozenset({"this"}),
)

PHP: Final = LanguageSpec(
    name="php",
    extensions=(".php",),
    top_level_kinds={
        "function_definition": "FunctionDeclaration",
        "class_declaration": "ClassDeclaration",
        "interface_declaration": "InterfaceDeclaration",
        "trait_declaration": "TraitDeclaration",
        "enum_declaration": "EnumDeclaration",
        "namespace_definition": "NamespaceDefinition",
        "namespace_use_declaration": "UseDeclaration",
        "comment": "",
    },
    function_nodes=("function_definition", "method_declaration"),
    class_nodes=(
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "enum_declaration",
    ),
    branch_nodes=(
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "case_statement",
        "catch_clause",
        "conditional_expression",
    ),
    nesting_nodes=(
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
    ),
    skip_subtree_nodes=("comment", "string_content"),
    return_type_field="return_type",
    # The instance form names the receiver `object`, the static form `scope`.
    call_function_field="name",
    call_receiver_fields=("object", "scope"),
    # A plain `foo()` names its callee `function` instead, so without the second
    # field every receiverless call in the language goes unrecorded.
    bare_call_function_field="function",
    call_nodes=(
        "member_call_expression",
        "scoped_call_expression",
        "function_call_expression",
    ),
    # PHP spells an identifier `name`. A receiver is a variable (`$repo->x()`),
    # a class name (`Repo::x()`), or a relative scope (`self::x()`); a callee is
    # only ever a `name`, so `$fn()` and `$obj->{$m}()` stay unrecorded rather
    # than claiming a call to a function named after the variable holding it.
    callee_nodes=("name",),
    receiver_nodes=("name", "variable_name", "relative_scope"),
    import_nodes=("namespace_use_declaration",),
    # `use App\A, App\B;` is one statement holding a clause per module; the
    # clause is also what carries the module apart from an `as` alias.
    import_descend_nodes=("namespace_use_clause",),
    param_nodes=("simple_parameter",),
    param_rest_nodes=("variadic_parameter",),
    param_name_field="name",
    # `self::` and `static::` both name the class the call is written in, so
    # they resolve the same way `$this->` does.
    self_names=frozenset({"$this", "self", "static"}),
)

ALL: Final[tuple[LanguageSpec, ...]] = (GO, RUST, JAVA, CSHARP, PHP)

# language name -> the extensions it claims. Kept derived from the specs so a
# spec and the discovery glob can never disagree about what a language is.
EXTENSIONS_BY_LANGUAGE: Final[dict[str, tuple[str, ...]]] = {
    spec.name: spec.extensions for spec in ALL
}
