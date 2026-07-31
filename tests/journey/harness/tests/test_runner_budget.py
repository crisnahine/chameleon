"""The budget model has to let a full run finish.

Both halves of this failed in practice. The default cap sat at $40 while the
sum of per-act MEDIAN costs was $40.38, so a healthy full run aborted partway
roughly half the time -- after the spend, with the gate incomplete. Then raising
one act's ceiling to match its recorded cost pushed the ceiling sum $0.30 past
the new default, which flips the failure to the other extreme: the preflight
check refuses to start at all.

Neither is visible by reading the table, so both are pinned here.
"""

from __future__ import annotations

from tests.journey.runner import _ACTS, build_parser


def test_default_budget_admits_a_full_run():
    """The preflight compares the SUM of selected ceilings against the cap, so a
    default below that sum rejects every full run before spawning anything."""
    ceilings = sum(a[2] for a in _ACTS)
    default = build_parser().parse_args([]).max_budget_usd
    assert default >= ceilings, (
        f"default --max-budget-usd {default} is below the ${ceilings:.2f} ceiling sum; "
        "a full run would exit 1 at the preflight check"
    )


def test_default_budget_keeps_headroom_for_one_ceiling_bump():
    """A default sitting exactly on the sum breaks the next time any single act
    is re-derived upward, which is a routine maintenance step."""
    ceilings = sum(a[2] for a in _ACTS)
    default = build_parser().parse_args([]).max_budget_usd
    assert default >= ceilings * 1.1, (
        f"default {default} leaves under 10% over the ${ceilings:.2f} ceiling sum; "
        "raising one act's ceiling would make full runs unstartable"
    )


def test_every_act_carries_a_positive_ceiling():
    """A zero or missing ceiling contributes nothing to the projection, so a
    runaway act in that slot could never trip the mid-run abort."""
    for act_id, _name, ceiling, phases in _ACTS:
        assert ceiling > 0, f"{act_id} has a non-positive ceiling"
        assert phases, f"{act_id} claims no phases"


def test_act_ids_and_phases_are_unique():
    """A duplicated phase number would let one act's PASS mask another's absence
    in the union that decides whether the gate actually covered everything."""
    ids = [a[0] for a in _ACTS]
    assert len(ids) == len(set(ids)), "duplicate act id"
    seen: dict[int, str] = {}
    for act_id, _name, _ceiling, phases in _ACTS:
        for p in phases:
            assert p not in seen, f"phase {p} claimed by both {seen[p]} and {act_id}"
            seen[p] = act_id
