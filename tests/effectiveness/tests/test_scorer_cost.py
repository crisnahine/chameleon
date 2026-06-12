from __future__ import annotations

from tests.effectiveness.scorers.base import run_scorer
from tests.effectiveness.tests.test_scorer_base import _ctx


def test_cost_scorer_reports_usd_and_wall_seconds(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.cost_usd = 0.123456789
    ctx.wall_seconds = 17.456
    out = run_scorer("cost", ctx)
    assert out == {"cost_usd": 0.123457, "wall_seconds": 17.46}
