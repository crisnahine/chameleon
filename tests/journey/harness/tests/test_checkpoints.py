"""Unit tests for checkpoint JSONL parsing + phase attribution."""
from __future__ import annotations

from pathlib import Path

from tests.journey.harness.checkpoints import (
    parse_checkpoint_file,
)


def test_started_and_completed_pair(tmp_path: Path) -> None:
    """Phase with both started + completed events is PASS."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 1, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 1, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[1])

    assert parse_errors == 0
    assert outcomes[1].status == "PASS"


def test_started_without_completed_is_fail(tmp_path: Path) -> None:
    """Phase that started but didn't complete is FAIL."""
    f = tmp_path / "cp.jsonl"
    f.write_text('{"phase": 2, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n')

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[2])

    assert parse_errors == 0
    assert outcomes[2].status == "FAIL"
    assert "incomplete" in outcomes[2].notes.lower()


def test_phase_never_started_is_skip(tmp_path: Path) -> None:
    """Expected phase missing entirely is SKIP."""
    f = tmp_path / "cp.jsonl"
    f.write_text("")

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[5])

    assert parse_errors == 0
    assert outcomes[5].status == "SKIP"
    assert "not attempted" in outcomes[5].notes.lower()


def test_explicit_failed_status(tmp_path: Path) -> None:
    """Phase with status:failed event is FAIL."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 3, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 3, "status": "failed", "ts": "2026-05-21T00:00:02Z", "notes": "assertion X"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[3])

    assert outcomes[3].status == "FAIL"
    assert "assertion X" in outcomes[3].notes


def test_malformed_line_is_skipped(tmp_path: Path) -> None:
    """Malformed JSON line increments parse_errors but doesn't abort."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        '{"phase": 4, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        'this is not json\n'
        '{"phase": 4, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[4])

    assert parse_errors == 1
    assert outcomes[4].status == "PASS"


def test_single_passed_event(tmp_path: Path) -> None:
    """New single-event schema: status=passed -> PASS."""
    f = tmp_path / "cp.jsonl"
    f.write_text('{"phase": 1, "status": "passed"}\n')
    outcomes, _ = parse_checkpoint_file(f, expected_phases=[1])
    assert outcomes[1].status == "PASS"


def test_single_failed_event(tmp_path: Path) -> None:
    """New single-event schema: status=failed -> FAIL with notes."""
    f = tmp_path / "cp.jsonl"
    f.write_text('{"phase": 2, "status": "failed", "notes": "boom"}\n')
    outcomes, _ = parse_checkpoint_file(f, expected_phases=[2])
    assert outcomes[2].status == "FAIL"
    assert "boom" in outcomes[2].notes


def test_skip_phase_with_parse_errors_includes_hint(tmp_path: Path) -> None:
    """SKIP-attributed phases include corruption hint when parse_errors > 0."""
    f = tmp_path / "cp.jsonl"
    f.write_text(
        'invalid json line\n'
        '{"phase": 5, "status": "started", "ts": "2026-05-21T00:00:00Z"}\n'
        '{"phase": 5, "status": "completed", "ts": "2026-05-21T00:00:05Z"}\n'
    )

    outcomes, parse_errors = parse_checkpoint_file(f, expected_phases=[5, 6])

    assert parse_errors == 1
    assert outcomes[5].status == "PASS"
    assert outcomes[6].status == "SKIP"
    assert "checkpoint corruption" in outcomes[6].notes.lower()
