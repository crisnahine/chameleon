"""PKG-11: idiom-coverage dedup for non-Ruby class-based languages.

An idiom that merely restates an auto-derived + surfaced convention is not novel.
For Python/TypeScript the base lives on class_contract (TS has no inheritance
section), and the contract's decorators / required methods / DSL macros are
surfaced in the SessionStart block -- so an idiom naming them is covered.
"""

from __future__ import annotations

from chameleon_mcp.idiom_coverage import _covered_reasons


def _arts(conventions: dict, language: str = "python") -> dict:
    return {"conventions": conventions, "rules": {}, "language": language}


def _reasons(text: str, conventions: dict, *, archetype: str, language: str = "python"):
    cand = {"archetype": archetype, "rationale": ""}
    tl = text.lower()
    return _covered_reasons(cand, tl, frozenset(tl.split()), _arts(conventions, language), [])


def test_covered_by_inheritance_via_class_contract_base():
    # No inheritance section -- the base is recorded only on class_contract,
    # the TS/Python shape. A "inherit from <base>" idiom is still covered.
    conv = {
        "class_contract": {
            "model": {"base": "models.Model", "decorators": [], "required_methods": []}
        }
    }
    reasons = _reasons("models inherit from models.Model", conv, archetype="model")
    assert any(r == "covered-by-inheritance:model" for r in reasons)


def test_covered_by_class_contract_required_method():
    # No inheritance phrase; the idiom restates a derived required method.
    conv = {
        "class_contract": {
            "view": {"base": None, "decorators": [], "required_methods": ["get_queryset"]}
        }
    }
    reasons = _reasons("every view must implement get_queryset", conv, archetype="view")
    assert any(r == "covered-by-class-contract:view" for r in reasons)


def test_covered_by_class_contract_decorator():
    conv = {
        "class_contract": {
            "view": {"base": None, "decorators": ["login_required"], "required_methods": []}
        }
    }
    reasons = _reasons("decorate views with login_required", conv, archetype="view")
    assert any(r == "covered-by-class-contract:view" for r in reasons)


def test_ruby_contract_idiom_stays_novel():
    # Ruby deliberately keeps a contract-naming idiom novel (its DSL nuance is
    # worth an explicit note); the non-Ruby dedup must not fire for it.
    conv = {
        "class_contract": {
            "interaction": {"base": "ActiveInteraction::Base", "required_methods": ["execute"]}
        }
    }
    reasons = _reasons(
        "interactions must implement execute", conv, archetype="interaction", language="ruby"
    )
    assert not any("class-contract" in r for r in reasons)


def test_unrelated_idiom_stays_novel():
    conv = {
        "class_contract": {
            "view": {"base": "APIView", "decorators": ["login_required"], "required_methods": []}
        }
    }
    reasons = _reasons("prefer composition over deep inheritance trees", conv, archetype="view")
    assert not any("inheritance" in r or "class-contract" in r for r in reasons)


def test_short_token_does_not_false_dedupe():
    # A 2-char required method must not dedupe an idiom that merely contains it.
    conv = {"class_contract": {"model": {"base": None, "required_methods": ["id"]}}}
    reasons = _reasons("models should expose a stable id field", conv, archetype="model")
    assert not any("class-contract" in r for r in reasons)
