"""Placeholder family demonstrating the Scenario shape. Real families land in later tasks."""
from tests.dogfood.scenario import Scenario, Result


def _smoke_run(ctx) -> Result:
    return Result(status="PASS", notes="framework smoke", cost_usd=0.0)


SCENARIOS = [
    Scenario(
        id="0.0",
        name="framework smoke",
        family="meta",
        needs_claude=False,
        cost="free",
        requires=[],
        run=_smoke_run,
    ),
]
