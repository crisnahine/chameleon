#!/usr/bin/env python3
"""Cell ledger for the full-matrix test campaign. Dev-only tooling.

The campaign spans more cells than one session can hold, so the ledger on disk
is the source of truth for what has been executed -- not the transcript. Every
resume reads it to find the first unfinished cell.

A cell is only PASS with evidence attached. `status` refuses to count a PASS
that carries no evidence string, because an unevidenced pass is
indistinguishable from an untested cell, which is the exact failure the
campaign is designed to prevent.

    qa-matrix.py status                    progress by surface and column
    qa-matrix.py next [--column C5] [-n 20]  the next PENDING cells to execute
    qa-matrix.py mark <cell_id> <status> --evidence ... [--correctness ...]
                                             [--effectiveness ...]
    qa-matrix.py show <cell_id>
    qa-matrix.py audit                     integrity check of the ledger itself

`--ledger PATH` points every command at a different cell file. An independent
re-verification keeps its own ledger so its verdicts can never be confused with,
or silently merged into, the run it is auditing.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "tests" / "matrix" / "cells.jsonl"

# PASS and N/A-ASSERTED are positive verdicts and both require evidence: an
# asserted n/a ("this rule correctly does NOT fire here") is a real observation,
# not an exemption.
EVIDENCE_REQUIRED = {"PASS", "NA-ASSERTED"}
VALID = {"PENDING", "PASS", "FAIL", "NA-ASSERTED", "BLOCKED"}


def _ledger(args) -> Path:
    return Path(args.ledger) if getattr(args, "ledger", None) else LEDGER


def load(path: Path = LEDGER) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def save(rows: list[dict], path: Path = LEDGER) -> None:
    # Rewrite whole-file: the ledger is small enough that an atomic replace is
    # cheaper than tracking offsets, and a torn ledger would strand the campaign.
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(path)


def cmd_status(args) -> int:
    rows = load(_ledger(args))
    by_surface = defaultdict(Counter)
    by_column = defaultdict(Counter)
    for r in rows:
        by_surface[r["surface"]][r["status"]] += 1
        by_column[r["column"]][r["status"]] += 1

    def line(label: str, c: Counter) -> str:
        done = c["PASS"] + c["NA-ASSERTED"]
        total = sum(c.values())
        pct = (done * 100 // total) if total else 0
        return (
            f"{label:22s}{total:6d}{done:7d}{c['FAIL']:6d}"
            f"{c['BLOCKED']:8d}{c['PENDING']:8d}{pct:6d}%"
        )

    hdr = f"{'':22s}{'cells':>6}{'done':>7}{'fail':>6}{'blocked':>8}{'pending':>8}{'':>6}"
    print(hdr)
    print("-" * len(hdr))
    for k in sorted(by_surface):
        print(line(k, by_surface[k]))
    print("-" * len(hdr))
    for k in sorted(by_column, key=lambda x: int(x[1:])):
        print(line(k, by_column[k]))
    print("-" * len(hdr))
    print(line("TOTAL", Counter(r["status"] for r in rows)))

    unevidenced = [
        r for r in rows if r["status"] in EVIDENCE_REQUIRED and not r["evidence"].strip()
    ]
    if unevidenced:
        print(f"\nINTEGRITY FAILURE: {len(unevidenced)} positive verdicts carry no evidence.")
        for r in unevidenced[:10]:
            print(f"  {r['cell_id']}")
        return 1
    return 0


def cmd_next(args) -> int:
    rows = load(_ledger(args))
    pend = [r for r in rows if r["status"] == "PENDING"]
    if args.column:
        pend = [r for r in pend if r["column"] == args.column]
    if args.surface:
        pend = [r for r in pend if r["surface"] == args.surface]
    for r in pend[: args.n]:
        print(f"{r['cell_id']}\t{r['repo']}\t{r['item_id']}")
    if not pend:
        print("no PENDING cells match")
    return 0


def cmd_mark(args) -> int:
    if args.status not in VALID:
        print(f"invalid status {args.status!r}; valid: {sorted(VALID)}")
        return 2
    if args.status in EVIDENCE_REQUIRED and not (args.evidence or "").strip():
        print(f"REFUSED: {args.status} requires --evidence (an unevidenced pass is untested)")
        return 2
    rows = load(_ledger(args))
    hit = False
    for r in rows:
        if r["cell_id"] == args.cell_id:
            r["status"] = args.status
            if args.evidence:
                r["evidence"] = args.evidence
            if args.correctness:
                r["correctness"] = args.correctness
            if args.effectiveness:
                r["effectiveness"] = args.effectiveness
            hit = True
            break
    if not hit:
        print(f"no such cell: {args.cell_id}")
        return 1
    save(rows, _ledger(args))
    print(f"{args.cell_id} -> {args.status}")
    return 0


def cmd_show(args) -> int:
    for r in load(_ledger(args)):
        if r["cell_id"] == args.cell_id:
            print(json.dumps(r, indent=2))
            return 0
    print(f"no such cell: {args.cell_id}")
    return 1


def cmd_audit(args) -> int:
    rows = load(_ledger(args))
    problems = []
    ids = Counter(r["cell_id"] for r in rows)
    dupes = [k for k, v in ids.items() if v > 1]
    if dupes:
        problems.append(f"{len(dupes)} duplicate cell_ids (first: {dupes[0]})")
    bad = [r for r in rows if r["status"] not in VALID]
    if bad:
        problems.append(f"{len(bad)} rows with an invalid status")
    unevidenced = [
        r for r in rows if r["status"] in EVIDENCE_REQUIRED and not r["evidence"].strip()
    ]
    if unevidenced:
        problems.append(f"{len(unevidenced)} positive verdicts with no evidence")
    cols = {r["column"] for r in rows}
    items = {(r["surface"], r["item_id"]) for r in rows}
    expected = len(items) * len(cols)
    if len(rows) != expected:
        problems.append(f"row count {len(rows)} != items({len(items)}) x columns({len(cols)})")

    if problems:
        print("LEDGER AUDIT FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"ledger OK: {len(items)} items x {len(cols)} columns = {len(rows)} cells")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", help="cell file to operate on (default: the campaign ledger)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)

    p = sub.add_parser("next")
    p.add_argument("--column")
    p.add_argument("--surface")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("mark")
    p.add_argument("cell_id")
    p.add_argument("status")
    p.add_argument("--evidence", default="")
    p.add_argument("--correctness", default="")
    p.add_argument("--effectiveness", default="")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("show")
    p.add_argument("cell_id")
    p.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
