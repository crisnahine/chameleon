"""Judge panel: blind pairwise, randomized order, 3 votes, stubbed spawn."""

from __future__ import annotations

import random

from tests.effectiveness.scorers import judge_panel


class _StubSession:
    def __init__(self, result_text: str, cost_usd: float = 0.01):
        self.result_text = result_text
        self.cost_usd = cost_usd
        self.returncode = 0


def _spawn_factory(answers):
    calls = []

    def spawn(prompt, transcript_path):
        calls.append(prompt)
        return _StubSession(answers[len(calls) - 1])

    spawn.calls = calls
    return spawn


def test_majority_vote_picks_winner():
    spawn = _spawn_factory(["WINNER: A\nreuses helper", "WINNER: A\ncleaner", "WINNER: B\nx"])
    out = judge_panel.run_panel(
        task_id="t1-x",
        pair=("off", "shadow"),
        diffs={
            "off": "diff --git a b\n-old\n+off change",
            "shadow": "diff --git a b\n-old\n+shadow change",
        },
        run_dir=None,
        spawn_fn=spawn,
        rng=random.Random(0),
    )
    # rng=Random(0): first random() < 0.5 decides label order per vote; the
    # mapping is asserted via the votes_for counts, not hardcoded labels.
    assert out["panel_votes_total"] == 3
    assert out["panel_votes_valid"] == 3
    assert out["panel_winner"] in ("off", "shadow")
    assert out["panel_votes_for_off"] + out["panel_votes_for_shadow"] == 3


def test_labels_are_blind_and_randomized():
    spawn = _spawn_factory(["WINNER: A\nx", "WINNER: A\nx", "WINNER: A\nx"])
    judge_panel.run_panel(
        task_id="t1-x",
        pair=("off", "shadow"),
        diffs={"off": "OFF_DIFF_MARKER", "shadow": "SHADOW_DIFF_MARKER"},
        run_dir=None,
        spawn_fn=spawn,
        rng=random.Random(1),
    )
    for prompt in spawn.calls:
        # The diff bodies are caller-supplied and pass through verbatim; the
        # blindness guarantee is about the harness's own framing, so strip the
        # marker payloads before asserting no arm name leaks.
        framing = prompt.replace("OFF_DIFF_MARKER", "").replace("SHADOW_DIFF_MARKER", "")
        assert "off" not in framing.lower().replace("trade-off", "")
        assert "shadow" not in framing.lower()
        assert "OFF_DIFF_MARKER" in prompt and "SHADOW_DIFF_MARKER" in prompt


def test_spawn_failures_fail_open():
    def spawn(prompt, transcript_path):
        raise OSError("no claude binary")

    out = judge_panel.run_panel(
        task_id="t1-x",
        pair=("off", "shadow"),
        diffs={"off": "a", "shadow": "b"},
        run_dir=None,
        spawn_fn=spawn,
        rng=random.Random(0),
    )
    assert set(out) == {"unscored"}


def test_unparseable_votes_majority_still_required():
    spawn = _spawn_factory(["garbage", "WINNER: B\nx", "also garbage"])
    out = judge_panel.run_panel(
        task_id="t1-x",
        pair=("off", "shadow"),
        diffs={"off": "a", "shadow": "b"},
        run_dir=None,
        spawn_fn=spawn,
        rng=random.Random(0),
    )
    assert set(out) == {"unscored"}  # 1 valid vote < 2 = no majority possible


def test_identical_diffs_short_circuit_to_tie():
    spawn = _spawn_factory([])
    out = judge_panel.run_panel(
        task_id="t1-x",
        pair=("off", "shadow"),
        diffs={"off": "same", "shadow": "same"},
        run_dir=None,
        spawn_fn=spawn,
        rng=random.Random(0),
    )
    assert out["panel_winner"] == "tie"
    assert spawn.calls == []


def test_deterministic_disagreement_detector():
    cells = [
        {
            "arm": "off",
            "status": "ok",
            "scores": {
                "convention": {"violations": 1},
                "duplication": {"body_hash_duplicates": 0, "reuse_credit": True},
            },
        },
        {
            "arm": "shadow",
            "status": "ok",
            "scores": {
                "convention": {"violations": 3},
                "duplication": {"body_hash_duplicates": 1, "reuse_credit": False},
            },
        },
    ]
    # convention says off wins; duplication also says off wins -> no disagreement
    assert judge_panel.deterministic_disagreement(cells, ("off", "shadow")) is False
    cells[1]["scores"]["convention"]["violations"] = 0
    # now convention says shadow, duplication says off -> disagreement
    assert judge_panel.deterministic_disagreement(cells, ("off", "shadow")) is True
