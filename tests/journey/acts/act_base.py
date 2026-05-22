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
At the END of each phase (after running all its steps), emit a checkpoint by running this Bash command:

  echo '{"phase": <N>, "status": "passed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

If an assertion fails inside the phase, emit:

  echo '{"phase": <N>, "status": "failed", "notes": "what failed"}' >> "$CHAMELEON_JOURNEY_CHECKPOINT"

ONE checkpoint per phase, after the phase completes (or fails). Do NOT emit a "started" event. Emit each checkpoint as a SINGLE LINE outside any code fence.
"""


def checkpoint_preamble() -> str:
    return _CHECKPOINT_PREAMBLE


def build_act_prompt(body: str) -> str:
    return checkpoint_preamble() + "\n\n" + body
