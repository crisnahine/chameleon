"""Pins the timeout(1)/gtimeout(1) wrapper cap around each hook's python spawn.

Five per-edit/per-turn hooks (preflight-and-advise, posttool-verify,
posttool-recorder, callout-detector, session-start) wrap their python
invocation in a hard timeout so a hung interpreter cannot stall the editor.
stop-backstop uses a wider cap since it may also wait on the in-turn judge
poll (CHAMELEON_JUDGE_WAIT). Neither cap had a pin before this file: dropping
the wrapper entirely, or silently enlarging either constant, would go
undetected by the rest of the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"

_WRAPPER_RE = re.compile(r'\$\{TIMEOUT_BIN:\+"\$\{TIMEOUT_BIN\}" (\d+)\}')

FAST_HOOKS = [
    "preflight-and-advise",
    "posttool-verify",
    "posttool-recorder",
    "callout-detector",
    "session-start",
]

FAST_HOOK_TIMEOUT_SECONDS = 3
STOP_BACKSTOP_TIMEOUT_SECONDS = 55


def _wrapper_timeout(script_name: str) -> int:
    text = (HOOKS_DIR / script_name).read_text(encoding="utf-8")
    m = _WRAPPER_RE.search(text)
    assert m, f"{script_name} must keep its timeout(1) wrapper cap"
    return int(m.group(1))


@pytest.mark.parametrize("script_name", FAST_HOOKS)
def test_fast_hook_wrapper_timeout_is_capped_small(script_name):
    # A per-edit/per-turn hook blocks the editor while it runs; the wrapper
    # must stay small enough that a hung interpreter is killed fast rather
    # than stalling the user's turn.
    assert _wrapper_timeout(script_name) == FAST_HOOK_TIMEOUT_SECONDS


def test_stop_backstop_wrapper_timeout_is_capped_at_55():
    # stop-backstop can additionally wait on the in-turn judge poll
    # (CHAMELEON_JUDGE_WAIT), so its wrapper cap is wider than the other
    # per-turn hooks -- but it is still a hard ceiling, not unbounded.
    assert _wrapper_timeout("stop-backstop") == STOP_BACKSTOP_TIMEOUT_SECONDS


def test_stop_backstop_timeout_wider_than_fast_hooks():
    # The two caps must stay ordered relative to each other: stop-backstop
    # does strictly more work in the worst case than the other per-turn
    # hooks, so its cap must never regress to match or undercut theirs.
    assert _wrapper_timeout("stop-backstop") > FAST_HOOK_TIMEOUT_SECONDS
