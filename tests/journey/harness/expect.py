"""Assertion helpers. Each takes phase: int for failure attribution.

Raises PhaseAssertionError on miss. The runner catches this and records
the phase as FAIL, then continues with the next phase.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any


class PhaseAssertionError(Exception):
    def __init__(self, phase: int, message: str):
        self.phase = phase
        super().__init__(f"[phase {phase}] {message}")


def path_exists(phase: int, path: Path) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"expected path to exist: {path}")


def path_absent(phase: int, path: Path) -> None:
    if path.exists():
        raise PhaseAssertionError(phase, f"expected path to be absent: {path}")


def json_field(phase: int, path: Path, key: str, expected: Any) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"json file missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data
    for part in key.split("."):
        if isinstance(actual, dict) and part in actual:
            actual = actual[part]
        else:
            raise PhaseAssertionError(phase, f"key {key!r} not found in {path}")
    if actual != expected:
        raise PhaseAssertionError(
            phase, f"{path}: key={key} expected={expected!r}, got={actual!r}"
        )


def json_field_in(phase: int, path: Path, key: str, allowed: list[Any]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data
    for part in key.split("."):
        actual = actual[part]
    if actual not in allowed:
        raise PhaseAssertionError(
            phase, f"{path}: key={key} value={actual!r} not in {allowed!r}"
        )


def file_size_between(phase: int, path: Path, min_bytes: int, max_bytes: int) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"file missing for size check: {path}")
    size = path.stat().st_size
    if not (min_bytes <= size <= max_bytes):
        raise PhaseAssertionError(
            phase, f"{path}: size={size} not in [{min_bytes}, {max_bytes}]"
        )


def file_mode(phase: int, path: Path, mode: int) -> None:
    if not path.exists():
        raise PhaseAssertionError(phase, f"file missing for mode check: {path}")
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode != mode:
        raise PhaseAssertionError(
            phase, f"{path}: mode={oct(actual_mode)} expected={oct(mode)}"
        )


