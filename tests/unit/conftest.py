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

import os
import re
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_peer_plugin_detection(monkeypatch):
    """Default every test to "superpowers is not installed".

    The SessionStart digest consults the real ~/.claude plugin registry, so any
    test reaching _peer_routing_block -- directly or through session_start() --
    otherwise exercises a 174-token-larger emission on a developer machine that
    has superpowers than in CI, which does not. Same hazard as the plugin-data
    dir below, same remedy.

    Tests that want the composed digest patch the same target again; the inner
    patch wins while active. The kill switch is left unset rather than set to
    "0" so the test that asserts on it still has a free variable, and so these
    defaults describe "peer absent" rather than "routing disabled" -- different
    states that happen to reach the same output.
    """
    monkeypatch.delenv("CHAMELEON_PEER_ROUTING", raising=False)
    with patch("chameleon_mcp.peer_plugins.superpowers_installed", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _isolated_plugin_data(tmp_path_factory, monkeypatch):
    """Default every test to an isolated CHAMELEON_PLUGIN_DATA dir.

    A test that bootstraps a repo without setting the override writes trust
    records, drift/index DB rows, and degraded telemetry into the developer's
    real ~/.local/share/chameleon — the suite polluted the real registry with
    hundreds of pytest tmp-repo rows this way. Tests that need a specific data
    dir set their own value (their setenv overrides this one); the default-path
    resolution test delenvs inside its body, which also wins over this fixture.
    """
    if "CHAMELEON_PLUGIN_DATA" not in os.environ:
        monkeypatch.setenv(
            "CHAMELEON_PLUGIN_DATA", str(tmp_path_factory.mktemp("isolated-plugin-data"))
        )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_judge_spawn: exercises the real judge._spawn_reviewer (subprocess "
        "mocked by the test); opts out of the no-real-spawn autouse guard.",
    )
    config.addinivalue_line(
        "markers",
        "perf: asserts a wall-clock bound rather than a behavior. Deselect with "
        '-m "not perf" on a loaded box; the repo\'s standing latency budgets live '
        "in tests/bench_hot_path.py, not here.",
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
    # The correctness/idiom lenses (stop/lenses/) spawn through the
    # status-returning variant; guard it too so an unmocked lens path degrades
    # to a no-spawn failure, never a real subprocess.
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


def hook_wrappers_from_registry() -> list[str]:
    """Every hook wrapper `hooks.json` actually registers.

    The three hook test suites each carried their own hand-typed list, and all
    three had drifted from the registry: `peer-skill-advise` (a live PreToolUse
    hook on the Skill matcher) appeared in none of them, and `stop-backstop` --
    the one hook that can refuse to end a turn -- was only ever shellchecked.
    Deriving the list here means a wrapper added to hooks.json is covered by the
    kill-switch, timeout and payload-guard suites the moment it is registered,
    instead of when someone remembers to append it in three places.
    """
    import json
    from pathlib import Path

    registry = Path(__file__).resolve().parents[2] / "plugin" / "hooks" / "hooks.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    names: list[str] = []
    for entries in (data.get("hooks") or {}).values():
        for entry in entries or ():
            for hook in entry.get("hooks") or ():
                cmd = hook.get("command") or ""
                match = re.search(r"run-hook\.cmd\"?\s+(\S+)", cmd)
                if match and match.group(1) not in names:
                    names.append(match.group(1))
    return names


class FakePopenFromRun:
    """Popen-shaped wrapper over a ``subprocess.run``-shaped test double.

    ``judge._spawn_reviewer_status`` spawns with Popen + ``communicate(timeout)``
    so a timed-out reviewer's whole process GROUP can be killed: ``run()``'s own
    timeout signals only the direct child, and `claude -p` starts children that
    inherit the pipe, so a grandchild could hold stdout open past the budget.

    The doubles keep asserting on the same ``(args, kwargs)`` they always did --
    including ``kwargs["input"]``, which judge now hands to ``communicate()``
    rather than to the spawn. The wrapped double is therefore invoked FROM
    ``communicate`` with ``input`` folded back into its kwargs, so a double that
    records the prompt still records it. Only the intercepted call moved; no
    assertion was relaxed.
    """

    def __init__(self, run_double, args, kwargs):
        self._run_double = run_double
        self._kwargs = kwargs
        self.args = args
        self.pid = -1
        self.returncode = 0

    def communicate(self, input=None, timeout=None):  # noqa: A002
        proc = self._run_double(self.args, **{**self._kwargs, "input": input})
        self.returncode = getattr(proc, "returncode", 0)
        return getattr(proc, "stdout", ""), getattr(proc, "stderr", "")

    def kill(self):
        return None

    def wait(self, timeout=None):
        return self.returncode


def as_popen_double(run_double):
    """Adapt a ``subprocess.run``-shaped double into a ``Popen``-shaped one.

    Accepts the three shapes the suites use: a callable, a bare return value,
    and an exception instance/class (``side_effect=OSError(...)``). A raising
    double raises from ``communicate``, which is inside the same try judge
    wraps around the spawn, so ``spawn_timeout`` / ``spawn_exec_error`` map
    exactly as they did under ``run``.
    """

    def _call(args, kwargs):
        if isinstance(run_double, BaseException):
            raise run_double
        if isinstance(run_double, type) and issubclass(run_double, BaseException):
            raise run_double()
        if callable(run_double):
            return run_double(args, **kwargs)
        return run_double

    def popen(args, **kwargs):
        return FakePopenFromRun(lambda a, **k: _call(a, k), list(args), kwargs)

    return popen
