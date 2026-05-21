"""Common types + helpers used by all act modules."""
from __future__ import annotations

import dataclasses
from typing import Any

from tests.journey.harness.checkpoints import PhaseOutcome


@dataclasses.dataclass
class ActResult:
    act_id: str
    cost_usd: float
    phase_outcomes: list[PhaseOutcome]
    checkpoint_parse_errors: int = 0
    notes: str = ""


_CHECKPOINT_PREAMBLE = """\
At each phase boundary, emit a checkpoint by running this Bash command:

  echo '{"phase": <N>, "status": "started", "ts": "'$(date -u +%FT%TZ)'"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

Then run the phase steps. When the phase succeeds, emit:

  echo '{"phase": <N>, "status": "completed", "ts": "'$(date -u +%FT%TZ)'"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

If an assertion fails inside the phase, emit:

  echo '{"phase": <N>, "status": "failed", "ts": "'$(date -u +%FT%TZ)'", "notes": "what failed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

Emit each checkpoint as a SINGLE LINE outside any code fence. Never wrap them in markdown.
"""


def checkpoint_preamble() -> str:
    return _CHECKPOINT_PREAMBLE


def build_act_prompt(body: str) -> str:
    return checkpoint_preamble() + "\n\n" + body
