"""Tests for the deterministic PR complexity-tier classifier (easy → complex).

Distinct from ``classify_change``'s ``risk`` (which rates review-cleanliness
confidence): the tier rates the change's inherent SIZE/NOVELTY/RISK structure
from diff facts alone, so per-tier review-clean rates can be tracked and the
hard/complex residual routed to humans. Zero LLM, zero I/O.
"""

from __future__ import annotations

from chameleon_mcp.autopass import classify_complexity_tier


def _facts(**kw):
    base = {
        "files_changed": 1,
        "lines_changed": 10,
        "new_files": 0,
        "unarchetyped_files": 0,
        "blast_radius": 0,
        "blast_radius_unknown": 0,
        "security_surface": False,
    }
    base.update(kw)
    return base


def test_tiny_in_pattern_change_is_easy():
    assert classify_complexity_tier(_facts()) == "easy"


def test_moderate_size_is_medium():
    assert classify_complexity_tier(_facts(files_changed=3, lines_changed=40)) == "medium"
    assert classify_complexity_tier(_facts(blast_radius=2)) == "medium"


def test_new_file_or_unarchetyped_is_hard():
    assert classify_complexity_tier(_facts(new_files=1)) == "hard"
    assert classify_complexity_tier(_facts(unarchetyped_files=1)) == "hard"
    assert classify_complexity_tier(_facts(files_changed=6, lines_changed=90)) == "hard"


def test_security_or_big_or_unknown_blast_is_complex():
    assert classify_complexity_tier(_facts(security_surface=True)) == "complex"
    assert classify_complexity_tier(_facts(blast_radius_unknown=1)) == "complex"
    assert classify_complexity_tier(_facts(files_changed=12)) == "complex"
    assert classify_complexity_tier(_facts(lines_changed=200)) == "complex"
    assert classify_complexity_tier(_facts(blast_radius=20)) == "complex"
    assert classify_complexity_tier(_facts(unarchetyped_files=3)) == "complex"


def test_complex_dominates_easy_signals():
    # A change that is otherwise tiny but touches a security surface is complex.
    assert (
        classify_complexity_tier(_facts(files_changed=1, lines_changed=2, security_surface=True))
        == "complex"
    )


def test_tier_is_independent_of_cleanliness():
    # The tier is structural: an unresolved blocking finding does not change it
    # (that drives auto_pass_eligible, not the tier).
    assert classify_complexity_tier(_facts(active_block_findings=5)) == "easy"


def test_missing_facts_default_safe():
    # Empty facts -> smallest structure -> easy (defaults are 0/False).
    assert classify_complexity_tier({}) == "easy"


def test_verdict_includes_complexity_tier():
    from chameleon_mcp.autopass import classify_change

    out = classify_change(_facts())
    assert out["complexity_tier"] == "easy"
    out2 = classify_change(_facts(security_surface=True))
    assert out2["complexity_tier"] == "complex"
