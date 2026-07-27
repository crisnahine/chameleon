"""Scope a lint to the violations a commit actually INTRODUCED.

Written for the effectiveness studies and promoted out of ``tests/`` when the
headless gate needed the same arithmetic. Both consumers must agree by
construction: a gate that counted differently from the study measuring it would
report a number nothing else in the repo could reproduce.

Both effectiveness studies started by linting each changed file's full blob and
counting every row. That measures the file's accumulated violation load, not the
commit's contribution: a file carrying 40 pre-existing violations scores 40 every
time anyone touches it, whatever the commit did. Under that counting a commit
that FIXES two violations and a commit that adds two both move the monthly rate
the same direction, because the level term swamps the delta. The correction is
large and measured -- pre-existing load ran 5.2x the introduced count on one
dogfood repo and 5.7x on the other.

docs/effectiveness-study.md pre-registered "violations introduced per 100 changed
source files". This module is what makes the code match the registration:

  scoped   - lint the file at the commit AND at its first parent, then subtract
             by greedy multiset match, so only NET-NEW rows count. The identical
             technique already lives in tests/effectiveness/scorers/convention.py;
             this module is the shared spelling so the two arms cannot drift.
  filtered - drop rules the per-edit path never shows the model. A rule chameleon
             suppresses before the model reads it cannot be one chameleon changed
             the rate of, so counting it only adds variance to the estimate.

Neither adjustment can manufacture an effect. Both shrink the counted surface,
and a null that survives them is a stronger null than the original, because it
is no longer explained by pre-existing load or by unenforced rules.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

# Rules the per-edit path strips before the model ever reads them. Keep in sync
# with hook_helper's posttool_verify filter: cross-file-importers is dropped there
# because the pre-edit injection already carried the caller list, making the lint
# row redundant rather than informative. On one dogfood repo it was 63% of all
# rows -- counting it would let a rule with no edit-time channel dominate the
# estimate of an edit-time intervention.
PER_EDIT_SUPPRESSED = frozenset({"cross-file-importers"})


def violation_key(row: dict) -> tuple:
    """Identity used to match a violation across two versions of a file.

    Deliberately excludes line/column: a pre-existing violation that shifted
    down because lines were inserted above it is the SAME violation, and keying
    on position would count it as newly introduced on every unrelated edit.
    """
    return (row.get("rule"), row.get("expected"), row.get("actual"), row.get("message"))


def actionable(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("rule") not in PER_EDIT_SUPPRESSED]


def blob_at(repo: Path, rev: str, rel: str, *, max_bytes: int) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{rel}"], capture_output=True)
    if r.returncode != 0 or len(r.stdout) > max_bytes:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def net_new(current: list[dict], baseline: list[dict]) -> tuple[list[dict], int]:
    """Rows present in `current` that are not matched by a row in `baseline`.

    Greedy multiset match: three identical violations before and five after
    yields two introduced, not five and not zero. Returns the introduced rows
    plus the baseline size, so a caller can report how much load was subtracted
    -- that number is the whole reason the naive count was wrong, and a study
    that hides it cannot be audited. The gate calls this once per changed file,
    so the size comes off len() rather than a second pass that would re-key
    every baseline row to reach the same number.
    """
    pool = Counter(violation_key(r) for r in baseline)
    introduced = []
    for row in current:
        key = violation_key(row)
        if pool.get(key, 0) > 0:
            pool[key] -= 1
            continue
        introduced.append(row)
    return introduced, len(baseline)


def first_parent(repo: Path, sha: str) -> str | None:
    """The commit's first parent, or None for a root commit (no baseline exists)."""
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{sha}^1"],
        capture_output=True,
        text=True,
    )
    out = r.stdout.strip()
    return out or None
