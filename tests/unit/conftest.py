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
