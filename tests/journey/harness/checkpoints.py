"""Parse JSONL checkpoint file emitted by Claude inside an act session.

New schema per line (single event per phase):
  {"phase": <int>, "status": "passed"|"failed", "notes": "<optional>"}

Legacy schema (backwards compat, still accepted):
  {"phase": <int>, "status": "started"|"completed"|"failed", "ts": "<ISO 8601>", "notes": "<optional>"}

Malformed lines are logged (caller decides where) and skipped via a parse-error count.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Literal

StatusName = Literal["PASS", "FAIL", "SKIP", "ERROR"]


@dataclasses.dataclass
class PhaseOutcome:
    phase: int
    status: StatusName
    notes: str = ""
    started_ts: str | None = None
    completed_ts: str | None = None


def parse_checkpoint_file(
    path: Path, expected_phases: list[int]
) -> tuple[dict[int, PhaseOutcome], int]:
    """Parse a checkpoint JSONL file and attribute phase outcomes.

    Returns (outcomes, parse_errors_count).

    Behavior (new single-event schema):
      - Phase with status "passed" -> PASS.
      - Phase with status "failed" -> FAIL with notes from event.
      - Expected phase with no events -> SKIP "phase not attempted (likely upstream failure)".
      - When parse_errors > 0, SKIP-attributed phases get an extra corruption hint.

    Backwards compat (legacy started/completed schema):
      - Phase with started + completed -> PASS.
      - Phase with started + failed -> FAIL.
      - Phase with started only -> FAIL "phase incomplete (no completion event)".
    """
    events: dict[int, list[dict]] = {}
    parse_errors = 0

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            phase = obj.get("phase")
            if not isinstance(phase, int):
                parse_errors += 1
                continue
            events.setdefault(phase, []).append(obj)

    outcomes: dict[int, PhaseOutcome] = {}
    for phase in expected_phases:
        phase_events = events.get(phase, [])
        if not phase_events:
            note = "phase not attempted (likely upstream failure)"
            if parse_errors > 0:
                note += " (may be checkpoint corruption, check transcripts)"
            outcomes[phase] = PhaseOutcome(phase=phase, status="SKIP", notes=note)
            continue

        passed_event = next((e for e in phase_events if e.get("status") == "passed"), None)
        failed_event = next((e for e in phase_events if e.get("status") == "failed"), None)

        started = next((e for e in phase_events if e.get("status") == "started"), None)
        completed = next((e for e in phase_events if e.get("status") == "completed"), None)

        if passed_event:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="PASS",
                notes=passed_event.get("notes", ""),
            )
        elif failed_event:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes=failed_event.get("notes", "explicit failed status"),
                started_ts=started.get("ts") if started else None,
                completed_ts=failed_event.get("ts"),
            )
        elif started and completed:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="PASS",
                notes=completed.get("notes", ""),
                started_ts=started.get("ts"),
                completed_ts=completed.get("ts"),
            )
        elif started and not completed:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes="phase incomplete (no completion event)",
                started_ts=started.get("ts"),
            )
        else:
            outcomes[phase] = PhaseOutcome(
                phase=phase,
                status="FAIL",
                notes="unexpected event sequence (no started)",
            )

    return outcomes, parse_errors
