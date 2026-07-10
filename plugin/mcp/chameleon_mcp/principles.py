"""Principle generation for chameleon.

Auto-generates coding principles from the repo's actual data. Only emits
principles that ADD information beyond what the structured convention
sections (IMPORTS, NAMING, INHERITANCE, PATTERNS, REUSE) already cover.

Each principle is gated on whether the repo has the relevant pattern, plus a
language- and framework-aware layer: the anti-hallucination protocol carries the
"don't invent a <X>" rule that fits the repo's actual stack (TS/JS, Ruby, Python
and Rails / Django / DRF / FastAPI / Flask / Next.js / NestJS), so the guidance
names the right place to verify a symbol instead of staying generic.

Token budget: under ~450 tokens for any repo. Only ONE language and ONE framework
ever apply, so the stack-specific lines add ~3 bullets, not a dump of every
language.
"""

from __future__ import annotations

# The repo's primary language (extractor.language) -> a numbered principle that
# adds a language-level reuse/structure rule the convention sections don't carry.
_LANGUAGE_PRINCIPLE: dict[str, str] = {
    "typescript": (
        "Honor each module's export style (named vs default) and the repo's "
        "import-path convention (alias vs relative); don't reshape an import "
        "siblings write one way."
    ),
    "python": (
        "Import a name from where the package exposes it (its `__init__` / "
        "`__all__` surface), not from a module's deep definition; respect the "
        "public API the package presents."
    ),
    "ruby": (
        "Reach for the repo's existing mixins, concerns, and helpers before "
        "re-implementing shared behavior by hand."
    ),
}

# Language -> the anti-hallucination rule that names where THIS language's
# fabrications hide (a field, a kwarg, a method). Keyed on extractor.language.
_LANGUAGE_PROTOCOL: dict[str, str] = {
    "typescript": (
        "Don't invent a type or interface field, a prop, an enum member, or a "
        "default export; read the type and the module's export style first. A "
        "property absent from the type does not exist."
    ),
    "python": (
        "Don't invent a keyword argument, an attribute, or an import path; read "
        "the class or module before calling it. A method or kwarg absent from "
        "the definition does not exist."
    ),
    "ruby": (
        "Don't invent a method, association, scope, or constant; Ruby's open "
        "classes make a typo look plausible, so confirm the name resolves "
        "before calling it."
    ),
}

# Detected framework family (orchestrator._classify_framework) -> the rule that
# names where THIS framework's fabrications hide. DRF folds into django.
_FRAMEWORK_PROTOCOL: dict[str, str] = {
    "rails": (
        "Don't invent a route helper, association, scope, validation, or "
        "callback; check `config/routes.rb`, the model, and shared concerns "
        "before using one."
    ),
    "django": (
        "Don't invent a model field, a manager/queryset method, or a settings "
        "key; check the model, its manager, and `settings.py` first. A DRF "
        "serializer field or permission class is no different - read the "
        "serializer and the viewset."
    ),
    "fastapi": (
        "Don't invent a dependency, a path operation, or a Pydantic model "
        "field; check the router and the schema model before referencing them."
    ),
    "flask": (
        "Don't invent a route, a blueprint, or an extension method; check the "
        "app factory and the blueprint registration first."
    ),
    "nextjs": (
        "Don't invent a Next.js data-fetching export, a route segment, or a "
        "config option; check the app/pages structure and `next.config` before "
        "using one."
    ),
    "nestjs": (
        "Don't invent a provider, a module import, or a decorator; check the "
        "module's providers and imports before wiring one."
    ),
}


def generate_principles(
    *,
    language: str | None = "",
    framework: str | None = "",
    conventions: dict | None = None,
    archetypes: dict | None = None,
) -> str:
    """Generate principles.md tailored to the repo's actual structure + stack.

    ``language`` is the extractor's language (``typescript`` / ``ruby`` /
    ``python``); ``framework`` is the discrete family from
    ``_classify_framework`` (``rails`` / ``django`` / ``fastapi`` / ``flask`` /
    ``nextjs`` / ``nestjs``) or None. Both are best-effort: an unknown or empty
    value simply emits no stack-specific line, never an error, so the doc always
    carries at least the universal core.
    """
    conventions = conventions or {}
    archetypes = archetypes or {}
    conv = conventions.get("conventions", {})
    arch_data = archetypes.get("archetypes", {})
    lang = (language or "").strip().lower()
    fw = (framework or "").strip().lower()

    principles: list[str] = []

    principles.append(
        "The conventions and code patterns shown here are extracted from this codebase. They override general best practices."
    )

    principles.append("Match directory granularity; don't extract what siblings inline.")

    principles.append(
        "Match the surrounding code's altitude: solve the problem at the level "
        "siblings do, and don't introduce an abstraction, wrapper, or layer they "
        "handle inline."
    )

    has_test_archs = any(name.startswith("test") for name in arch_data)
    if has_test_archs:
        principles.append("Match sibling test shape; skip tests where siblings have none.")

    has_api = any(
        "controller" in (body.get("paths_pattern") or "")
        or "routes" in (body.get("paths_pattern") or "")
        for body in arch_data.values()
    )
    if has_api:
        principles.append(
            "One action, one job: queries return data, downloads produce files. Match the API shape of sibling endpoints."
        )

    has_competing = any(data.get("competing") for data in conv.get("imports", {}).values())
    if has_competing:
        principles.append("Use the project's wrapper, not the raw library.")

    # Error-handling contract: the extractor only stores an entry once it clears
    # the frequency floor, so presence here means the archetype's files uniformly
    # handle errors one way. Naming the dominant shape gives the model a concrete
    # target to match instead of free-text "check the witness" prose.
    error_handling = conv.get("error_handling", {})
    if isinstance(error_handling, dict):
        for data in error_handling.values():
            if not isinstance(data, dict):
                continue
            if "rescues" in data:
                shape = data.get("error_shape")
                tail = f" ({shape})" if isinstance(shape, str) and shape else ""
                principles.append(
                    "Controllers rescue at the base and render the project error "
                    f"shape{tail}; match it instead of letting an action raise."
                )
                break
            if "try_catch" in data:
                principles.append(
                    "Wrap work that can fail in try/catch the way siblings do; "
                    "don't leave a rejection unhandled."
                )
                break

    lang_principle = _LANGUAGE_PRINCIPLE.get(lang)
    if lang_principle:
        principles.append(lang_principle)

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
    # Universal: a hallucinated dependency is the most language-agnostic failure.
    protocol.append(
        "Don't add a dependency the repo doesn't already use; if an import isn't "
        "in the manifest or lockfile, it isn't available - reach for what is "
        "installed."
    )
    # Stack-specific: name where THIS language's and framework's fabrications hide.
    lang_protocol = _LANGUAGE_PROTOCOL.get(lang)
    if lang_protocol:
        protocol.append(lang_protocol)
    framework_protocol = _FRAMEWORK_PROTOCOL.get(fw)
    if framework_protocol:
        protocol.append(framework_protocol)
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
