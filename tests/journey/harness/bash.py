"""Bash subprocess wrapper used by the runner (and acts) for filesystem setup.

Distinct from Claude's own Bash tool calls inside a session.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path


class BashTimeout(Exception):
    pass


@dataclasses.dataclass
class BashResult:
    returncode: int
    stdout: str
    stderr: str


def run_bash(
    command: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> BashResult:
    """Run a bash command, capture output. Inherits + overrides env."""
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BashTimeout(f"bash command exceeded {timeout_s}s: {command!r}") from exc

    return BashResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
