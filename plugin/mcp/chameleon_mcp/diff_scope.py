"""Diff-scoping for the per-edit lint.

The per-edit conformance lint runs against the whole file, so a finding anywhere
in the file is attributed to the current edit -- the top false-positive source
across every effectiveness eval (an edit blamed for a pre-existing issue it never
touched), measured to be 79-100% of per-edit findings. Diff-scoping fixes that by
surfacing only the findings the edit actually INTRODUCED: lint the file as it was
BEFORE the edit and again AFTER, and keep only the post findings absent from the
pre set.

``exempt_rules`` surface whenever present, pre-existing or not. The production
wiring passes the FULL block-eligible set (`violation_class.BLOCK_ELIGIBLE_RULES`
-- secrets, eval, phantom-import, naming/import/inheritance/jsx/file-naming), so
every ENFORCEMENT-relevant finding stays whole-file: the block partition, the
inline block, and the Stop-backstop arming are computed from exactly the same
findings as un-scoped lint, keeping enforcement byte-identical (a follow-up clean
edit can never disarm a still-present block-eligible violation an earlier edit in
the turn introduced). Only the pure-advisory dilution is diff-scoped.
``SECURITY_EXEMPT_RULES`` is the guaranteed minimum for a caller that does not
pass its own set.

The scope is deliberately narrow:
- Edit / MultiEdit modify part of a file, so pre-existing findings elsewhere are
  not the edit's doing -> diff-scope.
- Write / NotebookEdit author the whole file, so the model owns every finding in
  it -> no scoping (whole-file lint stands).
- A reversal that cannot be applied cleanly falls back to whole-file (returns
  None), never to a diff against a wrong reconstruction.

Both functions are pure (no I/O, no repo-code execution) so they unit-test in
isolation and add nothing to the hook hot path beyond a second in-process lint.
"""

from __future__ import annotations

import re

# "line 29" -> "line": a line-anchored finding (style-rule-violation embeds the
# line number in its message/actual) must key the same before and after an edit
# shifts line numbers, or it re-surfaces as "introduced" on any edit that adds or
# removes lines above it. Column counts and other numbers are left intact, so a
# genuinely different finding (101 vs 102 cols) still keys distinctly.
_LINE_REF_RE = re.compile(r"\bline\s+\d+", re.IGNORECASE)

# The guaranteed-minimum exempt set: a leaked credential or an eval/exec sink is a
# deterministic security fact that must always surface. The production wiring in
# posttool_verify passes the WIDER `violation_class.BLOCK_ELIGIBLE_RULES` (a
# superset of this) so no enforcement-relevant rule is ever diff-scoped; this
# subset is the fallback default and is asserted to be a subset of block-eligible.
SECURITY_EXEMPT_RULES: frozenset[str] = frozenset({"secret-detected-in-content", "eval-call"})


def reconstruct_pre_edit_content(tool_name: str, tool_input: dict, post_content: str) -> str | None:
    """Rebuild the file as it was BEFORE this edit, from the tool input.

    Returns None when diff-scoping does not apply or cannot be done reliably, so
    the caller keeps whole-file lint:
    - Write / NotebookEdit (whole-file authorship) -> None.
    - A reversal that is ambiguous (the replacement text is absent, or a
      single-shot replacement's new text does not appear exactly once in the post
      content) -> None, never a guess.
    - A pure deletion (new_string is empty) cannot be located to re-insert -> None.
    """
    if not isinstance(post_content, str) or not isinstance(tool_input, dict):
        return None
    if tool_name == "Edit":
        return _reverse_one(
            post_content,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            bool(tool_input.get("replace_all")),
        )
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        pre = post_content
        # Edits applied in order; reverse them last-first to peel back to pre.
        for e in reversed(edits):
            if not isinstance(e, dict):
                return None
            pre = _reverse_one(
                pre, e.get("old_string"), e.get("new_string"), bool(e.get("replace_all"))
            )
            if pre is None:
                return None
        return pre
    # Write / NotebookEdit / anything else: the whole file is the edit.
    return None


def _reverse_one(text: str, old: object, new: object, replace_all: bool) -> str | None:
    """Undo one old->new substitution in ``text``, returning the pre-substitution
    text or None when the reversal is ambiguous."""
    if not isinstance(old, str) or not isinstance(new, str) or old == new:
        return None
    if new == "":
        # A pure deletion (old -> "") leaves nothing to locate for re-insertion.
        return None
    if replace_all:
        # A replace_all is unsafe to reverse: if the new text ALSO existed in the
        # pre-edit file, `replace(new, old)` clobbers those pre-existing occurrences
        # too, yielding a wrong pre-content -- and whether the new text pre-existed
        # is undetectable from the post content alone. A wrong reconstruction can
        # false-SUPPRESS a genuinely-introduced finding (an enforcement gap), so
        # replace_all edits fall back to whole-file lint (no regression, just no
        # diff-scoping for this minority edit type). The single-shot path below is
        # safe: its count==1 guard already rejects the pre-existed-new case.
        return None
    if text.count(new) != 1:
        # Ambiguous (absent, or many occurrences): a single-shot Edit reverses
        # safely only when its new text appears exactly once.
        return None
    return text.replace(new, old, 1)


def _norm_line_refs(s: object) -> object:
    return _LINE_REF_RE.sub("line", s) if isinstance(s, str) else s


def _finding_key(v: dict) -> tuple:
    return (
        v.get("rule"),
        _norm_line_refs(v.get("expected")),
        _norm_line_refs(v.get("actual")),
        _norm_line_refs(v.get("message")),
    )


def edit_introduced_violations(
    pre_violations: list[dict],
    post_violations: list[dict],
    exempt_rules: frozenset[str] = SECURITY_EXEMPT_RULES,
) -> list[dict]:
    """The post findings the edit INTRODUCED: a finding whose key is absent from
    the pre set, plus every exempt (security) finding regardless. Pre-existing
    non-exempt findings are dropped. Order is preserved from ``post_violations``.

    Findings are keyed by (rule, expected, actual, message); the line-number
    reference is normalized out of the key so a shifted pre-existing finding
    matches its counterpart. The diff is a MULTISET diff, not set membership: a key
    that appears N times pre and M times post introduces max(0, M-N) instances. A
    single shifted finding (1 pre, 1 post) is pre-existing (0 introduced), but a
    NEW instance of a line-anchored rule -- a second `command-injection` an edit
    adds to a file that already had one -- surfaces, because its normalized key's
    post count exceeds its pre count. Set membership would have masked it.
    """
    from collections import Counter

    pre_counts = Counter(_finding_key(v) for v in pre_violations if isinstance(v, dict))
    seen: Counter = Counter()
    out: list[dict] = []
    for v in post_violations:
        if not isinstance(v, dict):
            continue
        if v.get("rule") in exempt_rules:
            out.append(v)
            continue
        key = _finding_key(v)
        seen[key] += 1
        if seen[key] > pre_counts.get(key, 0):
            out.append(v)
    return out
