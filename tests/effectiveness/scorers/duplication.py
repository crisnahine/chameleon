"""Replaced by its dedicated task; fail-open until then."""

from __future__ import annotations

from tests.effectiveness.scorers.base import ScoreContext, unscored


def score(ctx: ScoreContext) -> dict:
    return unscored("scorer not implemented yet")
