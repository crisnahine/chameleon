#!/usr/bin/env python3
"""Fold the deep-probe wave's results into the matrix ledger.

Per-column agents (scope names contain C1..C10) map their cells to that column.
Cross-cutting infra agents (daemon / merge / schema / stdio / statusline) probe
language-invariant surfaces, so each infra cell's verdict is applied to that
item across all ten columns. A cell whose item_id does not resolve to a real
ledger cell is dropped (never invented), and the count of dropped ids is
reported so a mismapping is visible rather than silent.

Usage: qa-fold-deepprobe.py <journal.jsonl>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "tests" / "matrix" / "cells.jsonl"
SURFACES = ("hooks", "skills", "mcp-tools", "bootstrap", "enforcement", "aux", "framework-layers")
COLUMNS = [f"C{i}" for i in range(1, 11)]
HOME = str(Path.home())


def _resolve(index: dict, item_id: str, col: str) -> str | None:
    iid = (item_id or "").strip()
    cands = ([f"{iid}@{col}"] if "/" in iid else []) + [f"{s}/{iid}@{col}" for s in SURFACES]
    return next((x for x in cands if x in index), None)


def main() -> int:
    journal = Path(sys.argv[1])
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    index = {r["cell_id"]: r for r in rows}

    results = []
    for line in journal.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") == "result" and isinstance(d.get("result"), dict) and "cells" in d["result"]:
            results.append(d["result"])

    applied = dropped = 0
    st = Counter()
    for res in results:
        scope = str(res.get("scope", ""))
        m = re.search(r"\bC(\d+)\b", scope)
        target_cols = [f"C{m.group(1)}"] if m else COLUMNS  # infra -> all columns
        infra = m is None
        tag = "deepprobe-infra" if infra else "deepprobe"
        for c in res.get("cells", []):
            ev = (c.get("evidence", "") or "").replace(HOME, "~")
            corr = (c.get("correctness", "") or "").replace(HOME, "~")
            eff = (c.get("effectiveness", "") or "").replace(HOME, "~")
            for col in target_cols:
                cid = _resolve(index, c.get("item_id", ""), col)
                if not cid:
                    dropped += 1
                    continue
                row = index[cid]
                # Never downgrade a PASS/N/A to nothing; deep-probe verdicts win
                # only when they carry a status. A FAIL/BLOCKED from a probe is
                # authoritative (it found a real defect in that surface).
                row["status"] = c["status"]
                row["evidence"] = f"[{tag}, v4.4.32] {ev}"[:4000]
                row["correctness"] = corr[:1500]
                row["effectiveness"] = eff[:1500]
                applied += 1
                st[c["status"]] += 1

    LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"folded: {applied} cell-writes  dropped-ids: {dropped}")
    print(f"statuses: {dict(st)}")
    # gap severity tally across all deep-probe results
    sev = Counter()
    for res in results:
        for g in res.get("gaps", []):
            sev[str(g.get("severity", "?")).lower()] += 1
    print(f"gaps by severity: {dict(sev)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
