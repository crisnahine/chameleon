"""Regression tests for production_ref worktree-sweep liveness.

``_creating_pid_alive`` decides whether a leftover ``<sha12>-<pid>`` tree
under the prodtree container is safe to sweep. The contract: a tree is
sweepable only when its creating pid is genuinely dead. Anything the
function cannot read as a dead pid -- a live pid, a permission-denied
probe, or a name with no parseable pid suffix -- must read as ALIVE so a
second checkout sharing the repo_id never has its in-flight tree swept.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from chameleon_mcp.production_ref import _creating_pid_alive


def _dead_pid() -> int:
    """A pid that has been spawned and reaped, so it is no longer running."""
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_live_pid_reads_alive() -> None:
    assert _creating_pid_alive(f"abc123def456-{os.getpid()}") is True


def test_dead_pid_is_sweepable() -> None:
    assert _creating_pid_alive(f"abc123def456-{_dead_pid()}") is False


@pytest.mark.parametrize(
    "tree_name",
    [
        "abc123def456-notapid",
        "abc123def456",
        "no-suffix-name",
        "trailing-dash-",
        "",
    ],
)
def test_malformed_name_reads_alive(tree_name: str) -> None:
    # A name we cannot parse a pid from could belong to an in-flight tree;
    # treat it as alive (not sweepable) per the documented intent.
    assert _creating_pid_alive(tree_name) is True
