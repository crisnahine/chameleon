"""Pin the rec-5 (v0.5.14 bug 5) fix: .claude/ is excluded from discovery.

The bug: `.claude/worktrees/` contains git worktrees that mirror the
parent repo's source. Without exclusion, bootstrap walked them and
clustered 9k+ duplicated files alongside the real source, polluting
archetypes (a `class-worktrees` archetype showed up).

The fix: add `.claude` to EXCLUDE_FROM_CLUSTERING_DIRS so discovery
skips it (and any nested git worktrees under .claude/worktrees/).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from chameleon_mcp.bootstrap.discovery import (
    EXCLUDE_FROM_CLUSTERING_DIRS,
    discover_files,
    discovery_stats,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


section(".claude is in the exclusion set")
t(".claude in EXCLUDE_FROM_CLUSTERING_DIRS", ".claude" in EXCLUDE_FROM_CLUSTERING_DIRS)


section("discovery skips .claude/worktrees/ source files")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "real.ts").write_text("export const x = 1;\n", encoding="utf-8")
    wt = repo / ".claude" / "worktrees" / "branch-a" / "src"
    wt.mkdir(parents=True)
    (wt / "leaked.ts").write_text("export const y = 2;\n", encoding="utf-8")
    (wt / "another.ts").write_text("export const z = 3;\n", encoding="utf-8")

    files = discover_files(repo)
    names = sorted(p.name for p in files)
    t("only real.ts returned (worktree files dropped)", names == ["real.ts"], str(names))

    counts = discovery_stats(repo)
    t("discovery_stats post_exclusion == 1", counts["post_exclusion"] == 1, str(counts))


section("discovery still walks normal subdirs (regression guard)")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "src").mkdir()
    (repo / "src" / "main.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (repo / "lib").mkdir()
    (repo / "lib" / "util.ts").write_text("export const y = 2;\n", encoding="utf-8")
    files = discover_files(repo)
    names = sorted(p.name for p in files)
    t("normal source files still discovered", names == ["main.ts", "util.ts"], str(names))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {len(PASS) + len(FAIL)}")
print(f"  Pass: {len(PASS)}")
print(f"  Fail: {len(FAIL)}")
if FAIL:
    print("\n  FAILURES:")
    for name, info in FAIL:
        print(f"    - {name}{(': ' + info) if info else ''}")
    sys.exit(1)
sys.exit(0)
