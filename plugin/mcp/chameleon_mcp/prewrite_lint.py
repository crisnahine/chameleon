"""Pre-write banned-import detection for the PreToolUse deny path.

Only inspects import statements in the PROPOSED content (Edit new_string /
Write content). Never inspects structure (a fragment is not a whole file).
Reuses the string/comment-stripping lexer so imports embedded in literals are
ignored. Returns violation dicts shaped like lint_engine.Violation.to_dict().
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import lint_conventions

_RULE = "import-preference-violation"


def banned_imports_in_content(
    content: str, *, language: str, archetype: str, conventions: dict
) -> list[dict]:
    if not archetype:
        return []
    arch_imports = (conventions.get("imports") or {}).get(archetype) or {}
    if not arch_imports.get("competing"):
        return []
    arch_conv = {"imports": arch_imports}
    # lint_conventions blanks string-embedded imports for TypeScript itself, so
    # the PreToolUse deny path and the PostToolUse / lint_file path agree on
    # whether a competing import inside a string literal is a real import.
    try:
        out = lint_conventions(content, arch_conv, language=language)
    except Exception:
        return []
    return [v.to_dict() for v in out if v.rule == _RULE]
