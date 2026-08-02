"""The language-capability registry: what each language is declared to get.

The registry exists to stop one failure: a language wired into the extractor but
not into the lint engine derives archetypes and conventions perfectly well while
every rule silently returns nothing, and that silence reads as "your code is
clean". These tests pin the declaration against the wiring, so a language cannot
be added to one and forgotten in the other.
"""

from __future__ import annotations

from chameleon_mcp import language_support as ls


def test_the_three_first_class_languages_are_declared_first_class():
    for language in ("typescript", "ruby", "python"):
        caps = ls.capabilities_for(language)
        assert caps is not None
        assert caps.tier == ls.FIRST_CLASS
        assert caps.lint_rules and caps.security_lint and caps.cross_file_index
        assert caps.missing() == (), f"{language} should lack nothing"


def test_every_spec_driven_language_is_declared_extraction_tier():
    """A spec-driven language must appear here, or it is silently
    half-supported -- the exact state this module exists to make visible."""
    from chameleon_mcp.extractors.treesitter.lang.specs import ALL

    for spec in ALL:
        caps = ls.capabilities_for(spec.name)
        assert caps is not None, f"{spec.name} has a spec but no declared capabilities"
        assert caps.tier == ls.EXTRACTION
        # What it DOES get is what a profile is built from.
        assert caps.archetypes and caps.conventions and caps.signatures and caps.imports
        # What it does NOT get must be named, not implied.
        assert caps.missing(), f"{spec.name} claims to lack nothing but is extraction tier"


def test_the_declaration_matches_the_actual_lint_wiring():
    """The registry must agree with lint_engine, not merely assert.

    `detect_language` returning None is what makes every rule silent for a
    language; a language declared to have lint rules while the engine refuses to
    classify its files would be a lie the registry exists to prevent.
    """
    from chameleon_mcp.lint_engine import detect_language

    probe = {
        "typescript": "a.ts",
        "ruby": "a.rb",
        "python": "a.py",
        "go": "a.go",
        "rust": "a.rs",
        "java": "a.java",
        "csharp": "a.cs",
        "php": "a.php",
    }
    for language, filename in probe.items():
        caps = ls.capabilities_for(language)
        assert caps is not None, language
        classified = detect_language(filename) is not None
        assert caps.lint_rules == classified, (
            f"{language}: registry says lint_rules={caps.lint_rules} but "
            f"detect_language({filename!r}) classified={classified}"
        )


def test_the_declaration_matches_the_cross_file_index_wiring():
    from chameleon_mcp.symbol_index import REVERSE_INDEXED_LANGUAGES

    for language in ls.supported_languages():
        caps = ls.capabilities_for(language)
        assert caps is not None
        if caps.tier == ls.EXTRACTION:
            assert language not in REVERSE_INDEXED_LANGUAGES, (
                f"{language} is declared extraction tier but builds a reverse index"
            )


def test_an_unsupported_language_says_so_rather_than_guessing():
    assert ls.tier_for("zig") == ls.UNSUPPORTED
    assert ls.tier_for(None) == ls.UNSUPPORTED
    assert ls.tier_for("") == ls.UNSUPPORTED
    assert ls.capabilities_for("zig") is None
    assert "not supported" in ls.describe("zig")


def test_describe_names_what_is_missing_for_an_extraction_language():
    """A user wondering why a rule never fired must be able to read the answer,
    not infer it from silence."""
    text = ls.describe("go")
    assert "extraction tier" in text
    assert "lint rules" in text
    assert "secret" in text


def test_first_class_languages_are_listed_before_extraction_ones():
    listed = ls.supported_languages()
    tiers = [ls.tier_for(language) for language in listed]
    assert tiers == sorted(tiers, key=lambda t: 0 if t == ls.FIRST_CLASS else 1)
    assert set(listed) >= {"typescript", "ruby", "python", "go", "rust", "java", "csharp", "php"}
