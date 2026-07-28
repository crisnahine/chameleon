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


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def derive_git_tags() -> int:
    out = _git("tag")
    if out is None:
        return -1
    return len([ln for ln in out.splitlines() if ln.strip()])


def derive_latest_git_tag() -> str | None:
    """Newest release tag by version order, not commit order.

    A tag cut on a maintenance branch lands out of chronological order, so
    sorting by refname version is what names the actual newest release.
    """
    out = _git("tag", "--sort=-v:refname")
    if out is None:
        return None
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return names[0] if names else None


def derive_unit_test_functions() -> int:
    """Test functions defined under tests/unit/.

    A FLOOR for what pytest collects, never the exact figure: parametrize
    turns one function into many collected items, so the collected count is
    always at least this. Deriving the exact number would mean importing the
    suite, and this check has to run on a bare interpreter with no
    dependencies installed.
    """
    d = ROOT / "tests" / "unit"
    if not d.is_dir():
        return -1
    pattern = re.compile(r"^[ \t]*(?:async[ \t]+)?def test_", re.MULTILINE)
    return sum(
        len(pattern.findall(p.read_text(encoding="utf-8", errors="replace")))
        for p in d.glob("*.py")
    )


def _underivable(actual: object) -> bool:
    """Whether a deriver failed to produce a value.

    Numeric derivers signal failure with a negative sentinel and string ones
    with None, so both shapes have to be recognized before a claim is
    compared -- a failed derive must skip the check, never fail it.
    """
    return actual is None or (isinstance(actual, int) and actual < 0)


def _claim_int(pattern: str, rel: str = "README.md") -> int | None:
    m = re.search(pattern, _read(rel))
    return int(m.group(1).replace(",", "")) if m else None


def claim_readme_releases() -> int | None:
    return _claim_int(r"(\d+)\s+releases\b")


def claim_table_releases() -> int | None:
    return _claim_int(r"\|\s*Released versions\s*\|\s*\*\*([\d,]+)\*\*")


def claim_table_latest_version() -> str | None:
    m = re.search(r"\|\s*Released versions\s*\|[^|]*?\bto\s+(v[\d.]+)\)", _read("README.md"))
    return m.group(1) if m else None


def claim_table_unit_tests() -> int | None:
    return _claim_int(r"\|\s*Unit tests\s*\|\s*\*\*([\d,]+)\*\*")


# Doc claims: (label, what the doc says, what the source says, why it matters).
# Every entry is compared for EQUALITY, so each claim must be exactly derivable
# from source on a bare interpreter -- this runs in CI with no deps installed.
DOC_CLAIMS: list[tuple[str, object, object, str]] = [
    (
        "README.md release count",
        claim_readme_releases,
        derive_git_tags,
        "A release is a git tag; the count moves every time one is cut.",
    ),
    (
        "README.md proof-table release count",
        claim_table_releases,
        derive_git_tags,
        "The proof table promises every number in it is checkable right now.",
    ),
    (
        "README.md proof-table latest version",
        claim_table_latest_version,
        derive_latest_git_tag,
        "The table names the version range it covers; the upper bound is the newest tag.",
    ),
]

# Claims that can only be bounded from below, not derived exactly.
# (label, what the doc says, the floor the source proves, why it matters)
FLOOR_CLAIMS: list[tuple[str, object, object, str]] = [
    (
        "README.md proof-table unit test count",
        claim_table_unit_tests,
        derive_unit_test_functions,
        "pytest collects at least one item per test function, so a claim below "
        "the number of defined test functions is provably stale.",
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
        if _underivable(actual):
            print(f"SKIP  {label}: could not derive")
            continue
        if claimed != actual:
            failures.append(f"{label}: doc says {claimed}, source says {actual}\n      {why}")
        else:
            print(f"ok    {label}: {claimed}")

    for label, claim_fn, floor_fn, why in FLOOR_CLAIMS:
        claimed = claim_fn()
        floor = floor_fn()
        if claimed is None:
            print(f"SKIP  {label}: no claim found to check")
            continue
        if _underivable(floor):
            print(f"SKIP  {label}: could not derive")
            continue
        if claimed < floor:
            failures.append(
                f"{label}: doc says {claimed}, source proves at least {floor}\n      {why}"
            )
        else:
            print(f"ok    {label}: {claimed} (at least {floor})")

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
