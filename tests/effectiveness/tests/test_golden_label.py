"""Golden-set sampler + kappa gate CLI (golden_label.py).

Fixture results dirs mirror the real stored-run layout (run.json with
"cells" + "panel" lists, diffs/<task>__<arm>__r<rep>.patch), discovered
from effectiveness_20260615T175635Z. Expected kappa values are
hand-computed independently of stats.py. No test spawns claude.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.effectiveness import golden_label

# Fixture diffs deliberately avoid the arm names ("off"/"shadow") so the
# no-leak test can assert the blinded sheet never mentions an arm.
_TASKS_RUN1 = [
    ("t3-ts-dup-alpha", "shadow", "diff-content-alpha-ctl", "diff-content-alpha-trt"),
    ("t3-ts-dup-beta", "off", "diff-content-beta-ctl", "diff-content-beta-trt"),
    ("t3-ts-dup-gamma", "shadow", "diff-content-gamma-ctl", "diff-content-gamma-trt"),
    ("t3-rb-dup-delta", "tie", "diff-content-delta-ctl", "diff-content-delta-trt"),
]
_TASKS_RUN2 = [
    # Same task id as run1 on purpose: pair ids must disambiguate by run.
    ("t3-ts-dup-alpha", "off", "diff-content-alpha2-ctl", "diff-content-alpha2-trt"),
    ("t3-ts-dup-epsilon", "shadow", "diff-content-eps-ctl", "diff-content-eps-trt"),
]


def _write_run(root: Path, run_id: str, tasks) -> Path:
    """One synthetic results dir in the real shape (cells + panel + diffs/)."""
    run_dir = root / run_id
    (run_dir / "diffs").mkdir(parents=True)
    cells, panel = [], []
    for task_id, winner, diff_off, diff_shadow in tasks:
        for arm, diff in (("off", diff_off), ("shadow", diff_shadow)):
            cells.append(
                {
                    "task_id": task_id,
                    "category": "duplication",
                    "fixture": "env-ts",
                    "arm": arm,
                    "repeat": 1,
                    "status": "ok",
                    "reason": None,
                    "session": {
                        "session_id": "s",
                        "cost_usd": 0.1,
                        "wall_seconds": 1.0,
                        "returncode": 0,
                        "transcript": "t.txt",
                        "baseline_sha": "sha",
                        "model": "sonnet",
                    },
                    "scores": {},
                }
            )
            (run_dir / "diffs" / f"{task_id}__{arm}__r1.patch").write_text(diff, encoding="utf-8")
        votes_off = 3 if winner == "off" else 0
        panel.append(
            {
                "task_id": task_id,
                "pair": ["off", "shadow"],
                "panel_winner": winner,
                "panel_votes_total": 3,
                "panel_votes_valid": 3,
                "panel_votes_for_off": votes_off,
                "panel_votes_for_shadow": 3 - votes_off,
                "panel_cost_usd": 0.3,
            }
        )
    # One unscored panel row (real runs can contain these); must be skipped.
    panel.append({"task_id": "t3-ts-dup-unscored", "pair": ["off", "shadow"], "unscored": "x"})
    doc = {
        "run_id": run_id,
        "tier": "full",
        "arms": ["off", "shadow"],
        "model": "sonnet",
        "toggle": None,
        "cells": cells,
        "panel": panel,
        "aggregates": {},
        "baseline_deltas": [],
        "errors": 0,
        "total_cost_usd": 1.0,
    }
    (run_dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    return run_dir


def _sample(tmp_path: Path, out_dir: Path, n: int = 6, seed: int = 7) -> int:
    r1 = (
        _write_run(tmp_path, "eff_run1", _TASKS_RUN1)
        if not (tmp_path / "eff_run1").exists()
        else tmp_path / "eff_run1"
    )
    r2 = (
        _write_run(tmp_path, "eff_run2", _TASKS_RUN2)
        if not (tmp_path / "eff_run2").exists()
        else tmp_path / "eff_run2"
    )
    return golden_label.main(
        [
            "sample",
            "--runs",
            str(r1),
            str(r2),
            "--n",
            str(n),
            "--seed",
            str(seed),
            "--out",
            str(out_dir / "pairs.jsonl"),
        ]
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_sample_writes_n_pairs_sidecar_and_template(tmp_path):
    out = tmp_path / "golden"
    assert _sample(tmp_path, out, n=4) == 0

    pairs = _lines(out / "pairs.jsonl")
    assert len(pairs) == 4
    for row in pairs:
        assert set(row) == {"pair_id", "task_id", "side_a", "side_b"}
        assert row["side_a"].startswith("diff-content-")
        assert row["side_b"].startswith("diff-content-")

    sidecar = _lines(out / "panel_verdicts.jsonl")
    assert [r["pair_id"] for r in sidecar] == [r["pair_id"] for r in pairs]
    assert all(r["panel_winner"] in ("A", "B", "tie") for r in sidecar)

    template = _lines(out / "labels.jsonl.example")
    assert [r["pair_id"] for r in template] == [r["pair_id"] for r in pairs]
    assert all(r["winner"] == "" for r in template)


def test_sample_is_seed_stable_and_order_randomized(tmp_path):
    out1, out2 = tmp_path / "g1", tmp_path / "g2"
    assert _sample(tmp_path, out1, n=6, seed=7) == 0
    assert _sample(tmp_path, out2, n=6, seed=7) == 0
    for name in ("pairs.jsonl", "panel_verdicts.jsonl", "labels.jsonl.example"):
        assert (out1 / name).read_bytes() == (out2 / name).read_bytes()

    # The blinding actually randomizes: with 6 pairs, both side orders appear.
    sidecar = _lines(out1 / "panel_verdicts.jsonl")
    assert {r["side_a_arm"] for r in sidecar} == {"off", "shadow"}

    # Sidecar A/B verdicts are consistent with the recorded arm mapping.
    winners = {t[0]: t[1] for t in _TASKS_RUN1}
    for row in sidecar:
        if row["run_id"] != "eff_run1":
            continue
        arm_winner = winners[row["task_id"]]
        if arm_winner == "tie":
            assert row["panel_winner"] == "tie"
        elif arm_winner == row["side_a_arm"]:
            assert row["panel_winner"] == "A"
        else:
            assert row["panel_winner"] == "B"


def test_sample_never_leaks_panel_verdict_or_arms(tmp_path):
    out = tmp_path / "golden"
    assert _sample(tmp_path, out, n=6) == 0
    raw = (out / "pairs.jsonl").read_text(encoding="utf-8")
    assert "panel_winner" not in raw
    assert "off" not in raw and "shadow" not in raw
    raw_template = (out / "labels.jsonl.example").read_text(encoding="utf-8")
    assert "panel_winner" not in raw_template


def test_sample_caps_n_at_available_and_warns(tmp_path, capsys):
    out = tmp_path / "golden"
    assert _sample(tmp_path, out, n=40) == 0
    assert len(_lines(out / "pairs.jsonl")) == 6  # 4 + 2 judged pairs exist
    err = capsys.readouterr().err
    assert "6" in err and "40" in err


def test_sample_refuses_to_orphan_existing_labels(tmp_path, capsys):
    out = tmp_path / "golden"
    out.mkdir()
    (out / "labels.jsonl").write_text('{"pair_id": "x", "winner": "A"}\n', encoding="utf-8")
    assert _sample(tmp_path, out, n=4) == 2
    assert "labels.jsonl" in capsys.readouterr().err
    assert not (out / "pairs.jsonl").exists()


def _write_kappa_inputs(root: Path, panel_winners, human_winners) -> tuple[Path, Path, Path]:
    """Hand-built pairs/panel/labels files; None in human_winners = unlabeled."""
    pairs, panel, labels = (
        root / "pairs.jsonl",
        root / "panel_verdicts.jsonl",
        root / "labels.jsonl",
    )
    root.mkdir(parents=True, exist_ok=True)
    with pairs.open("w") as fp, panel.open("w") as fv, labels.open("w") as fl:
        for i, (pw, hw) in enumerate(zip(panel_winners, human_winners, strict=True)):
            pid = f"pair{i}"
            fp.write(
                json.dumps({"pair_id": pid, "task_id": f"t{i}", "side_a": "a", "side_b": "b"})
                + "\n"
            )
            fv.write(json.dumps({"pair_id": pid, "panel_winner": pw}) + "\n")
            if hw is not None:
                fl.write(json.dumps({"pair_id": pid, "winner": hw}) + "\n")
    return pairs, labels, panel


def _run_kappa(pairs: Path, labels: Path, panel: Path) -> int:
    return golden_label.main(
        ["kappa", "--pairs", str(pairs), "--labels", str(labels), "--panel", str(panel)]
    )


def test_kappa_matches_hand_computed_value(tmp_path, capsys):
    # human [A,A,B,tie] vs panel [A,B,B,tie]: observed 3/4; expected
    # 0.5*0.25 + 0.25*0.5 + 0.25*0.25 = 0.3125; kappa = 0.4375/0.6875 = 0.63636.
    pairs, labels, panel = _write_kappa_inputs(
        tmp_path / "g", ["A", "B", "B", "tie"], ["A", "A", "B", "tie"]
    )
    assert _run_kappa(pairs, labels, panel) == 0
    out = capsys.readouterr().out
    assert "kappa=0.636 n=4 citable=yes (gate 0.6, per stats.py)" in out


def test_kappa_below_gate_is_not_citable(tmp_path, capsys):
    # human [A,B,A,B] vs panel [A,B,B,A]: observed 0.5 == expected 0.5 -> kappa 0.
    pairs, labels, panel = _write_kappa_inputs(
        tmp_path / "g", ["A", "B", "B", "A"], ["A", "B", "A", "B"]
    )
    assert _run_kappa(pairs, labels, panel) == 0
    assert "kappa=0.000 n=4 citable=no (gate 0.6, per stats.py)" in capsys.readouterr().out


def test_kappa_partial_labels_reports_coverage(tmp_path, capsys):
    # Only 2 of 4 labeled; the labeled subset agrees fully -> kappa 1.0, n=2.
    pairs, labels, panel = _write_kappa_inputs(
        tmp_path / "g", ["A", "B", "B", "tie"], ["A", None, "B", None]
    )
    assert _run_kappa(pairs, labels, panel) == 0
    out = capsys.readouterr().out
    assert "2/4" in out
    assert "kappa=1.000 n=2 citable=yes (gate 0.6, per stats.py)" in out


def test_kappa_ignores_invalid_winner_values(tmp_path, capsys):
    # An invalid winner is unlabeled, lowercase "a"/"TIE" normalize fine.
    pairs, labels, panel = _write_kappa_inputs(
        tmp_path / "g", ["A", "B", "tie"], ["a", "maybe", "TIE"]
    )
    assert _run_kappa(pairs, labels, panel) == 0
    out = capsys.readouterr().out
    assert "2/3" in out
    assert "kappa=1.000 n=2 citable=yes (gate 0.6, per stats.py)" in out


def test_kappa_with_no_labels_reports_zero_coverage(tmp_path, capsys):
    pairs, labels, panel = _write_kappa_inputs(tmp_path / "g", ["A", "B"], [None, None])
    assert _run_kappa(pairs, labels, panel) == 0
    out = capsys.readouterr().out
    assert "0/2" in out
    assert "kappa=n/a n=0 citable=no (gate 0.6, per stats.py)" in out


def test_kappa_mode_never_writes(tmp_path):
    root = tmp_path / "g"
    pairs, labels, panel = _write_kappa_inputs(root, ["A", "B"], ["A", "B"])
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert _run_kappa(pairs, labels, panel) == 0
    after = {p.name: p.read_bytes() for p in root.iterdir()}
    assert after == before
