#!/usr/bin/env python3
"""Fold an execution-wave workflow's results into the matrix ledger.

Each column agent returns {column: "C<n>", cells: [{item_id, status, evidence,
correctness, effectiveness}]}. Every cell is applied to that agent's own column.
A cell is written only when it carries real evidence for a PASS/NA-ASSERTED (the
same guard qa-matrix.py enforces on manual marks); a FAIL/BLOCKED is always
authoritative. An item_id that does not resolve to a real ledger cell is dropped
and counted, never invented. $HOME is stripped to ~ so no personal path lands in
a tracked file (the CI no-personal-paths guard).

Usage: qa-fold-exec-wave.py <journal.jsonl>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "tests" / "matrix" / "cells.jsonl"
SURFACES = ("hooks", "skills", "mcp-tools", "bootstrap", "enforcement", "aux", "framework-layers")
HOME = str(Path.home())
EVIDENCE_REQUIRED = {"PASS", "NA-ASSERTED"}


def _resolve(index: dict, item_id: str, col: str) -> str | None:
    iid = (item_id or "").strip()
    cands = ([f"{iid}@{col}"] if "/" in iid else []) + [f"{s}/{iid}@{col}" for s in SURFACES]
    return next((x for x in cands if x in index), None)


def main() -> int:
    journal = Path(sys.argv[1])
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    index = {r["cell_id"]: r for r in rows}

    results = []
    for line in journal.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = d.get("result") if d.get("type") == "result" else None
        if isinstance(r, dict) and "cells" in r and r.get("column"):
            results.append(r)

    applied = dropped = skipped_noevidence = 0
    st = Counter()
    for res in results:
        col = str(res["column"]).strip()
        for c in res.get("cells", []):
            status = str(c.get("status", "")).strip()
            ev = (c.get("evidence", "") or "").replace(HOME, "~").strip()
            if status in EVIDENCE_REQUIRED and not ev:
                skipped_noevidence += 1
                continue
            cid = _resolve(index, c.get("item_id", ""), col)
            if not cid:
                dropped += 1
                continue
            row = index[cid]
            row["status"] = status
            row["evidence"] = f"[exec-wave] {ev}"[:4000]
            row["correctness"] = (c.get("correctness", "") or "").replace(HOME, "~")[:1500]
            row["effectiveness"] = (c.get("effectiveness", "") or "").replace(HOME, "~")[:1500]
            applied += 1
            st[status] += 1

    LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"folded: {applied}  dropped-ids: {dropped}  skipped-no-evidence: {skipped_noevidence}")
    print(f"statuses: {dict(st)}")
    sev = Counter()
    for res in results:
        for g in res.get("gaps", []):
            sev[str(g.get("severity", "?")).lower()] += 1
    if sev:
        print(f"gaps by severity: {dict(sev)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
