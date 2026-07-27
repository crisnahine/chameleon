"""The real shell hook chain, driven against a real trusted profile.

Every other subprocess-level hook test drives a path that exits early: the
kill switch short-circuits in bash, HOME is unset, the interpreter cannot be
resolved. None of them proves the chain still produces guidance when nothing
is wrong, so the wrapper could stop emitting context entirely and the gated
suite would stay green -- the in-process tests call `preflight_and_advise`
directly and never touch `_resolve-python.sh`, the `timeout` wrapper, or the
stdout contract between them.

`tests/qa_hook_simulation.py` covers this ground, but it is a `main()` script
keyed on CHAMELEON_TEST_TS_REPO / CHAMELEON_TEST_RUBY_REPO pointing at real
external repos, and CI runs `tests/unit/` rather than `tests/`, so nothing
collects it. This is the same assertion in the shape the gate actually runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "plugin" / "hooks"
ARCH = "service"
WITNESS = "svc/order_service.py"


@pytest.fixture
def trusted_repo(tmp_path, monkeypatch):
    """A minimal profile the hooks will actually load, trusted for this repo id."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    from chameleon_mcp import tools
    from chameleon_mcp.profile.trust import grant_trust

    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (repo / "svc").mkdir()
    (repo / WITNESS).write_text(
        "class OrderService:\n    def execute(self):\n        return 1\n", encoding="utf-8"
    )
    (cham / "profile.json").write_text(
        json.dumps({"generation": 1, "language": "python", "schema_version": 8}), encoding="utf-8"
    )
    (cham / "archetypes.json").write_text(
        json.dumps(
            {
                "generation": 1,
                "archetypes": {
                    ARCH: {
                        # paths_pattern is the MATCHING key (<dir>:<ext>);
                        # paths_pattern_display is only ever shown. A fixture
                        # carrying just the display form resolves no archetype,
                        # and the hook then exits with an empty object.
                        "paths_pattern": "svc:py",
                        "paths_pattern_display": "svc/*.py",
                        "summary": "service objects",
                        "cluster_size": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (cham / "rules.json").write_text(json.dumps({"generation": 1, "rules": {}}), encoding="utf-8")
    (cham / "canonicals.json").write_text(
        json.dumps({"generation": 1, "canonicals": {ARCH: [{"witness": {"path": WITNESS}}]}}),
        encoding="utf-8",
    )
    (cham / "conventions.json").write_text(
        json.dumps({"generation": 1, "conventions": {}}), encoding="utf-8"
    )
    (cham / "COMMITTED").touch()
    grant_trust(tools._compute_repo_id(repo), cham)
    return repo


def _drive(wrapper: str, repo: Path, tmp_path: Path, file_rel: str) -> subprocess.CompletedProcess:
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / file_rel)},
            "session_id": "chain-test",
            "cwd": str(repo),
        }
    )
    env = {
        **os.environ,
        "CHAMELEON_PLUGIN_DATA": str(tmp_path / "data"),
        "CHAMELEON_HOOK_ERROR_LOG": str(tmp_path / "err.log"),
        "CHAMELEON_ALLOW_TMP_REPO": "1",
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT / "plugin"),
    }
    return subprocess.run(
        [str(HOOKS / wrapper)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_preflight_emits_guidance_through_the_real_shell_chain(trusted_repo, tmp_path):
    """The whole chain: wrapper -> interpreter resolve -> python -> stdout JSON.

    Asserts guidance actually arrives, not merely that the hook exited 0. A
    wrapper that emitted {} on every edit would satisfy exit-code checks while
    delivering nothing, which is the failure this file exists to catch.
    """
    proc = _drive("preflight-and-advise", trusted_repo, tmp_path, WITNESS)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    ctx = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "<chameleon-context>" in ctx
    assert ARCH in ctx


def test_the_chain_logs_nothing_when_it_succeeds(trusted_repo, tmp_path):
    """A healthy run must leave the error log clean.

    Guards the swallowed-stage marker against firing on the happy path: a
    marker written on every successful edit would make the new degradation
    class useless the day it shipped.
    """
    _drive("preflight-and-advise", trusted_repo, tmp_path, WITNESS)
    log = tmp_path / "err.log"
    assert not log.exists() or "swallowed=" not in log.read_text(encoding="utf-8")


def test_posttool_verify_survives_the_real_chain(trusted_repo, tmp_path):
    """The other per-edit wrapper, same contract: exit 0 and valid JSON."""
    proc = _drive("posttool-verify", trusted_repo, tmp_path, WITNESS)
    assert proc.returncode == 0, proc.stderr
    json.loads(proc.stdout or "{}")
