"""Principle generation for Smart Injection.

Principles are universal coding rules tailored to the repo's language and
conventions. Generated during bootstrap/refresh, fully regenerated each time.
Users who want custom per-repo rules use idioms.md via /chameleon-teach.
"""
from __future__ import annotations

# Universal principles (apply to any language)
_UNIVERSAL = [
    "Search the codebase for an existing utility, helper, or service before creating a new one.",
    "Match the testing granularity of sibling files - if similar classes have no test file, don't create one.",
    "Keep each class at the same abstraction level as its neighbors - don't over-extract small operations into separate files when siblings inline them.",
    "Separate data queries from side-effect operations - don't add file downloads to a JSON endpoint.",
    "Follow the codebase's existing pattern for resource lookup parameters (query params vs path segments vs body).",
    "Check if the codebase already wraps a library before importing it directly (custom HTTP client, logger, query hook).",
    "Inherit the same base class or mixin that sibling files use unless the new file has a fundamentally different responsibility.",
]

# Ruby-specific principles
_RUBY = [
    "Use find_or_initialize_by / find_or_create_by for upsert patterns instead of manual check-then-create.",
    "Mirror the error handling pattern of neighboring controllers - if they use render_data/render_error, don't use raw render json:.",
    "Use the codebase's DSL (scopes, validations, callbacks, before_action) instead of reimplementing in plain methods.",
]

# TypeScript-specific principles
_TYPESCRIPT = [
    "Use the project's query/mutation hook wrapper (useCustomQuery, queryOptions, etc.) instead of raw useQuery/useMutation.",
    "Mirror the error handling pattern of neighboring files - if they use a centralized error handler, don't add manual try/catch.",
    "Use the codebase's component declaration style (arrow + FC, observer(function), plain arrow) consistently.",
]


def generate_principles(*, language: str, conventions: dict) -> str:
    """Generate principles.md content tailored to the repo.

    Selects universal + language-specific principles and customizes
    based on what conventions were auto-derived (to avoid duplication).
    """
    lines: list[str] = []
    lines.append("# principles")
    lines.append("")
    lines.append("Auto-generated coding principles for this codebase.")
    lines.append("Regenerated on /chameleon-init and /chameleon-refresh.")
    lines.append("For custom per-repo rules, use /chameleon-teach (writes to idioms.md).")
    lines.append("")

    principles: list[str] = list(_UNIVERSAL)

    if language == "ruby":
        principles.extend(_RUBY)
    elif language == "typescript":
        principles.extend(_TYPESCRIPT)

    # Add convention-aware principles
    conv = conventions.get("conventions", {})

    # If inheritance conventions exist, reinforce with specifics
    for _arch, data in conv.get("inheritance", {}).items():
        base = data.get("dominant_base")
        if base:
            principles.append(
                f"This codebase's dominant base class for {_arch} is {base} - use it for new files in this archetype."
            )
            break  # one example is enough

    # If key_exports exist, make the search principle concrete
    all_exports: list[str] = []
    for _arch, names in conv.get("key_exports", {}).items():
        all_exports.extend(names[:3])
    if all_exports:
        sample = ", ".join(sorted(set(all_exports))[:8])
        principles.append(
            f"Existing utilities in this codebase include: {sample}. Check these before writing new ones."
        )

    for i, p in enumerate(principles, 1):
        lines.append(f"{i}. {p}")

    lines.append("")
    return "\n".join(lines)
