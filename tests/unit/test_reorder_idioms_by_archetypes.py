"""Tests for the multi-archetype idiom reorder used by the turn-end self-review.

``_reorder_idioms_by_archetypes`` surfaces idioms for ANY of the turn's edited
archetypes first, so the downstream char-cap truncation in ``_idiom_review_gate``
keeps the relevant idioms instead of cutting them away behind an unrelated
archetype's block at the top of idioms.md.
"""

from __future__ import annotations

from chameleon_mcp.tools import _reorder_idioms_by_archetypes

_IDIOMS = """### a
Archetype: controller
ctrl idiom

### b
Archetype: service
svc idiom

### c
general idiom

### d
Archetype: model
model idiom
"""


def _block_order(text: str) -> list[str]:
    return [ln[4:].strip() for ln in text.splitlines() if ln.startswith("### ")]


def test_union_of_archetypes_surfaced_first():
    out = _reorder_idioms_by_archetypes(_IDIOMS, ["service", "model"])
    # Matching-any blocks first (b, d), then general (c), then other (a).
    assert _block_order(out) == ["b", "d", "c", "a"]


def test_single_archetype_surfaced_first():
    out = _reorder_idioms_by_archetypes(_IDIOMS, ["controller"])
    assert _block_order(out) == ["a", "c", "b", "d"]


def test_match_is_case_insensitive():
    out = _reorder_idioms_by_archetypes(_IDIOMS, ["SERVICE"])
    assert _block_order(out)[0] == "b"


def test_none_and_blank_entries_tolerated():
    out = _reorder_idioms_by_archetypes(_IDIOMS, [None, "", "  ", "model"])
    assert _block_order(out)[0] == "d"


def test_no_matching_archetype_returns_unchanged():
    out = _reorder_idioms_by_archetypes(_IDIOMS, ["nonexistent"])
    assert out == _IDIOMS


def test_empty_archetype_set_returns_unchanged():
    assert _reorder_idioms_by_archetypes(_IDIOMS, []) == _IDIOMS


def test_text_without_blocks_returns_unchanged():
    plain = "just some prose, no ### headers\n"
    assert _reorder_idioms_by_archetypes(plain, ["service"]) == plain


def test_reorder_saves_relevant_idiom_from_char_cap():
    # The bug FIX 1 closes: a fixed top-truncation can cut the relevant idiom when
    # an unrelated archetype's block sits first. After reordering by the edited
    # archetype, the relevant idiom survives the same cap.
    filler = "x" * 1600
    idioms = (
        f"### unrelated\nArchetype: controller\n{filler}\n\n"
        "### relevant\nArchetype: service\nuse the ServiceClient wrapper\n"
    )
    cap = 1500
    # Without reorder the service idiom is past the cap.
    assert "ServiceClient" not in idioms[:cap]
    reordered = _reorder_idioms_by_archetypes(idioms, ["service"])
    assert "ServiceClient" in reordered[:cap]
