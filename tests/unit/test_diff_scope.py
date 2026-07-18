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
