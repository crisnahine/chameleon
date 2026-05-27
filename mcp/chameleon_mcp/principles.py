"""Principle generation for chameleon.

Principles are coding rules injected into every SessionStart. They cover
patterns that AST analysis and frequency counting can't detect: when to
reuse, when not to test, how to match the codebase's grain.

Three categories:
  - Universal: apply to any language, any codebase.
  - Ruby-specific: Rails/Sidekiq/ActiveRecord patterns.
  - TypeScript-specific: React/hooks/component patterns.

Generated during bootstrap and refresh. Tailored by language.
Custom per-repo rules go in idioms.md via /chameleon-teach.
"""
from __future__ import annotations

_UNIVERSAL = [
    "Check what exists before you build. Search for utilities, helpers, and services in the codebase before creating new ones.",
    "Match how your neighbors test. If sibling files in this directory have no specs, don't add one. If they do, follow the same shape.",
    "Stay at the same granularity as the directory. Don't extract a 5-line helper into its own file when neighbors inline similar logic.",
    "One endpoint, one job. Data queries return data. Downloads produce files. Don't combine both in a single action.",
    "Match the API shape. If existing endpoints use query params for IDs, don't introduce path segments. Consistency beats REST purity.",
    "Use the wrapper, not the raw library. If the codebase has a custom HTTP client, logger, or query hook, import that instead of the underlying package.",
    "Inherit what your siblings inherit. Use the same base class or mixin as other files in this directory unless your file does something fundamentally different.",
]

_RUBY = [
    "Use find_or_initialize_by for upserts. Don't write manual if-exists-then-update-else-create logic.",
    "Use render_data and render_error. Don't use raw render json: unless every sibling controller does.",
    "Use the DSL. Scopes, validations, callbacks, before_action - use the framework's macros, not hand-rolled equivalents.",
]

_TYPESCRIPT = [
    "Use the project's data-fetching wrapper. Don't import useQuery or useMutation directly if the codebase has a custom hook for it.",
    "Use the project's error handling. Don't add manual try/catch in components if there's a centralized error boundary or handler.",
    "Match the component style. Arrow with FC, observer(function), plain arrow - use whichever this codebase uses.",
]


def generate_principles(*, language: str) -> str:
    """Generate principles.md tailored to the repo's language."""
    parts: list[str] = []
    parts.append("# principles\n")

    principles: list[str] = list(_UNIVERSAL)
    if language == "ruby":
        principles.extend(_RUBY)
    elif language == "typescript":
        principles.extend(_TYPESCRIPT)

    for i, p in enumerate(principles, 1):
        parts.append(f"{i}. {p}")

    parts.append("")
    return "\n".join(parts)
