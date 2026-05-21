"""Unit tests for expect.* assertion helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.journey.harness import expect


def test_path_exists_passes(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    expect.path_exists(phase=1, path=f)


def test_path_exists_fails(tmp_path: Path) -> None:
    with pytest.raises(expect.PhaseAssertionError) as exc:
        expect.path_exists(phase=2, path=tmp_path / "missing.txt")
    assert "phase 2" in str(exc.value).lower()


def test_path_absent_passes(tmp_path: Path) -> None:
    expect.path_absent(phase=1, path=tmp_path / "absent.txt")


def test_path_absent_fails(tmp_path: Path) -> None:
    f = tmp_path / "exists.txt"
    f.write_text("x")
    with pytest.raises(expect.PhaseAssertionError):
        expect.path_absent(phase=1, path=f)


def test_json_field_equals(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"schema_version": 7, "name": "x"}))
    expect.json_field(phase=1, path=f, key="schema_version", expected=7)


def test_json_field_mismatch_raises(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"schema_version": 6}))
    with pytest.raises(expect.PhaseAssertionError) as exc:
        expect.json_field(phase=1, path=f, key="schema_version", expected=7)
    assert "schema_version" in str(exc.value)
    assert "expected=7" in str(exc.value)


def test_json_field_in_allowed(tmp_path: Path) -> None:
    f = tmp_path / "doc.json"
    f.write_text(json.dumps({"match_quality": "ast"}))
    expect.json_field_in(phase=1, path=f, key="match_quality", allowed=["ast", "exact", "fallback", "none"])


def test_file_size_between(tmp_path: Path) -> None:
    f = tmp_path / "size.bin"
    f.write_bytes(b"x" * 1024)
    expect.file_size_between(phase=1, path=f, min_bytes=1000, max_bytes=2000)
    with pytest.raises(expect.PhaseAssertionError):
        expect.file_size_between(phase=2, path=f, min_bytes=2000, max_bytes=3000)


def test_file_mode(tmp_path: Path) -> None:
    f = tmp_path / "mode.txt"
    f.write_text("x")
    f.chmod(0o600)
    expect.file_mode(phase=1, path=f, mode=0o600)
    with pytest.raises(expect.PhaseAssertionError):
        expect.file_mode(phase=2, path=f, mode=0o644)
