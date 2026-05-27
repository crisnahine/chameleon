"""Principle generation for chameleon.

Auto-generates coding principles from the repo's actual data. Only emits
principles that ADD information beyond what the structured convention
sections (IMPORTS, NAMING, INHERITANCE, PATTERNS, REUSE) already cover.

Each principle is gated on whether the repo has the relevant pattern.
Token budget: under 200 tokens for any repo.
"""
from __future__ import annotations


def generate_principles(
    *,
    language: str,
    conventions: dict | None = None,
    archetypes: dict | None = None,
) -> str:
    """Generate principles.md tailored to the repo's actual structure."""
    conventions = conventions or {}
    archetypes = archetypes or {}
    conv = conventions.get("conventions", {})
    arch_data = archetypes.get("archetypes", {})

    principles: list[str] = []

    # Always: granularity matching (no convention section covers this)
    principles.append(
        "Match directory granularity; don't extract what siblings inline."
    )

    # Gate: test archetypes exist → tell Claude when NOT to test
    has_test_archs = any(
        name.startswith("test") for name in arch_data
    )
    if has_test_archs:
        principles.append(
            "Match sibling test shape; skip tests where siblings have none."
        )

    # Gate: controller/API archetypes → endpoint discipline
    has_api = any(
        "controller" in (body.get("paths_pattern") or "")
        or "routes" in (body.get("paths_pattern") or "")
        for body in arch_data.values()
    )
    if has_api:
        principles.append(
            "Match endpoint style and response patterns of sibling controllers."
        )

    # Gate: no competing imports detected → general wrapper principle
    has_competing = any(
        data.get("competing")
        for data in conv.get("imports", {}).values()
    )
    if not has_competing:
        principles.append(
            "Use the project's wrapper, not the raw library."
        )

    # Gate: Ruby → built-in upsert idioms
    if language == "ruby":
        principles.append(
            "Prefer built-in idioms (find_or_create_by, find_or_initialize_by) over manual check-then-create."
        )

    parts: list[str] = ["# principles\n"]
    for i, p in enumerate(principles, 1):
        parts.append(f"{i}. {p}")
    parts.append("")
    return "\n".join(parts)
