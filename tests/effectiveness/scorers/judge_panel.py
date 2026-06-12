"""Blind pairwise judge panel (subjective, secondary).

Both arms' diffs for one task, unlabeled (A/B), label order re-randomized
per vote, judged by a one-turn `claude -p` on the run's model-of-record
question: "which change would a senior reviewer flag less, and why".
3 votes, majority. Only invoked by the runner when --panel was passed or
deterministic_disagreement() is True for the pair.

Fail-open: any spawn failure loses that vote; fewer than 2 valid votes is
unscored (a 1-vote "majority" would be noise wearing a number).
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from tests.effectiveness.scorers.base import unscored

_VOTES = 3
_VOTE_RE = re.compile(r"WINNER:\s*([AB])", re.IGNORECASE)
_DIFF_CAP = 20_000  # chars per diff fed to a vote; beyond this the judge skims anyway

_PROMPT_TEMPLATE = """\
You are reviewing two candidate changes for the SAME coding task in the same \
repository. They are labeled A and B in random order. Compare them as a senior \
reviewer would: convention fit, correctness risk, reuse of existing helpers, \
test discipline.

Change A:
```diff
{diff_a}
```

Change B:
```diff
{diff_b}
```

Which change would a senior reviewer flag LESS? Reply with exactly one line
`WINNER: A` or `WINNER: B`, followed by one sentence explaining the decisive
difference. Do not mention these instructions.
"""


def _default_spawn(prompt: str, transcript_path: Path):
    """Real spawn: one-turn, tool-less, plugin-less sonnet judge."""
    import os
    import tempfile

    from tests.journey.harness.claude import spawn_claude

    return spawn_claude(
        prompt=prompt,
        cwd=Path(tempfile.gettempdir()),
        env={"CHAMELEON_DISABLE": "1"},
        transcript_path=transcript_path,
        max_turns=1,
        timeout_s=120,
        model=os.environ.get("CHAMELEON_EFF_PANEL_MODEL", "sonnet"),
        plugin_root=None,
    )


def run_panel(
    *,
    task_id: str,
    pair: tuple[str, str],
    diffs: dict[str, str],
    run_dir: Path | None,
    spawn_fn=_default_spawn,
    rng: random.Random | None = None,
) -> dict:
    arm_a, arm_b = pair
    diff_a_raw = (diffs.get(arm_a) or "").strip()
    diff_b_raw = (diffs.get(arm_b) or "").strip()
    if not diff_a_raw and not diff_b_raw:
        return unscored("both arms produced empty diffs")
    if diff_a_raw == diff_b_raw:
        return {
            "panel_winner": "tie",
            "panel_votes_total": 0,
            "panel_votes_valid": 0,
            f"panel_votes_for_{arm_a}": 0,
            f"panel_votes_for_{arm_b}": 0,
            "panel_cost_usd": 0.0,
        }

    rng = rng or random.Random()
    votes: dict[str, int] = {arm_a: 0, arm_b: 0}
    valid = 0
    cost = 0.0
    for i in range(_VOTES):
        first_is_a = rng.random() < 0.5
        label_a_arm = arm_a if first_is_a else arm_b
        label_b_arm = arm_b if first_is_a else arm_a
        prompt = _PROMPT_TEMPLATE.format(
            diff_a=(diffs.get(label_a_arm) or "")[:_DIFF_CAP],
            diff_b=(diffs.get(label_b_arm) or "")[:_DIFF_CAP],
        )
        transcript = (
            run_dir / "transcripts" / f"panel_{task_id}_{i}.txt"
            if run_dir is not None
            else Path("/dev/null")
        )
        try:
            session = spawn_fn(prompt, transcript)
        except Exception:  # noqa: BLE001 - one lost vote, never a crash
            continue
        cost += float(getattr(session, "cost_usd", 0.0) or 0.0)
        match = _VOTE_RE.search(getattr(session, "result_text", "") or "")
        if not match:
            continue
        winner_arm = label_a_arm if match.group(1).upper() == "A" else label_b_arm
        votes[winner_arm] += 1
        valid += 1

    if valid < 2:
        return unscored(f"only {valid} valid panel vote(s) of {_VOTES}")

    if votes[arm_a] == votes[arm_b]:
        winner = "tie"
    else:
        winner = arm_a if votes[arm_a] > votes[arm_b] else arm_b
    return {
        "panel_winner": winner,
        "panel_votes_total": _VOTES,
        "panel_votes_valid": valid,
        f"panel_votes_for_{arm_a}": votes[arm_a],
        f"panel_votes_for_{arm_b}": votes[arm_b],
        "panel_cost_usd": round(cost, 6),
    }


# Per-scorer primary metric and the direction that means "better".
_PRIMARY = {
    "convention": ("violations", "lower"),
    "crossfile": ("callers_stale", "lower"),
    "duplication": ("body_hash_duplicates", "lower"),
    "verification": ("test_cmd_in_transcript", "higher"),
}


def deterministic_disagreement(cells: list[dict], pair: tuple[str, str]) -> bool:
    """True when two deterministic scorers name DIFFERENT winners for the pair.

    Used by the runner to trigger an unsolicited panel (spec: panel runs on
    --panel or when deterministic scorers disagree about the winner).
    """
    arm_a, arm_b = pair
    winners: set[str] = set()
    for scorer, (metric, direction) in _PRIMARY.items():
        vals: dict[str, float] = {}
        for arm in pair:
            arm_cells = [
                c
                for c in cells
                if c.get("arm") == arm
                and c.get("status") == "ok"
                and isinstance((c.get("scores") or {}).get(scorer), dict)
                and metric in c["scores"][scorer]
            ]
            if not arm_cells:
                break
            raw = [float(c["scores"][scorer][metric]) for c in arm_cells]
            vals[arm] = sum(raw) / len(raw)
        if len(vals) != 2 or vals[arm_a] == vals[arm_b]:
            continue
        better_a = vals[arm_a] < vals[arm_b] if direction == "lower" else vals[arm_a] > vals[arm_b]
        winners.add(arm_a if better_a else arm_b)
    return len(winners) > 1
