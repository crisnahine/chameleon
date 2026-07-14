"""Shared unit-test fixtures.

The headline one keeps the suite from launching a real ``claude -p`` subprocess.
The correctness/duplication judges now inherit the real config dir so they stay
authenticated (the prior empty-config-dir approach silently broke auth, see
``judge._spawn_reviewer``). A side effect: a test that triggers the judge without
mocking it would now spawn a real, authenticated, slow, billable subprocess
instead of failing fast. This autouse guard defaults every test to a fail-open
no-spawn; the few tests that assert on the real spawn opt out with
``@pytest.mark.real_judge_spawn`` and mock ``subprocess`` themselves.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_judge_spawn: exercises the real judge._spawn_reviewer (subprocess "
        "mocked by the test); opts out of the no-real-spawn autouse guard.",
    )


@pytest.fixture(autouse=True)
def _no_real_judge_spawn(request, monkeypatch):
    if request.node.get_closest_marker("real_judge_spawn"):
        return
    try:
        from chameleon_mcp import judge
    except Exception:
        return
    monkeypatch.setattr(judge, "_spawn_reviewer", lambda *a, **k: None, raising=False)
    # run_correctness_judge spawns through the status-returning variant; guard it
    # too so an unmocked judge path degrades to a no-spawn failure, never a real
    # subprocess.
    monkeypatch.setattr(
        judge,
        "_spawn_reviewer_status",
        lambda *a, **k: (None, "spawn_exec_error"),
        raising=False,
    )
    # The turn-end VERIFY stage spawns the refuter, which bound the status spawner
    # via `from judge import _spawn_reviewer_status as _spawn_status` at import time,
    # so patching judge above does NOT reach it. Neutralize the refuter's own binding
    # (and its CLI probe) so a gate that reaches VERIFY degrades to unverified rather
    # than launching a real, billable `claude -p`. Tests that assert on refuter
    # verdicts patch `refuter.run_batch` themselves, overriding this.
    try:
        from chameleon_mcp import refuter

        monkeypatch.setattr(
            refuter, "_spawn_status", lambda *a, **k: (None, "spawn_exec_error"), raising=False
        )
        monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None, raising=False)
    except Exception:
        pass
    # Phase 3: stop/scheduler.py is the only code allowed to spawn a model going
    # forward. Its launch_job() detaches a real `python -m chameleon_mcp.stop.job`
    # child (which itself spawns `claude -p`), so an unmocked call through it is
    # exactly as billable as an unmocked judge/refuter spawn above. Neutralize it
    # the same way: fail closed to "launch failed", never a real subprocess. Tests
    # that assert on the real detach mechanics opt out with `real_judge_spawn` and
    # mock `subprocess.Popen` themselves, same convention as the judge/refuter guards.
    try:
        from chameleon_mcp.stop import scheduler

        monkeypatch.setattr(scheduler, "launch_job", lambda *a, **k: False, raising=False)
    except Exception:
        pass
