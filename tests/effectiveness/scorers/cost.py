"""Cost scorer: per-cell USD + wall seconds, straight from the session."""

from __future__ import annotations

from tests.effectiveness.scorers.base import ScoreContext


def score(ctx: ScoreContext) -> dict:
    return {
        "cost_usd": round(float(ctx.cost_usd), 6),
        "wall_seconds": round(float(ctx.wall_seconds), 2),
    }
