from chameleon_mcp.idiom_coverage import _class_contract, _covered_reasons


def _artifacts(conv: dict) -> dict:
    return {"conventions": conv, "rules": {}}


def _conv() -> dict:
    return {
        "inheritance": {
            "interaction": {
                "dominant_base": "ActiveInteraction::Base",
                "known_bases": ["ActiveInteraction::Base"],
            }
        },
        "class_contract": {
            "interaction": {
                "dsl_macros": ["string", "integer", "object"],
                "required_methods": ["execute"],
                "base": "ActiveInteraction::Base",
            }
        },
    }


def test_pure_inheritance_idiom_still_suppressed():
    cand = {
        "archetype": "interaction",
        "rationale": "Interactions must inherit from ActiveInteraction::Base.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_conv()), [])
    assert any(r.startswith("covered-by-inheritance") for r in reasons)


def test_contract_idiom_not_suppressed():
    cand = {
        "archetype": "interaction",
        "rationale": (
            "Interactions inherit from ActiveInteraction::Base and declare typed "
            "filters with string/integer/object, then define execute."
        ),
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_conv()), [])
    assert not any(r.startswith("covered-by-inheritance") for r in reasons)


def test_class_contract_helper_shape():
    out = _class_contract(_conv())
    assert out["interaction"]["required_methods"] == ["execute"]
    assert out["interaction"]["base"] == "ActiveInteraction::Base"
