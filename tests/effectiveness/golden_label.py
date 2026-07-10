"""Golden-set tooling for LLM-judge calibration (sample + kappa CLI).

The judge panel's pairwise preference numbers are citable only once the
panel agrees with a HUMAN-labeled golden set at Cohen's kappa >= 0.6 (the
gate stats.cohens_kappa documents). This CLI produces the blinded labeling
sheet from stored effectiveness runs and computes the kappa once labels
exist. Stdlib only.

Usage:
  PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.golden_label sample \
      --runs tests/effectiveness/results/effectiveness_<ts> [...] \
      --n 40 --seed 7 --out tests/effectiveness/golden/pairs.jsonl
  PYTHONPATH=. mcp/.venv/bin/python -m tests.effectiveness.golden_label kappa \
      --pairs tests/effectiveness/golden/pairs.jsonl \
      --labels tests/effectiveness/golden/labels.jsonl \
      --panel tests/effectiveness/golden/panel_verdicts.jsonl

labels.jsonl MUST be filled by a human (see golden/README.md): a model
labeling it makes the kappa judge-vs-judge and voids the gate. This tool
therefore never writes labels.jsonl, only labels.jsonl.example.

Exit codes: 0 = ok (kappa below the gate still exits 0; the verdict line is
the report), 2 = usage/input error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Chars of diff shown per side; the panel judged at this same cap
# (judge_panel._DIFF_CAP), so the human sees the evidence the judge saw.
_DIFF_CAP = 20_000

_VALID_LABELS = {"A", "B", "tie"}


def _warn(msg: str) -> None:
    print(f"golden_label: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- sample


def _pair_id(run_id: str, task_id: str, arm_a: str, arm_b: str) -> str:
    """Opaque, deterministic id: must not reveal the arm names to the labeler."""
    digest = hashlib.sha1(f"{run_id}|{task_id}|{arm_a}|{arm_b}".encode()).hexdigest()
    return digest[:12]


def _best_diff_path(run_dir: Path, cells: list[dict], task_id: str, arm: str) -> Path | None:
    """Diff of the highest ok repeat: the one the panel judged (last repeat wins)."""
    repeats = [
        int(c.get("repeat") or 0)
        for c in cells
        if c.get("task_id") == task_id and c.get("arm") == arm and c.get("status") == "ok"
    ]
    if not repeats:
        return None
    return run_dir / "diffs" / f"{task_id}__{arm}__r{max(repeats)}.patch"


def _load_candidates(run_dirs: list[Path]) -> list[dict]:
    """Every panel-judged pairwise comparison in the given runs, with both diffs."""
    candidates: list[dict] = []
    for run_dir in run_dirs:
        run_json = run_dir / "run.json"
        if not run_json.is_file():
            _warn(f"skipping {run_dir}: no run.json")
            continue
        doc = json.loads(run_json.read_text(encoding="utf-8"))
        run_id = doc.get("run_id") or run_dir.name
        cells = doc.get("cells") or []
        for row in doc.get("panel") or []:
            winner = row.get("panel_winner")
            pair = row.get("pair") or []
            task_id = row.get("task_id")
            if winner is None or len(pair) != 2 or not task_id:
                continue  # unscored or malformed panel row
            arm_a, arm_b = pair
            if winner not in (arm_a, arm_b, "tie"):
                _warn(f"skipping {run_id}:{task_id}: winner {winner!r} not in pair {pair}")
                continue
            diffs: dict[str, str] = {}
            for arm in (arm_a, arm_b):
                path = _best_diff_path(run_dir, cells, task_id, arm)
                if path is None or not path.is_file():
                    _warn(f"skipping {run_id}:{task_id}: no diff for arm {arm}")
                    break
                diffs[arm] = path.read_text(encoding="utf-8", errors="replace")[:_DIFF_CAP]
            if len(diffs) != 2:
                continue
            candidates.append(
                {
                    "pair_id": _pair_id(run_id, task_id, arm_a, arm_b),
                    "run_id": run_id,
                    "task_id": task_id,
                    "arm_a": arm_a,
                    "arm_b": arm_b,
                    "winner_arm": winner,
                    "diffs": diffs,
                }
            )
    return candidates


def _cmd_sample(args: argparse.Namespace) -> int:
    out_path = Path(args.out)
    out_dir = out_path.parent
    labels_path = out_dir / "labels.jsonl"
    if labels_path.exists() and not args.force:
        _warn(
            f"{labels_path} already exists; re-sampling would orphan human labels. "
            "Move it away (or pass --force) first."
        )
        return 2

    candidates = _load_candidates([Path(r) for r in args.runs])
    if not candidates:
        _warn("no panel-judged pairs found in the given runs")
        return 2
    candidates.sort(key=lambda c: c["pair_id"])
    if len(candidates) < args.n:
        _warn(
            f"only {len(candidates)} panel-judged pairs available "
            f"(requested {args.n}); sampling all of them"
        )

    rng = random.Random(args.seed)
    sampled = rng.sample(candidates, min(args.n, len(candidates)))
    sampled.sort(key=lambda c: c["pair_id"])

    pair_rows, sidecar_rows, template_rows = [], [], []
    for cand in sampled:
        # Blind the side order per pair; only the sidecar keeps the mapping.
        flipped = rng.random() < 0.5
        side_a_arm = cand["arm_b"] if flipped else cand["arm_a"]
        side_b_arm = cand["arm_a"] if flipped else cand["arm_b"]
        if cand["winner_arm"] == "tie":
            blinded_winner = "tie"
        else:
            blinded_winner = "A" if cand["winner_arm"] == side_a_arm else "B"
        pair_rows.append(
            {
                "pair_id": cand["pair_id"],
                "task_id": cand["task_id"],
                "side_a": cand["diffs"][side_a_arm],
                "side_b": cand["diffs"][side_b_arm],
            }
        )
        sidecar_rows.append(
            {
                "pair_id": cand["pair_id"],
                "run_id": cand["run_id"],
                "task_id": cand["task_id"],
                "panel_winner": blinded_winner,
                "side_a_arm": side_a_arm,
                "side_b_arm": side_b_arm,
            }
        )
        template_rows.append({"pair_id": cand["pair_id"], "winner": ""})

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_path, pair_rows)
    _write_jsonl(out_dir / "panel_verdicts.jsonl", sidecar_rows)
    _write_jsonl(out_dir / "labels.jsonl.example", template_rows)
    _warn(
        f"sampled {len(pair_rows)} pairs -> {out_path} "
        f"(sidecar: panel_verdicts.jsonl, template: labels.jsonl.example)"
    )
    return 0


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# ----------------------------------------------------------------- kappa


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _normalize_label(raw: object) -> str | None:
    text = str(raw or "").strip()
    if text.upper() in ("A", "B"):
        return text.upper()
    if text.lower() == "tie":
        return "tie"
    return None


def _cmd_kappa(args: argparse.Namespace) -> int:
    pair_ids = [r["pair_id"] for r in _read_jsonl(Path(args.pairs))]
    panel = {r["pair_id"]: r.get("panel_winner") for r in _read_jsonl(Path(args.panel))}

    human: dict[str, str] = {}
    for row in _read_jsonl(Path(args.labels)):
        pid = row.get("pair_id")
        if pid not in panel:
            _warn(f"label for unknown pair_id {pid!r} ignored")
            continue
        label = _normalize_label(row.get("winner"))
        if label is None:
            _warn(f"pair {pid}: winner {row.get('winner')!r} is not A/B/tie; treated as unlabeled")
            continue
        human[pid] = label  # last occurrence wins, so appended corrections stick

    labeled = [pid for pid in pair_ids if pid in human and panel.get(pid) in _VALID_LABELS]
    n = len(labeled)
    print(f"labels cover {n}/{len(pair_ids)} sampled pairs ({len(pair_ids) - n} unlabeled)")
    if n == 0:
        print("kappa=n/a n=0 citable=no (gate 0.6, per stats.py)")
        return 0

    from tests.effectiveness.stats import cohens_kappa

    kappa = cohens_kappa([human[p] for p in labeled], [panel[p] for p in labeled])
    citable = "yes" if kappa >= 0.6 else "no"
    print(f"kappa={kappa:.3f} n={n} citable={citable} (gate 0.6, per stats.py)")
    return 0


# ------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golden_label", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="Sample a blinded labeling sheet from stored runs")
    p_sample.add_argument("--runs", nargs="+", required=True, help="Results dirs with run.json")
    p_sample.add_argument("--n", type=int, default=40, help="Pairs to sample (default 40)")
    p_sample.add_argument("--seed", type=int, default=7, help="RNG seed (default 7)")
    p_sample.add_argument("--out", required=True, help="Output pairs.jsonl path")
    p_sample.add_argument(
        "--force", action="store_true", help="Re-sample even if labels.jsonl already exists"
    )
    p_sample.set_defaults(func=_cmd_sample)

    p_kappa = sub.add_parser("kappa", help="Human-vs-panel Cohen's kappa (read-only)")
    p_kappa.add_argument("--pairs", required=True, help="pairs.jsonl from sample")
    p_kappa.add_argument("--labels", required=True, help="Human labels.jsonl")
    p_kappa.add_argument("--panel", required=True, help="panel_verdicts.jsonl sidecar")
    p_kappa.set_defaults(func=_cmd_kappa)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
