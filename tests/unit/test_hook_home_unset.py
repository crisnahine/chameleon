"""The hook wrappers fail open when HOME is unset.

Each wrapper runs ``set -euo pipefail`` and, when CHAMELEON_HOOK_ERROR_LOG is
not set, builds LOG_DIR from ``$HOME``. Under ``set -u`` a bare ``$HOME`` that
is unset aborts the script (exit 1, no JSON) before any fail-open path runs.
The wrappers must instead fall back to a tmp dir and still emit empty-or-valid
JSON with exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "plugin" / "hooks"
PAYLOAD = (
    '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x.ts"},"session_id":"s","cwd":"/tmp"}'
)

ALL_WRAPPERS = [
    "preflight-and-advise",
    "session-start",
    "posttool-recorder",
    "posttool-verify",
    "callout-detector",
    "stop-backstop",
]


def _run_without_home(wrapper: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # Strip HOME so the else branch expands a bare ${HOME} under set -u.
    # CHAMELEON_HOOK_ERROR_LOG is intentionally left unset to take that branch.
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env.update(
        {
            "CHAMELEON_PLUGIN_DATA": str(tmp_path / "data"),
            "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT / "plugin"),
            "TMPDIR": str(tmp_path / "tmp"),
        }
    )
    env.pop("HOME", None)
    return subprocess.run(
        [str(HOOKS / wrapper)],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


@pytest.mark.parametrize("wrapper", ALL_WRAPPERS)
def test_wrapper_fails_open_with_home_unset(wrapper, tmp_path):
    proc = _run_without_home(wrapper, tmp_path)
    assert proc.returncode == 0, f"{wrapper} exited {proc.returncode}: {proc.stderr!r}"
    out = proc.stdout.strip()
    if out:
        # Any emitted stdout must be valid JSON, not a partial/crashed write.
        json.loads(out)
