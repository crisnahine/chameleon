"""Act 0: Pre-flight wipe + isolation setup.

This act is mostly runner-side scaffolding. The runner has already created
<run_dir>/* in build_context() and copied fixtures via setup_fixture() in
runner.py. This act verifies isolation and emits a single checkpoint.
"""
from __future__ import annotations

from pathlib import Path

from tests.journey.acts.act_base import ActResult
from tests.journey.harness import expect
from tests.journey.harness.checkpoints import PhaseOutcome
from tests.journey.harness.context import JourneyContext


def run(ctx: JourneyContext) -> ActResult:
    phase = 0
    notes: list[str] = []

    try:
        # Env vars point under run_dir
        for var in ("CHAMELEON_PLUGIN_DATA", "CHAMELEON_HMAC_KEY_PATH", "TMPDIR", "CHAMELEON_HOOK_ERROR_LOG"):
            value = ctx.env.get(var)
            assert value, f"{var} not set in ctx.env"
            assert str(ctx.run_dir) in value, f"{var}={value!r} is not under {ctx.run_dir}"

        # Per-run dirs exist + are empty (or only contain harness scaffolding)
        expect.path_exists(phase, ctx.plugin_data_dir)
        expect.path_exists(phase, ctx.tmpdir)
        expect.path_exists(phase, ctx.run_dir / "working")
        expect.path_exists(phase, ctx.run_dir / "checkpoints")

        # Home dir guard: developer's own chameleon data must NOT be inside run_dir
        home_data = Path.home() / ".local" / "share" / "chameleon"
        if home_data.exists():
            # If dev has chameleon data, ensure run_dir is NOT a parent of it (silly check, but enforces isolation intent)
            try:
                home_data.resolve().relative_to(ctx.run_dir.resolve())
                raise AssertionError("home dir is inside run_dir, isolation broken")
            except ValueError:
                pass  # expected: home_data is outside run_dir

        outcome = PhaseOutcome(phase=phase, status="PASS", notes="; ".join(notes) or "isolation verified")
    except (expect.PhaseAssertionError, AssertionError) as e:
        outcome = PhaseOutcome(phase=phase, status="FAIL", notes=str(e))

    return ActResult(
        act_id="00_preflight",
        cost_usd=0.0,
        phase_outcomes=[outcome],
        checkpoint_parse_errors=0,
    )
