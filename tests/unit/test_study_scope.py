"""The subtraction that decides every number both effectiveness studies report.

A bug here does not crash anything -- it silently shifts the measured rate, and
the study's whole claim rests on that rate. So the cases below pin the direction
of each failure mode rather than only the happy path.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.study_scope import (
    PER_EDIT_SUPPRESSED,
    actionable,
    first_parent,
    net_new,
    violation_key,
)


def _v(rule="naming", expected="camelCase", actual="snake_case", message="m", line=1):
    return {"rule": rule, "expected": expected, "actual": actual, "message": message, "line": line}


def test_unchanged_file_introduces_nothing():
    """The bug this whole module exists to fix: a touched file used to score its
    entire accumulated load on every commit."""
    rows = [_v(), _v(rule="import-order"), _v(rule="jsx")]
    introduced, carried = net_new(rows, list(rows))
    assert introduced == []
    assert carried == 3


def test_only_the_added_row_counts():
    before = [_v(), _v(rule="import-order")]
    after = [*before, _v(rule="jsx")]
    introduced, carried = net_new(after, before)
    assert [r["rule"] for r in introduced] == ["jsx"]
    assert carried == 2


def test_multiset_not_set_semantics():
    """Three identical violations before and five after is two introduced --
    set-difference would say zero and lose every duplicate-shaped regression."""
    introduced, _ = net_new([_v()] * 5, [_v()] * 3)
    assert len(introduced) == 2


def test_fixing_violations_never_scores_negative():
    """A commit that REMOVES violations introduces none. The count is a floor at
    zero by construction; a negative would corrupt the per-100-files rate."""
    introduced, carried = net_new([_v()], [_v()] * 4)
    assert introduced == []
    assert carried == 4


def test_new_file_has_empty_baseline_so_every_row_is_introduced():
    rows = [_v(), _v(rule="jsx")]
    introduced, carried = net_new(rows, [])
    assert len(introduced) == 2
    assert carried == 0


def test_line_shift_is_not_a_new_violation():
    """Inserting lines above a pre-existing violation moves it. Keying on
    position would score it as introduced on every unrelated edit in the file."""
    before = [_v(line=10)]
    after = [_v(line=87)]
    introduced, _ = net_new(after, before)
    assert introduced == []


def test_same_rule_different_content_is_a_distinct_violation():
    """The key is not the rule alone -- two different naming violations in one
    file must not cancel each other out."""
    before = [_v(actual="foo_bar")]
    after = [_v(actual="baz_qux")]
    introduced, _ = net_new(after, before)
    assert len(introduced) == 1
    assert violation_key(before[0]) != violation_key(after[0])


def test_actionable_drops_only_the_per_edit_suppressed_rule():
    rows = [_v(), _v(rule="cross-file-importers"), _v(rule="jsx")]
    kept = actionable(rows)
    assert [r["rule"] for r in kept] == ["naming", "jsx"]
    assert "cross-file-importers" in PER_EDIT_SUPPRESSED


def test_actionable_is_applied_before_subtraction_not_after():
    """Order matters: filtering after subtraction would let a suppressed row in
    the baseline cancel a real introduced row that happens to share its key."""
    before = [_v(rule="cross-file-importers")]
    after = [_v(rule="cross-file-importers"), _v(rule="jsx")]
    introduced, carried = net_new(actionable(after), actionable(before))
    assert [r["rule"] for r in introduced] == ["jsx"]
    assert carried == 0


def test_first_parent_of_root_commit_is_none(tmp_path):
    """A root commit has no baseline. None must mean empty baseline, not crash."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "root"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    assert first_parent(tmp_path, head) is None

    (tmp_path / "a.py").write_text("x = 2\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "second"], check=True)
    second = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert first_parent(tmp_path, second) == head


@pytest.mark.parametrize("missing", ["rule", "expected", "actual", "message"])
def test_key_tolerates_absent_fields(missing):
    """lint_file omits null-valued fields, so a row can arrive without any of
    these. Matching must stay consistent rather than raising mid-study."""
    row = _v()
    row.pop(missing)
    introduced, _ = net_new([row], [dict(row)])
    assert introduced == []
