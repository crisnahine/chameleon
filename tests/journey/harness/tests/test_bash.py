"""Unit tests for bash subprocess wrapper."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.journey.harness.bash import BashResult, run_bash


def test_basic_command_capture(tmp_path: Path) -> None:
    """Run echo, capture stdout."""
    result = run_bash("echo hello", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


def test_env_override(tmp_path: Path) -> None:
    """Env dict overrides process env."""
    result = run_bash("printenv MY_TEST_VAR", cwd=tmp_path, env={"MY_TEST_VAR": "abc"})
    assert result.stdout.strip() == "abc"


def test_timeout(tmp_path: Path) -> None:
    """Timeout raises BashTimeout."""
    from tests.journey.harness.bash import BashTimeout

    with pytest.raises(BashTimeout):
        run_bash("sleep 5", cwd=tmp_path, timeout_s=1)


def test_non_zero_exit(tmp_path: Path) -> None:
    """Non-zero exit is captured, not raised."""
    result = run_bash("false", cwd=tmp_path)
    assert result.returncode == 1
