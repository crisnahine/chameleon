"""principles.py is language- and framework-aware.

The anti-hallucination protocol and principles adapt to the repo's actual
language (TS/JS, Ruby, Python) and detected framework (Rails, Django/DRF,
FastAPI, Flask, Next.js, NestJS): the right "don't invent a <X>" rule surfaces
for the stack in use, an unsupported language degrades to the universal core,
and the whole doc stays bounded.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.principles import generate_principles

EMPTY = {"conventions": {}}

# language -> a phrase unique to that language's anti-hallucination rule
_LANG_PROTOCOL_PHRASE = {
    "typescript": "type or interface field",
    "python": "keyword argument",
    "ruby": "open classes",
}
# language -> a phrase unique to that language's numbered principle
_LANG_PRINCIPLE_PHRASE = {
    "typescript": "export style",
    "python": "__all__",
    "ruby": "mixins",
}
# (language, framework) -> a phrase unique to that framework's rule
_FRAMEWORK_PHRASE = {
    ("ruby", "rails"): "route helper",
    ("python", "django"): "settings.py",
    ("python", "fastapi"): "Pydantic",
    ("python", "flask"): "blueprint",
    ("typescript", "nextjs"): "next.config",
    ("typescript", "nestjs"): "provider",
}


@pytest.mark.parametrize("lang,phrase", sorted(_LANG_PROTOCOL_PHRASE.items()))
def test_language_protocol_present_and_others_absent(lang, phrase):
    out = generate_principles(language=lang, conventions=EMPTY, archetypes={})
    assert phrase in out, f"{lang} protocol rule missing"
    for other, other_phrase in _LANG_PROTOCOL_PHRASE.items():
        if other != lang:
            assert other_phrase not in out, f"{lang} doc leaked {other} rule"


@pytest.mark.parametrize("lang,phrase", sorted(_LANG_PRINCIPLE_PHRASE.items()))
def test_language_principle_present(lang, phrase):
    out = generate_principles(language=lang, conventions=EMPTY, archetypes={})
    assert phrase in out, f"{lang} principle missing"


@pytest.mark.parametrize("pair,phrase", sorted(_FRAMEWORK_PHRASE.items()))
def test_framework_protocol_present_for_each_framework(pair, phrase):
    lang, fw = pair
    out = generate_principles(language=lang, framework=fw, conventions=EMPTY, archetypes={})
    assert phrase in out, f"{fw} framework rule missing"


def test_no_framework_no_framework_lines():
    out = generate_principles(language="ruby", conventions=EMPTY, archetypes={})
    for phrase in _FRAMEWORK_PHRASE.values():
        assert phrase not in out


def test_framework_none_is_handled():
    # the orchestrator passes None when no framework is detected
    out = generate_principles(language="python", framework=None, conventions=EMPTY, archetypes={})
    assert "## anti-hallucination protocol" in out


def test_unsupported_language_degrades_to_universal_core():
    out = generate_principles(language="go", conventions=EMPTY, archetypes={})
    assert "## anti-hallucination protocol" in out
    assert "Don't invent symbols" in out
    for phrase in _LANG_PROTOCOL_PHRASE.values():
        assert phrase not in out


def test_universal_dependency_line_always_present():
    for lang in ("typescript", "ruby", "python", "", "go"):
        out = generate_principles(language=lang, conventions=EMPTY, archetypes={})
        assert "manifest or lockfile" in out, f"dependency rule missing for {lang!r}"


def test_existing_universal_lines_preserved():
    out = generate_principles(language="ruby", conventions=EMPTY, archetypes={})
    assert "Don't invent symbols" in out
    assert "canonical witness" in out


def test_doc_stays_bounded_when_fully_loaded():
    conv = {
        "conventions": {
            "key_exports": {"service": ["formatDate"]},
            "inheritance": {
                "model": {
                    "dominant_base": "ApplicationRecord",
                    "known_bases": ["ApplicationRecord"],
                }
            },
            "imports": {"service": {"competing": [{"preferred": "x", "over": "y"}]}},
            "error_handling": {"controller": {"rescues": 5, "error_shape": "render_error"}},
        }
    }
    arch = {
        "archetypes": {
            "test_service": {},
            "controller": {"paths_pattern": "app/controllers"},
        }
    }
    out = generate_principles(language="ruby", framework="rails", conventions=conv, archetypes=arch)
    assert len(out) < 2600, f"principles doc too large: {len(out)} chars"
