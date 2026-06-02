"""Pre-write banned-import detection for the PreToolUse deny path.

Only inspects import statements in the PROPOSED content (Edit new_string /
Write content). Never inspects structure (a fragment is not a whole file).
Reuses the string/comment-stripping lexer so imports embedded in literals are
ignored. Returns violation dicts shaped like lint_engine.Violation.to_dict().
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import _TS_IMPORT_FROM_RE, lint_conventions
from chameleon_mcp.phantom_imports import _strip_ts_noise

_RULE = "import-preference-violation"


def _blank_string_embedded_imports(content: str) -> str:
    """Blank out `import ... from ...` runs that live entirely inside a string
    literal, so a code snippet stored as a string value is not mistaken for a
    real import.

    `lint_conventions` runs its import scan on raw content (it needs the literal
    module specifier, which the strip helper blanks). A real import keeps its
    `import`/`from` keywords in unmasked code while only the quoted module name
    is masked. A string-embedded fake has the `import` keyword itself masked.
    We blank only matches whose `import` keyword is masked, leaving real imports
    (and their module specifiers) intact for the downstream scan.
    """
    stripped, mask = _strip_ts_noise(content)
    if not any(mask):
        return content
    chars = list(content)
    for m in _TS_IMPORT_FROM_RE.finditer(stripped):
        start = m.start()
        if start < len(mask) and mask[start]:
            for i in range(m.start(), m.end()):
                if i < len(chars) and chars[i] != "\n":
                    chars[i] = " "
    return "".join(chars)


def banned_imports_in_content(
    content: str, *, language: str, archetype: str, conventions: dict
) -> list[dict]:
    if not archetype:
        return []
    arch_imports = (conventions.get("imports") or {}).get(archetype) or {}
    if not arch_imports.get("competing"):
        return []
    arch_conv = {"imports": arch_imports}
    scan = content
    if language == "typescript":
        try:
            scan = _blank_string_embedded_imports(content)
        except Exception:
            scan = content
    try:
        out = lint_conventions(scan, arch_conv, language=language)
    except Exception:
        return []
    return [v.to_dict() for v in out if v.rule == _RULE]
