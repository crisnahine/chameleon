"""F1: refuter integrity canaries.

The refuter is the ONLY component allowed to kill a finding, and nothing measures
it. A miscalibrated-aggressive refuter silently destroys recall (kills real
findings); a too-lenient one lets false findings through. This feeds a curated set
of ground-truth canaries through the REAL refuter path and measures recall +
precision by defect class, so a refuter-prompt or model change that tanks either
is caught before it ships.

OFFLINE / periodic ONLY: it spawns the real refuter (claude -p), so it never runs
inline in a user turn or on any hook hot path. Worktree-free -- each canary is
self-contained (excerpt + finding + expectation), so nothing touches the repo.

A canary is REAL (the excerpt genuinely has the named bug -> the refuter must NOT
refute; a refutation is a RECALL failure) or FALSE (the excerpt is correct and the
finding is a plausible-but-wrong claim -> the refuter SHOULD refute; a
non-refutation is a PRECISION failure). The verdict vocabulary is the refuter's:
confirmed / refuted / unverified.
"""

from __future__ import annotations

import os
from pathlib import Path

# fmt: off
CANARIES: list[dict] = [
    {
        "cls": "inverted-condition", "real": True, "file": "auth.py", "line": 3,
        "excerpt": (
            "def can_access(user):\n"
            "    if user.is_admin:\n"
            "        return False\n"
            "    return True\n"
        ),
        "claim": (
            "the admin check is inverted: an admin is denied access (returns False) "
            "and every non-admin is granted it"
        ),
    },
    {
        "cls": "dropped-await", "real": True, "file": "save.ts", "line": 2,
        "excerpt": (
            "async function save(u) {\n"
            "  const r = persist(u);\n"
            "  return r.id;\n"
            "}\n"
        ),
        "claim": "persist(u) is not awaited, so r is a Promise and r.id is undefined",
    },
    {
        "cls": "off-by-one", "real": True, "file": "slice.py", "line": 2,
        "excerpt": (
            "def last_n(items, n):\n"
            "    return items[len(items) - n - 1:]\n"
        ),
        "claim": "off-by-one: the slice start should be len(items) - n, not len(items) - n - 1",
    },
    {
        "cls": "null-deref", "real": True, "file": "user.ts", "line": 2,
        "excerpt": (
            "function name(u?: User) {\n"
            "  return u.name.toUpperCase();\n"
            "}\n"
        ),
        "claim": "u is optional but dereferenced without a guard; u.name throws when u is undefined",
    },
    {
        "cls": "false-positive", "real": False, "file": "total.py", "line": 2,
        "excerpt": (
            "def total(xs):\n"
            "    return sum(xs)\n"
        ),
        "claim": "this returns the count of xs, not their sum",
    },
    {
        "cls": "false-positive", "real": False, "file": "even.py", "line": 2,
        "excerpt": (
            "def is_even(n):\n"
            "    return n % 2 == 0\n"
        ),
        "claim": "this returns True for odd numbers",
    },
    {
        "cls": "false-positive", "real": False, "file": "greet.ts", "line": 2,
        "excerpt": (
            "function greet(name: string): string {\n"
            "  return `Hello, ${name}`;\n"
            "}\n"
        ),
        "claim": "the template literal is unterminated and will not compile",
    },
]
# fmt: on


def _canary_finding(idx: int, canary: dict) -> dict:
    """Adapt a canary to the refuter's finding shape (id maps the verdict back)."""
    return {
        "id": str(idx),
        "severity": "high",
        "file": canary.get("file") or "",
        "line": canary.get("line"),
        "claim": canary.get("claim") or "",
        "evidence": "",
    }


def evaluate_canaries(verdict_by_id: dict, canaries: list[dict] | None = None) -> dict:
    """Pure aggregation: given the refuter's ``{canary_index: verdict}`` map, score
    recall (real canaries the refuter did NOT kill) and precision (false canaries it
    DID kill), overall and per defect class. No spawning -- fully testable.

    A missing verdict (the refuter was never run for that canary) is scored as
    ``unverified`` (the refuter took no killing action), which counts as recall-OK
    for a real canary and precision-MISS for a false one.
    """
    canaries = canaries if canaries is not None else CANARIES

    def _new() -> dict:
        return {"real_total": 0, "recall_ok": 0, "false_total": 0, "precision_ok": 0}

    overall = _new()
    by_class: dict[str, dict] = {}
    misses: list[dict] = []
    for i, c in enumerate(canaries):
        verdict = str(verdict_by_id.get(str(i)) or verdict_by_id.get(i) or "unverified")
        refuted = verdict == "refuted"
        bucket = by_class.setdefault(c.get("cls", "?"), _new())
        if c.get("real"):
            overall["real_total"] += 1
            bucket["real_total"] += 1
            if not refuted:  # a real finding SURVIVED the refuter -> recall OK
                overall["recall_ok"] += 1
                bucket["recall_ok"] += 1
            else:  # the refuter KILLED a real finding -> recall failure
                misses.append({"cls": c.get("cls"), "kind": "recall", "verdict": verdict})
        else:
            overall["false_total"] += 1
            bucket["false_total"] += 1
            if refuted:  # a false finding was KILLED -> precision OK
                overall["precision_ok"] += 1
                bucket["precision_ok"] += 1
            else:  # a false finding SURVIVED -> precision failure
                misses.append({"cls": c.get("cls"), "kind": "precision", "verdict": verdict})

    def _rate(ok: int, total: int) -> float | None:
        return (ok / total) if total else None

    def _finalize(b: dict) -> dict:
        return {
            **b,
            "recall": _rate(b["recall_ok"], b["real_total"]),
            "precision": _rate(b["precision_ok"], b["false_total"]),
        }

    return {
        "overall": _finalize(overall),
        "by_class": {k: _finalize(v) for k, v in by_class.items()},
        "misses": misses,
    }


def run_refuter_canaries(
    repo_root, *, model: str | None = None, timeout: int | None = None, canaries=None
) -> dict:
    """Spawn the real refuter over each canary and score it (``evaluate_canaries``).

    Returns ``{"status": "unavailable", "reason": ...}`` without spawning when the
    refuter CLI is absent. OFFLINE ONLY -- caller must not invoke this on a hook
    path. Each spawn is retry-free.
    """
    from chameleon_mcp import refuter

    canaries = canaries if canaries is not None else CANARIES
    absent = refuter.refuter_cli_absent()
    if absent is not None:
        return {"status": "unavailable", "reason": absent}

    from chameleon_mcp._thresholds import threshold_int

    model = model or os.environ.get("CHAMELEON_REFUTER_MODEL", "sonnet")
    timeout = timeout if timeout is not None else threshold_int("REFUTER_TIMEOUT_SECONDS")

    root = Path(repo_root)
    verdict_by_id: dict = {}
    for i, c in enumerate(canaries):
        try:
            result = refuter.run_one(
                root,
                _canary_finding(i, c),
                c.get("excerpt") or "",
                model=model,
                timeout=timeout,
                retry=False,
            )
            verdict_by_id[str(i)] = (result or {}).get("verdict", "unverified")
        except Exception:
            verdict_by_id[str(i)] = "unverified"
    return {"status": "ran", "model": model, **evaluate_canaries(verdict_by_id, canaries)}


def main(argv: list[str] | None = None) -> int:
    """Offline runner: ``python -m chameleon_mcp.refuter_canary [repo_root]``.

    Spawns the real refuter over the shipped canaries and prints the recall /
    precision scoreboard. Meant for periodic / on-refuter-prompt-change runs, never
    a hook. Returns nonzero when recall or precision fell below 1.0 (a regression a
    CI job can gate on) or the refuter was unavailable.
    """
    import json
    import sys

    root = (argv or sys.argv[1:] or ["."])[0]
    out = run_refuter_canaries(root)
    print(json.dumps(out, indent=2, sort_keys=True))
    if out.get("status") != "ran":
        return 2
    ov = out.get("overall", {})
    return 0 if (ov.get("recall") == 1.0 and ov.get("precision") == 1.0) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
