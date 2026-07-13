"""Gist quality for idiom summaries: long parentheticals must not eat the cap.

An idiom directive often leads with a long enumeration in parens ("Write every
derived profile artifact (a.json, b.json, ... k.json) only inside ..."); a hard
cut at the summary cap would land inside that list and drop the verbs the
summary exists to carry. `_summarize_idiom_block` elides long parentheticals
first and truncates only if the sentence is still over budget.
"""

from __future__ import annotations

from chameleon_mcp.tools import _elide_long_parens, _summarize_idiom_block

# Mirrors the real atomic-commit idiom: an 11-item artifact list in parens ahead
# of the directive verbs.
_LONG_PAREN_BLOCK = (
    "### profile-writes-via-atomic-commit\n"
    "Language: python\n"
    "Write every derived profile artifact (archetypes.json, canonicals.json, "
    "conventions.json, rules.json, idioms.md, principles.md, profile.json, "
    "counterexamples.json, calls_index.json, renames.json, profile.summary.md) "
    "only inside a `with atomic_profile_commit(profile_dir) as txn_dir:` block, "
    "never a raw write into the live dir.\n"
    "\n"
    "Example:\n"
    "```\n"
    "with atomic_profile_commit(d) as txn_dir: ...\n"
    "```\n"
)


def test_directive_survives_cap_via_paren_elision():
    out = _summarize_idiom_block(_LONG_PAREN_BLOCK, max_chars=160)
    assert "atomic_profile_commit" in out
    assert "(...)" in out
    assert "canonicals.json" not in out  # the enumeration is what got elided
    assert len(out) <= 163  # cap + "..." tail at most


def test_under_cap_sentence_keeps_parens_verbatim():
    block = "### keep-parens\nUse threshold_int (never a literal) at the use site.\n"
    out = _summarize_idiom_block(block, max_chars=160)
    assert out == "Use threshold_int (never a literal) at the use site."


def test_elide_keeps_short_call_syntax_parens():
    text = 'Read caps via threshold_int("X") (the DEFAULTS dict carries every operator override).'
    out = _elide_long_parens(text)
    assert 'threshold_int("X")' in out
    assert "DEFAULTS dict" not in out


def test_elide_converges_on_nested_parens():
    inner = "x" * 30
    text = f"Start (outer with (inner {inner} span) and more outer padding here) end."
    out = _elide_long_parens(text)
    assert out == "Start (...) end."
    # the marker itself never re-matches
    assert _elide_long_parens(out) == out


def test_still_truncates_when_elision_is_not_enough():
    long_tail = "verb " * 60
    block = f"### long\n{long_tail.strip()}.\n"
    out = _summarize_idiom_block(block, max_chars=100)
    assert out.endswith("...")
    assert len(out) <= 103
