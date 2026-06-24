"""Plain-word labels for AST node kinds shown in user-facing text.

Both the Tier 1 pointer summary (bootstrap/orchestrator) and the lint
violation messages (lint_engine) surface these kinds to the user. "imports,
declarations" reads; "ImportDeclaration, FirstStatement" is parser jargon, and
TS-flavoured names like "default export" leak into Ruby/Python text where the
construct does not exist. Sharing one humanizer keeps both surfaces honest.
"""

_KIND_LABELS: dict[str, str] = {
    "ImportDeclaration": "imports",
    "ExportDeclaration": "exports",
    "ExportNamedDeclaration": "exports",
    "ExportAssignment": "default export",
    "FunctionDeclaration": "functions",
    "ClassDeclaration": "classes",
    "InterfaceDeclaration": "interfaces",
    "TypeAliasDeclaration": "type aliases",
    "EnumDeclaration": "enums",
    "VariableStatement": "declarations",
    "FirstStatement": "declarations",
    "CodeDeclaration": "declarations",
    "ExpressionStatement": "statements",
    "ClassNode": "classes",
    "ModuleNode": "modules",
    "DefNode": "methods",
    "CallNode": "method calls",
    "ConstantWriteNode": "constant assignments",
    "LocalVariableWriteNode": "assignments",
    # The lint engine normalizes an uncategorized DSL macro to a bare "DslCall"
    # and any mixin to "IncludeCall"; both reach user text without the colon
    # that the DslCall: prefix branch keys on, so map them explicitly.
    "DslCall": "DSL calls",
    "IncludeCall": "includes",
}


def humanize_kind(kind: str) -> str:
    if kind.startswith("DslCall:"):
        return kind.split(":", 1)[1] + " calls"
    return _KIND_LABELS.get(kind, kind)
