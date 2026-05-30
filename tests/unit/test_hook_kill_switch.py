"""The hook wrappers honor CHAMELEON_DISABLE / CHAMELEON_VERIFY in bash.

Before this, the kill switch only took effect AFTER a full python interpreter
spawned (optouts returned user_disable at the end), so a "disabled" plugin
still paid ~80ms per Edit/Write/Bash. The wrappers now short-circuit in bash
(emit {} and exit) before any python spawn.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"
PAYLOAD = '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x.ts"},"session_id":"s"}'

ALL_WRAPPERS = [
    "preflight-and-advise", "session-start", "posttool-recorder",
    "posttool-verify", "callout-detector",
]


def _run(wrapper: str, env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "CHAMELEON_PLUGIN_DATA": str(tmp_path / "data"),
        "CHAMELEON_HOOK_ERROR_LOG": str(tmp_path / "err.log"),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        **env_extra,
    }
    return subprocess.run(
        [str(HOOKS / wrapper)],
        input=PAYLOAD, capture_output=True, text=True, env=env, timeout=15,
    )


@pytest.mark.parametrize("wrapper", ALL_WRAPPERS)
def test_disable_short_circuits_every_wrapper(wrapper, tmp_path):
    proc = _run(wrapper, {"CHAMELEON_DISABLE": "1"}, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
    # No hook-failure logged: the short-circuit is clean, not a fallback.
    log = tmp_path / "err.log"
    assert not (log.exists() and "failed" in log.read_text())


def test_verify_off_short_circuits_posttool_verify(tmp_path):
    proc = _run("posttool-verify", {"CHAMELEON_VERIFY": "0"}, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
