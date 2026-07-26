#!/usr/bin/env python3
"""Fail when a number written in the docs no longer matches the source.

Every count in this repo's prose was true once. They drift silently because
nothing recomputes them: the README badge, its own table, and the collected
suite disagreed three ways, and the README's release count sat 82 behind the
tags. A reader cannot tell a stale number from a current one, and a
stale number in a doc that exists to be authoritative is worse than no number.

Each check DERIVES the value from source and compares it to what the doc claims,
so the failure names both sides and the fix is unambiguous. Run it in CI and the
class stops recurring.

Exit 0 = every claim matches. Exit 1 = at least one drifted.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def unwired_hook_scripts() -> list[str]:
    """Hook wrappers on disk that hooks.json never names.

    Compares SETS, not counts: one wrapper is legitimately wired to several
    events, so a count comparison would fail on a healthy repo. What actually
    matters is that nothing ships unreferenced -- peer-skill-advise ran live as
    a PreToolUse hook while no workflow, smoke test, or shellcheck job touched
    it, and a set check is what surfaces that.
    """
    d = ROOT / "plugin" / "hooks"
    if not d.is_dir():
        return []
    manifest = _read("plugin/hooks/hooks.json")
    return sorted(
        p.name
        for p in d.iterdir()
        if p.is_file()
        and not p.name.startswith("_")
        and p.suffix not in (".json", ".cmd")
        and p.name not in manifest
    )


def derive_git_tags() -> int:
    try:
        out = subprocess.run(
            ["git", "tag"], cwd=ROOT, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if out.returncode != 0:
        return -1
    return len([ln for ln in out.stdout.splitlines() if ln.strip()])


def claim_readme_releases() -> int | None:
    m = re.search(r"(\d+)\s+releases\b", _read("README.md"))
    return int(m.group(1)) if m else None




# Doc claims: (label, what the doc says, what the source says, why it matters).
DOC_CLAIMS: list[tuple[str, object, object, str]] = [
    (
        "README.md release count",
        claim_readme_releases,
        derive_git_tags,
        "A release is a git tag; the count moves every time one is cut.",
    ),
]


def main() -> int:
    failures: list[str] = []

    unwired = unwired_hook_scripts()
    if unwired:
        failures.append(
            "hook wrappers never named in hooks.json: "
            + ", ".join(unwired)
            + "\n      An unreferenced wrapper is either dead or wired somewhere "
            "no gate can see."
        )
    else:
        print("ok    every hook wrapper on disk is named in hooks.json")

    for label, claim_fn, derive_fn, why in DOC_CLAIMS:
        claimed = claim_fn()
        actual = derive_fn()
        if claimed is None:
            print(f"SKIP  {label}: no claim found to check")
            continue
        if actual < 0:
            print(f"SKIP  {label}: could not derive")
            continue
        if claimed != actual:
            failures.append(f"{label}: doc says {claimed}, source says {actual}\n      {why}")
        else:
            print(f"ok    {label}: {claimed}")

    if failures:
        print("\nDerived-count drift:\n")
        for f in failures:
            print(f"  - {f}")
        print("\nUpdate the doc, or fix the source. Do not delete the check.")
        return 1
    print("\nAll derived counts match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
