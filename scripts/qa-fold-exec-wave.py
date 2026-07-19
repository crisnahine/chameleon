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
import re
import sys
from collections import Counter
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "tests" / "matrix" / "cells.jsonl"
SURFACES = ("hooks", "skills", "mcp-tools", "bootstrap", "enforcement", "aux", "framework-layers")
HOME = str(Path.home())
EVIDENCE_REQUIRED = {"PASS", "NA-ASSERTED"}

# The no-personal-paths CI guard flags any Users-or-home path whose account
# segment is not a known placeholder. Agents sometimes write example paths with
# an arbitrary segment into their evidence prose; neutralize every such shape to
# a placeholder segment the guard allows, so a tracked evidence string can never
# redden CI regardless of what an agent typed.
_PERSONAL_PATH = re.compile(r"/(Users|home)/([A-Za-z0-9._-]+)")
_ALLOWED_SEG = {
    "you",
    "your-user",
    "youruser",
    "user",
    "username",
    "name",
    "me",
    "runner",
    "ci",
    "example",
}


def _scrub_paths(text: str) -> str:
    """Replace any non-placeholder /Users|home/<seg> with an allowed placeholder."""
    return _PERSONAL_PATH.sub(
        lambda m: m.group(0) if m.group(2).lower() in _ALLOWED_SEG else f"/{m.group(1)}/you",
        text.replace(HOME, "~"),
    )


# The secret-detection enforcement tests craft real-SHAPED credentials (an AWS
# key, a GitHub token, an OpenAI key, a PEM header) to trigger the deny, then
# quote the trigger in their evidence. GitHub push protection blocks any commit
# carrying such a token even as a test fixture, so redact every secret shape to a
# placeholder before it reaches a tracked file. The redaction keeps the token's
# prefix so the evidence still reads as "an AWS-key-shaped string was flagged".
_SECRET_SHAPES = (
    re.compile(r"\b(AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{8,20}"),
    re.compile(r"\bA3T[A-Z0-9]{13,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def _redact_secrets(text: str) -> str:
    """Mask any real-shaped credential token to <prefix>REDACTED."""
    out = text
    for pat in _SECRET_SHAPES:
        out = pat.sub(lambda m: (m.group(0)[:4] + "REDACTED"), out)
    return out


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
            ev = _redact_secrets(_scrub_paths(c.get("evidence", "") or "")).strip()
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
            row["correctness"] = _redact_secrets(_scrub_paths(c.get("correctness", "") or ""))[:1500]
            row["effectiveness"] = _redact_secrets(
                _scrub_paths(c.get("effectiveness", "") or "")
            )[:1500]
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
