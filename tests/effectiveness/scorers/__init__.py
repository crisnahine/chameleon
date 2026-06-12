"""Scorer registry.

SCORERS maps per-cell scorer names to callables; judge_panel is task-level
(pairwise across arms) so it is registered as a name (PANEL_SCORER) but run
by the runner's panel phase, never per-cell.
"""

from __future__ import annotations

from tests.effectiveness.scorers.convention import score as _convention
from tests.effectiveness.scorers.cost import score as _cost
from tests.effectiveness.scorers.crossfile import score as _crossfile
from tests.effectiveness.scorers.duplication import score as _duplication
from tests.effectiveness.scorers.verification import score as _verification

PANEL_SCORER = "judge_panel"

SCORERS = {
    "convention": _convention,
    "crossfile": _crossfile,
    "duplication": _duplication,
    "verification": _verification,
    "cost": _cost,
}
