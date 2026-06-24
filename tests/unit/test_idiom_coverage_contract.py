from chameleon_mcp.idiom_coverage import (
    _class_contract,
    _covered_reasons,
    _naming_prefixes,
)


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


def _import_conv() -> dict:
    return {
        "imports": {
            "component": {
                "preferred": [
                    {"module": "react", "frequency": 90, "total": 100},
                    {"module": "@app/date", "frequency": 40, "total": 100},
                ]
            }
        }
    }


def test_preferred_import_restating_idiom_is_covered():
    cand = {
        "archetype": "component",
        "rationale": "Prefer the @app/date wrapper for date helpers, never reach for moment.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_import_conv()), [])
    assert any(r.startswith("covered-by-preferred-import") for r in reasons)


def test_module_mention_without_import_prescription_stays_novel():
    # 'react' is a high-frequency preferred import and a literal substring of the
    # rationale, but the idiom prescribes nothing about importing it — the intent
    # gate must keep it novel (no naive-substring false positive).
    cand = {
        "archetype": "component",
        "rationale": "Memoize expensive React subtrees so re-renders stay cheap.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_import_conv()), [])
    assert not any(r.startswith("covered-by-preferred-import") for r in reasons)


def test_prefer_plus_bare_framework_name_stays_novel():
    # The bare-"prefer" tier must NOT fire on a perf idiom that merely says
    # "prefer ... React": a bare framework name needs explicit import vocabulary
    # before "prefer" counts as a restatement of the import convention.
    cand = {
        "archetype": "component",
        "rationale": "Prefer memoizing expensive React subtrees so re-renders stay cheap.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_import_conv()), [])
    assert not any(r.startswith("covered-by-preferred-import") for r in reasons)


def test_prefer_plus_scoped_module_common_segment_stays_novel():
    # A scoped module collapses to a common last segment ("@app/date" -> "date").
    # The bare-"prefer" tier must require the VERBATIM scoped name, so an idiom
    # that only mentions the segment ("prefer storing the date in UTC") is novel,
    # not a restatement of the import convention.
    cand = {
        "archetype": "component",
        "rationale": "Prefer storing the date in UTC, never local time.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_import_conv()), [])
    assert not any(r.startswith("covered-by-preferred-import") for r in reasons)


def test_import_verb_covers_bare_framework_name():
    # An explicit import verb DOES prescribe a bare framework name.
    cand = {
        "archetype": "component",
        "rationale": "Always import hooks from react, never from the legacy shim.",
    }
    text = cand["rationale"].lower()
    reasons = _covered_reasons(cand, text, frozenset(), _artifacts(_import_conv()), [])
    assert any(r.startswith("covered-by-preferred-import") for r in reasons)


def test_naming_prefixes_reader_surfaces_patterns():
    conv = {
        "naming": {
            "model": {
                "interface_prefix": {"pattern": "I", "consistency": 0.9},
                "file_naming": {"casing": "snake_case"},
            }
        }
    }
    out = _naming_prefixes(conv)
    assert out == {"model": {"interface": "I"}}
