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

Runs on a bare interpreter by default, which bounds the test count from below
rather than deriving it. Pass --exact to collect the suite for real and compare
that count for equality too; it needs the suite's dependencies installed and
fails rather than quietly serving the weaker floor.

Exit 0 = every claim matches. Exit 1 = at least one drifted. Exit 2 = the check
could not run as asked (bad argument, or --exact could not collect).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_USAGE = "usage: check-derived-counts.py [--exact]"


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


_TEST_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def test_", re.MULTILINE)
_UNIT_DIR = ROOT / "tests" / "unit"


def derive_unit_test_functions() -> int:
    """Test functions defined under tests/unit/.

    A FLOOR for what pytest collects, never the exact figure: parametrize
    turns one function into many collected items, so the collected count is
    always at least this. Deriving the exact number means importing the suite,
    which needs its dependencies -- see ``derive_collected_unit_tests`` and the
    ``--exact`` mode. This one runs on a bare interpreter with nothing
    installed, which is why it is the default.
    """
    if not _UNIT_DIR.is_dir():
        return -1
    return sum(
        len(_TEST_DEF_RE.findall(p.read_text(encoding="utf-8", errors="replace")))
        for p in _UNIT_DIR.glob("*.py")
    )


def _files_defining_tests() -> set[str]:
    """Repo-relative paths of tests/unit files that define at least one test.

    The expected collection surface. A file with no ``def test_`` (conftest.py)
    contributes no node ids and must not be expected.
    """
    return {
        f"tests/unit/{p.name}"
        for p in _UNIT_DIR.glob("*.py")
        if _TEST_DEF_RE.search(p.read_text(encoding="utf-8", errors="replace"))
    }


def derive_collected_unit_tests() -> int:
    """Items pytest actually collects from tests/unit/ -- the exact figure.

    The floor above tolerates a claim anywhere above it, so a README that
    drifted DOWNWARD (tests deleted, count not updated) stays green: the very
    drift this script exists to catch. Only a real collection resolves
    parametrize, so only a real collection can compare for equality.

    Returns the underivable sentinel rather than a number it cannot stand
    behind. Caller decides what that means; --exact treats it as fatal.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/unit/", "--co", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    if out.returncode != 0:
        return -1
    m = re.search(r"^(\d+) tests? collected", out.stdout, re.MULTILINE)
    if not m:
        return -1

    # A module skipped at COLLECTION time -- a module-level importorskip whose
    # dependency is missing -- contributes no node ids, yet pytest still exits 0
    # and still prints a plain "N tests collected". The count alone therefore
    # cannot tell a complete collection from a partial one, and a partial one
    # would read as a drifted README (`doc says 6849, pytest collects 6841`)
    # instead of an environment that cannot answer the question. Compare the
    # files that actually produced node ids against the files defining tests.
    collected_files = {
        line.split("::", 1)[0].replace("\\", "/")
        for line in out.stdout.splitlines()
        if "::" in line
    }
    if collected_files != _files_defining_tests():
        return -1
    return int(m.group(1))


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


def claim_badge_unit_tests() -> int | None:
    """The shields.io badge's test count.

    Its thousands separator is percent-encoded inside the badge URL, so the
    digits are recovered before the shared comma stripping runs.
    """
    m = re.search(r"unit%20tests-([\d%C]+?)-", _read("README.md"))
    return int(m.group(1).replace("%2C", "")) if m else None


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

# Claims bounded from below on a bare interpreter, compared for EQUALITY under
# --exact (which collects the suite for real).
# (label, what the doc says, the floor the source proves, why it matters)
FLOOR_CLAIMS: list[tuple[str, object, object, str]] = [
    (
        "README.md proof-table unit test count",
        claim_table_unit_tests,
        derive_unit_test_functions,
        "pytest collects at least one item per test function, so a claim below "
        "the number of defined test functions is provably stale.",
    ),
    (
        "README.md test-count badge",
        claim_badge_unit_tests,
        derive_unit_test_functions,
        "The badge is the first number a reader sees; it drifted to less than "
        "half the suite while the table beside it claimed something else again.",
    ),
]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # A misspelled flag must not be ignored. Silently dropping --exact would run
    # the weak floor while the caller believes equality was checked, and the CI
    # log would look identical either way.
    unknown = [a for a in args if a != "--exact"]
    if unknown:
        print(f"error: unrecognized argument(s): {', '.join(unknown)}")
        print(_USAGE)
        return 2

    failures: list[str] = []

    # --exact FAILS CLOSED. Whoever passes it is asking for the strong check, so
    # quietly serving the floor would defeat the request; and in CI, where the
    # dependencies are installed, a collection that cannot complete is itself
    # the thing worth reporting. Omit the flag to get the floor deliberately.
    exact_total = -1
    if "--exact" in args:
        exact_total = derive_collected_unit_tests()
        if _underivable(exact_total):
            print("error: --exact could not collect tests/unit/ completely.")
            print("       Install the suite's dependencies (uv sync --extra dev), or drop")
            print("       --exact to compare against the bare-interpreter floor instead.")
            return 2

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
        if claimed is None:
            print(f"SKIP  {label}: no claim found to check")
            continue
        if not _underivable(exact_total):
            # Not `why`: that one argues from the floor, which a claim above it
            # satisfies. Under equality the reproducible number is the point.
            if claimed != exact_total:
                failures.append(
                    f"{label}: doc says {claimed}, pytest collects {exact_total}\n"
                    "      The collected count is the number the doc invites a "
                    "reader to reproduce."
                )
            else:
                print(f"ok    {label}: {claimed} (exact)")
            continue
        floor = floor_fn()
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
