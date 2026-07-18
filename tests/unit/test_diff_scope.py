"""Diff-scoping helpers for the per-edit lint."""

from __future__ import annotations

from chameleon_mcp.diff_scope import (
    SECURITY_EXEMPT_RULES,
    edit_introduced_violations,
    reconstruct_pre_edit_content,
)


def _v(rule, message="m", expected="e", actual="a"):
    return {"rule": rule, "message": message, "expected": expected, "actual": actual}


# ---- reconstruct_pre_edit_content ----------------------------------------


def test_edit_insertion_reverses_to_pre():
    # Edit that inserts a new block: old_string is the anchor, new_string adds text.
    post = "class A {\n  foo() {}\n  bar() {}\n}\n"
    ti = {"old_string": "  foo() {}\n", "new_string": "  foo() {}\n  bar() {}\n"}
    pre = reconstruct_pre_edit_content("Edit", ti, post)
    assert pre == "class A {\n  foo() {}\n}\n"


def test_edit_pure_insertion_old_empty():
    # old_string == "" (append) is reversible: remove the inserted new_string once.
    post = "line1\nNEWLINE\nline2\n"
    ti = {"old_string": "", "new_string": "NEWLINE\n"}
    pre = reconstruct_pre_edit_content("Edit", ti, post)
    assert pre == "line1\nline2\n"


def test_edit_replace_all_falls_back_to_whole_file():
    # replace_all is unsafe to reverse: if the new text pre-existed in the file,
    # replace(new, old) clobbers those occurrences too -> a wrong pre-content that
    # could false-suppress a real introduced finding. So it falls back (None).
    post = "x = NEW; y = NEW; z = NEW\n"
    ti = {"old_string": "OLD", "new_string": "NEW", "replace_all": True}
    assert reconstruct_pre_edit_content("Edit", ti, post) is None
    # a MultiEdit containing any replace_all edit also falls back
    ti2 = {
        "edits": [
            {"old_string": "aaa", "new_string": "AAA"},
            {"old_string": "OLD", "new_string": "NEW", "replace_all": True},
        ]
    }
    assert reconstruct_pre_edit_content("MultiEdit", ti2, "AAA NEW NEW\n") is None


def test_edit_ambiguous_new_string_returns_none():
    # new_string appears more than once and replace_all is false -> cannot reverse.
    post = "dup dup rest\n"
    ti = {"old_string": "one", "new_string": "dup"}
    assert reconstruct_pre_edit_content("Edit", ti, post) is None


def test_edit_pure_deletion_returns_none():
    ti = {"old_string": "removed text", "new_string": ""}
    assert reconstruct_pre_edit_content("Edit", ti, "remaining\n") is None


def test_write_and_notebook_return_none():
    assert reconstruct_pre_edit_content("Write", {"content": "x"}, "x") is None
    assert reconstruct_pre_edit_content("NotebookEdit", {"new_source": "x"}, "x") is None


def test_multiedit_reverses_in_order():
    # two sequential edits; reversal peels them back last-first
    post = "AAA one BBB two\n"
    ti = {
        "edits": [
            {"old_string": "aaa", "new_string": "AAA"},
            {"old_string": "bbb", "new_string": "BBB"},
        ]
    }
    pre = reconstruct_pre_edit_content("MultiEdit", ti, post)
    assert pre == "aaa one bbb two\n"


def test_multiedit_one_bad_edit_falls_back():
    post = "AAA one dup dup\n"
    ti = {
        "edits": [
            {"old_string": "aaa", "new_string": "AAA"},
            {"old_string": "x", "new_string": "dup"},  # ambiguous
        ]
    }
    assert reconstruct_pre_edit_content("MultiEdit", ti, post) is None


def test_malformed_inputs_fail_safe():
    assert reconstruct_pre_edit_content("Edit", {}, "x") is None
    assert reconstruct_pre_edit_content("Edit", {"old_string": 5, "new_string": "y"}, "x") is None
    assert reconstruct_pre_edit_content("Edit", {"old_string": "a", "new_string": "a"}, "x") is None
    assert reconstruct_pre_edit_content("Edit", {"old_string": "a", "new_string": "b"}, 123) is None
    assert reconstruct_pre_edit_content("MultiEdit", {"edits": []}, "x") is None


# ---- edit_introduced_violations ------------------------------------------


def test_pre_existing_finding_dropped():
    pre = [_v("naming-convention-violation", "bad foo_bar")]
    post = [_v("naming-convention-violation", "bad foo_bar")]
    assert edit_introduced_violations(pre, post) == []


def test_new_finding_surfaced():
    pre = [_v("naming-convention-violation", "bad foo_bar")]
    post = [
        _v("naming-convention-violation", "bad foo_bar"),
        _v("naming-convention-violation", "bad baz_qux"),
    ]
    got = edit_introduced_violations(pre, post)
    assert len(got) == 1 and got[0]["message"] == "bad baz_qux"


def test_security_finding_always_surfaced_even_if_preexisting():
    pre = [_v("secret-detected-in-content", "aws key")]
    post = [_v("secret-detected-in-content", "aws key")]
    got = edit_introduced_violations(pre, post)
    assert len(got) == 1  # exempt: surfaces despite being pre-existing


def test_eval_exempt():
    pre = [_v("eval-call", "eval used")]
    post = [_v("eval-call", "eval used")]
    assert len(edit_introduced_violations(pre, post)) == 1


def test_order_preserved_and_mixed():
    pre = [_v("import-preference-violation", "prefer import")]
    post = [
        _v("secret-detected-in-content", "key"),  # exempt
        _v("import-preference-violation", "prefer import"),  # pre-existing -> drop
        _v("naming-convention-violation", "new bad name"),  # new -> keep
    ]
    got = [v["rule"] for v in edit_introduced_violations(pre, post)]
    assert got == ["secret-detected-in-content", "naming-convention-violation"]


def test_empty_pre_all_new():
    post = [_v("naming-convention-violation", "x"), _v("import-preference-violation", "y")]
    assert len(edit_introduced_violations([], post)) == 2


def test_non_dict_entries_ignored():
    got = edit_introduced_violations([None, "junk"], [_v("naming-convention-violation"), 5])
    assert len(got) == 1


def test_security_exempt_rules_are_subset_of_block_eligible():
    # Parity guard: every exempt rule must be a real deterministic block-eligible
    # rule, so diff-scoping never exempts a rule that isn't actually security.
    from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

    assert SECURITY_EXEMPT_RULES <= BLOCK_ELIGIBLE_RULES


def test_production_exempt_keeps_every_block_eligible_finding_whole_file():
    # The wiring passes BLOCK_ELIGIBLE_RULES, so a pre-existing block-eligible
    # finding (naming, phantom-import, ...) is NEVER diff-scoped -> the hard
    # partition + Stop arming stay computed from the whole-file set (enforcement
    # byte-identical, no follow-up-edit disarm). Only advisory findings drop.
    from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

    pre = [
        _v("phantom-import", "broken import ./nope"),
        _v("naming-convention-violation", "bad foo_bar"),
        _v("some-style-advisory", "spacing"),  # NOT block-eligible
    ]
    post = list(pre)  # a clean follow-up edit: same findings, nothing introduced
    got = {v["rule"] for v in edit_introduced_violations(pre, post, BLOCK_ELIGIBLE_RULES)}
    # both block-eligible findings survive (arming preserved); the advisory drops
    assert "phantom-import" in got
    assert "naming-convention-violation" in got
    assert "some-style-advisory" not in got


def test_finding_key_is_line_number_insensitive():
    # BUG #2: a line-anchored finding (style-rule-violation embeds "line N" in its
    # message/actual) must match its post-edit counterpart after a line shift, or a
    # pre-existing finding re-surfaces as "introduced" on any edit that adds/removes
    # lines above it. The docstring already promises "no line number distinguishes
    # them" -- enforce it in the key.
    pre = [
        {
            "rule": "style-rule-violation",
            "expected": "<matches config>",
            "actual": "line 29 is 101 cols (max 100)",
            "message": "line 29 is 101 columns; max 100.",
        }
    ]
    post = [
        {
            "rule": "style-rule-violation",
            "expected": "<matches config>",
            "actual": "line 31 is 101 cols (max 100)",  # same violation, shifted 2 lines
            "message": "line 31 is 101 columns; max 100.",
        }
    ]
    # pre-existing -> must NOT be re-surfaced as introduced
    assert edit_introduced_violations(pre, post, frozenset()) == []
    # but a genuinely different overage (102 vs 101 cols) is still distinct
    post2 = [
        {
            "rule": "style-rule-violation",
            "expected": "<matches config>",
            "actual": "line 31 is 102 cols (max 100)",
            "message": "line 31 is 102 columns; max 100.",
        }
    ]
    assert len(edit_introduced_violations(pre, post2, frozenset())) == 1


def _sink(line):
    # A dangerous-sink advisory whose ONLY per-instance discriminator is the line
    # number (command-injection, insecure-random, weak-hash, ...). After line-ref
    # normalization two instances share a key, so a set-based diff masks a new one.
    return {
        "rule": "command-injection",
        "expected": "",
        "actual": "",
        "message": f"SECURITY: shell command built from a variable at line {line}.",
    }


def test_new_instance_of_line_anchored_rule_still_surfaces():
    # An edit ADDS a second command-injection; the file already had one. After
    # line-ref normalization both share a key, but a NEW instance must still surface
    # -- the diff is a MULTISET diff (count increase), not set membership.
    pre = [_sink(12)]
    post = [_sink(5), _sink(12)]  # new sink at line 5, old shifted to 12
    got = edit_introduced_violations(pre, post, frozenset())
    assert len(got) == 1
    assert got[0]["rule"] == "command-injection"


def test_line_shifted_single_finding_not_resurfaced():
    # Count parity: one pre, one post (same finding, shifted) -> 0 introduced. This
    # is the v4.4.7 line-ref fix; the multiset diff must preserve it.
    assert edit_introduced_violations([_sink(12)], [_sink(31)], frozenset()) == []


def test_pre_existing_duplicate_pair_not_surfaced():
    # Two pre, two post of the same normalized key -> 0 introduced (both pre-existing).
    pre = [_sink(4), _sink(9)]
    post = [_sink(6), _sink(11)]
    assert edit_introduced_violations(pre, post, frozenset()) == []


def test_three_post_two_pre_surfaces_one():
    # Three post instances, two pre -> exactly one newly introduced.
    pre = [_sink(4), _sink(9)]
    post = [_sink(2), _sink(6), _sink(11)]
    assert len(edit_introduced_violations(pre, post, frozenset())) == 1
