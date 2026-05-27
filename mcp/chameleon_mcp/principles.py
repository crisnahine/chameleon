"""Principle generation for chameleon.

Principles are coding rules injected into every SessionStart. They cover
the patterns that AST analysis and frequency counting can't detect: when
to reuse, when not to test, how to match the codebase's grain.

All principles are language-agnostic. Language-specific and framework-specific
patterns are handled by the conventions system (conventions.json) and
per-repo idioms (idioms.md via /chameleon-teach).
"""
from __future__ import annotations

PRINCIPLES = [
    "Check what exists before you build. Search for utilities, helpers, and services in the codebase before creating new ones.",
    "Match how your neighbors test. If sibling files have no tests, don't add one. If they do, follow the same shape and framework.",
    "Stay at the same granularity as the directory. Don't extract a small helper into its own file when neighbors inline similar logic.",
    "One endpoint, one job. Data queries return data. Downloads produce files. Side effects mutate state. Don't combine these in a single action.",
    "Match the API shape. If existing endpoints use query params for IDs, don't introduce path segments. Consistency beats convention purity.",
    "Use the wrapper, not the raw library. If the codebase wraps a library with a custom client, hook, or helper, import that instead of the underlying package.",
    "Inherit what your siblings inherit. Use the same base class, mixin, or interface as other files in this directory.",
    "Use the language's built-in idiom for upserts, lookups, and defaults. Don't write manual check-then-create when a one-liner exists.",
    "Use the project's response pattern. If sibling files use a response wrapper, error helper, or result type, use the same one.",
    "Use the framework's declarative API. If macros, decorators, annotations, or DSLs exist for a pattern, use them instead of hand-rolling the behavior.",
]


def generate_principles(*, language: str) -> str:  # noqa: ARG001
    """Generate principles.md. Language param reserved for future use."""
    parts: list[str] = ["# principles\n"]
    for i, p in enumerate(PRINCIPLES, 1):
        parts.append(f"{i}. {p}")
    parts.append("")
    return "\n".join(parts)
