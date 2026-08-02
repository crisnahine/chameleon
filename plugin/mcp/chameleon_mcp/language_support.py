"""What each supported language actually gets, stated rather than implied.

Chameleon supports languages at two different depths, and the difference is
worth naming because the failure it prevents is specific. A language wired into
the extractor but not into the lint engine derives archetypes, conventions and
signatures perfectly well while every lint rule silently returns nothing. That
silence is indistinguishable from "your code is clean" -- the same
vacuous-silence class `violation_class.BLOCK_RULE_LANGUAGES` exists to prevent
at the rule level, one layer up.

So the tier is data here, and `/chameleon-doctor` and the support matrix read it
from this one place. A language is FIRST_CLASS or EXTRACTION, and asking either
question of a language chameleon has never heard of answers "unsupported"
rather than guessing.

Nothing here decides behavior. Every capability below was MEASURED against a
real bootstrapped repo, not inferred from which lists a language appears in; if
a capability lands later, this file changes with it or the honesty is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

FIRST_CLASS: Final[str] = "first-class"
EXTRACTION: Final[str] = "extraction"
UNSUPPORTED: Final[str] = "unsupported"


@dataclass(frozen=True)
class LanguageCapabilities:
    """What chameleon can and cannot do for one language."""

    language: str
    tier: str
    archetypes: bool
    conventions: bool
    signatures: bool
    imports: bool
    #: Call edges beyond the language-neutral `same_file` grade. The graded
    #: cross-file edges (`import`, `typed_property`, `constant_receiver`,
    #: `module_attribute`) each need a per-language resolver.
    graded_call_edges: bool
    #: The reverse/exports index behind query_symbol_importers and the
    #: removed-export existence checks.
    cross_file_index: bool
    #: The per-edit lint rules: naming, imports, inheritance, shape, test
    #: quality. Each needs a per-language signal source in lint_engine.
    lint_rules: bool
    #: Secret and eval-class detection. Gated on `lint_engine.security_language`,
    #: which is deliberately WIDER than `detect_language`: these two checks read
    #: content rather than a derived shape, so they need no per-language
    #: extractor and every source language gets them.
    security_lint: bool

    def missing(self) -> tuple[str, ...]:
        """The capabilities this language does NOT have, for an honest report."""
        absent: list[str] = []
        if not self.graded_call_edges:
            absent.append("graded cross-file call edges (same-file edges only)")
        if not self.cross_file_index:
            absent.append("reverse/exports index (query_symbol_importers, removed-export checks)")
        if not self.lint_rules:
            absent.append("per-edit lint rules")
        if not self.security_lint:
            absent.append("secret and eval detection")
        return tuple(absent)


def _first_class(language: str) -> LanguageCapabilities:
    return LanguageCapabilities(
        language=language,
        tier=FIRST_CLASS,
        archetypes=True,
        conventions=True,
        signatures=True,
        imports=True,
        graded_call_edges=True,
        # Ruby has no static export surface, so its cross-file view is the
        # constant index rather than the reverse index -- a different mechanism
        # for the same question, which is why it still counts here.
        cross_file_index=True,
        lint_rules=True,
        security_lint=True,
    )


def _extraction(language: str) -> LanguageCapabilities:
    return LanguageCapabilities(
        language=language,
        tier=EXTRACTION,
        archetypes=True,
        conventions=True,
        signatures=True,
        imports=True,
        graded_call_edges=False,
        cross_file_index=False,
        lint_rules=False,
        # TRUE, and the asymmetry is the point: the secret and eval-sink scans
        # read content rather than a derived shape, so they need no per-language
        # extractor. Gating them on the narrow `detect_language` exempted every
        # extraction-tier language from the two checks that are not style
        # opinions -- an AWS key in a `.go` file was advisory where the identical
        # key in a `.rb` file was block-eligible.
        security_lint=True,
    )


_CAPABILITIES: Final[dict[str, LanguageCapabilities]] = {
    language: _first_class(language) for language in ("typescript", "ruby", "python")
}


def _register_spec_languages() -> None:
    """Every spec-driven language, at the extraction tier.

    Derived from the specs so a language cannot be added to the extractor and
    forgotten here -- which would put it back to being silently half-supported,
    the exact thing this module exists to prevent.
    """
    try:
        from chameleon_mcp.extractors.treesitter.lang.specs import ALL

        for spec in ALL:
            _CAPABILITIES.setdefault(spec.name, _extraction(spec.name))
    except Exception:
        # A broken spec costs its own languages; the three first-class ones are
        # declared above and unaffected.
        pass


_register_spec_languages()


def capabilities_for(language: str | None) -> LanguageCapabilities | None:
    """What `language` gets, or None when chameleon does not support it."""
    if not language:
        return None
    return _CAPABILITIES.get(language)


def tier_for(language: str | None) -> str:
    """`first-class`, `extraction`, or `unsupported`."""
    found = capabilities_for(language)
    return found.tier if found is not None else UNSUPPORTED


def supported_languages() -> tuple[str, ...]:
    """Every language chameleon supports, first-class ones first."""
    ordered = sorted(
        _CAPABILITIES.values(),
        key=lambda c: (0 if c.tier == FIRST_CLASS else 1, c.language),
    )
    return tuple(c.language for c in ordered)


def describe(language: str | None) -> str:
    """One line stating what this language gets, for doctor and status.

    Written to be read by someone wondering why a rule never fired.
    """
    found = capabilities_for(language)
    if found is None:
        return (
            f"{language or 'unknown'}: not supported -- no profile is derived, "
            "and no guidance is injected"
        )
    if found.tier == FIRST_CLASS:
        return f"{found.language}: first-class -- derivation, lint rules and cross-file checks"
    absent = "; no ".join(found.missing())
    return (
        f"{found.language}: extraction tier -- archetypes, conventions, signatures and "
        f"imports are derived, but no {absent}"
    )
