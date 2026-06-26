"""A malformed value in one convention section must not wipe the whole block.

``format_conventions_for_session`` renders each section (naming, inheritance,
method_calls, key_exports, ...) in its own loop. The imports / required_guards /
class_contract loops guard each per-archetype value with ``isinstance(data,
dict)``, but the naming / inheritance / method_calls / key_exports loops did
not, so a single non-dict value (from a corrupt / hand-edited / 3-way-merged
conventions.json) raised out of the whole function. The caller catches that
blanket and drops the ENTIRE injected conventions block -- well-formed sections
included. Each loop must skip a malformed entry and keep rendering the rest.
"""

from __future__ import annotations

from chameleon_mcp.conventions import format_conventions_for_session


def _wrap(sections: dict) -> dict:
    # format_conventions_for_session reads conventions["conventions"].
    return {"conventions": sections}


def test_malformed_naming_section_does_not_wipe_other_sections() -> None:
    conv = _wrap(
        {
            "naming": {"BadArch": "should-be-a-dict-but-is-a-string"},
            "inheritance": {
                "Controller": {"dominant_base": "ApplicationController", "frequency": 0.97}
            },
            "key_exports": {"Service": ["fooSchema", "barSchema"]},
        }
    )
    out = format_conventions_for_session(conv)
    # The well-formed inheritance + key_exports sections must still render.
    assert "ApplicationController" in out
    assert "fooSchema" in out


def test_malformed_inheritance_section_does_not_wipe_other_sections() -> None:
    conv = _wrap(
        {
            "inheritance": {"BadArch": ["not", "a", "dict"]},
            "key_exports": {"Service": ["fooSchema"]},
        }
    )
    out = format_conventions_for_session(conv)
    assert "fooSchema" in out


def test_malformed_method_calls_section_does_not_wipe_other_sections() -> None:
    conv = _wrap(
        {
            "method_calls": {"BadArch": 12345},
            "key_exports": {"Service": ["fooSchema"]},
        }
    )
    out = format_conventions_for_session(conv)
    assert "fooSchema" in out


def test_malformed_key_exports_value_does_not_wipe_other_sections() -> None:
    conv = _wrap(
        {
            "key_exports": {"BadArch": 999, "Service": ["fooSchema"]},
            "inheritance": {"Controller": {"dominant_base": "Base", "frequency": 0.99}},
        }
    )
    out = format_conventions_for_session(conv)
    assert "Base" in out
