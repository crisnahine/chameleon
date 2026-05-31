"""Principle generation for chameleon.

Auto-generates coding principles from the repo's actual data. Only emits
principles that ADD information beyond what the structured convention
sections (IMPORTS, NAMING, INHERITANCE, PATTERNS, REUSE) already cover.

Each principle is gated on whether the repo has the relevant pattern.
Token budget: under ~300 tokens for any repo (the always-on
anti-hallucination protocol adds ~60-90 over the numbered principles).
"""
from __future__ import annotations


def generate_principles(
    *,
    language: str = "",  # noqa: ARG001  # reserved for future use
    conventions: dict | None = None,
    archetypes: dict | None = None,
) -> str:
    """Generate principles.md tailored to the repo's actual structure."""
    conventions = conventions or {}
    archetypes = archetypes or {}
    conv = conventions.get("conventions", {})
    arch_data = archetypes.get("archetypes", {})

    principles: list[str] = []

    principles.append(
        "The conventions and code patterns shown here are extracted from this codebase. They override general best practices."
    )

    principles.append(
        "Match directory granularity; don't extract what siblings inline."
    )

    has_test_archs = any(
        name.startswith("test") for name in arch_data
    )
    if has_test_archs:
        principles.append(
            "Match sibling test shape; skip tests where siblings have none."
        )

    has_api = any(
        "controller" in (body.get("paths_pattern") or "")
        or "routes" in (body.get("paths_pattern") or "")
        for body in arch_data.values()
    )
    if has_api:
        principles.append(
            "One action, one job: queries return data, downloads produce files. Match the API shape of sibling endpoints."
        )

    has_competing = any(
        data.get("competing")
        for data in conv.get("imports", {}).values()
    )
    if has_competing:
        principles.append(
            "Use the project's wrapper, not the raw library."
        )

    principles.append(
        "Prefer the language's built-in idiom for upserts, lookups, and defaults over manual check-then-create."
    )

    parts: list[str] = ["# principles\n"]
    for i, p in enumerate(principles, 1):
        parts.append(f"{i}. {p}")
    parts.append("")

    # Anti-hallucination protocol: always-on universal core + data-gated lines.
    # Rendered as a markdown section with "- " bullets so the numbered-principle
    # parsers in conventions.py (which only read digit-leading lines) skip it;
    # format_conventions_for_session surfaces it under its own header.
    protocol: list[str] = [
        "Don't invent symbols, imports, file paths, config keys, or APIs. "
        "If you're not certain something exists, grep or read it before using it.",
        "Match the canonical witness's real shape; don't fabricate fields, "
        "options, or structure it doesn't show.",
    ]
    if conv.get("key_exports"):
        protocol.append(
            "This archetype's real exports are listed under 'Check before "
            "creating' - reuse those before adding a new one; a name absent "
            "from the injected context may not exist."
        )
    if any(
        isinstance(data, dict) and (data.get("known_bases") or data.get("dominant_base"))
        for data in conv.get("inheritance", {}).values()
    ):
        protocol.append(
            "Inherit only from bases this repo already uses; a base class not "
            "in the listed set is probably wrong."
        )

    parts.append("## anti-hallucination protocol\n")
    for line in protocol:
        parts.append(f"- {line}")
    parts.append("")
    return "\n".join(parts)
