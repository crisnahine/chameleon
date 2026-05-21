"""Pin the v0.5.14 bug-1 fix: refresh_repo honors the original paths_glob.

Bug: a scoped bootstrap (paths_glob="{app,db,lib,config,spec}/**/*.rb")
clustered 4,852 files. Calling refresh_repo on the same repo (without
re-specifying paths_glob) walked 14,554 files — the full tree,
including .claude/worktrees/, polluting the profile with bogus
archetypes like class-worktrees.

Fix: bootstrap_repo persists the user-supplied paths_glob in
profile.json under profile_data["discovery"]["paths_glob"];
refresh_repo reads it via _persisted_paths_glob and re-applies it
to every internal bootstrap call.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp.tools import (  # noqa: E402
    _persisted_paths_glob,
    bootstrap_repo,
    refresh_repo,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _build_repo_with_scope_pollution(td: Path) -> Path:
    """Build a repo whose default-discovery file count is much larger
    than what a scoped paths_glob would match. The polluting dir
    isn't a real worktree, just enough source files in a non-scoped
    location to make the scope-loss obvious."""
    repo = td / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    # In-scope files
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"main_{i}.ts").write_text(
            f"export const x{i} = {i};\n", encoding="utf-8"
        )
    # OUT-OF-scope files (would be picked up without paths_glob)
    other = repo / "experiments"
    other.mkdir()
    for i in range(30):
        (other / f"e_{i}.ts").write_text(
            f"export const y{i} = {i};\n", encoding="utf-8"
        )
    return repo


section("bootstrap_repo persists paths_glob in profile.json")
with tempfile.TemporaryDirectory() as td:
    repo = _build_repo_with_scope_pollution(Path(td))
    bootstrap_repo(str(repo), paths_glob="src/**/*.ts")
    profile_path = repo / ".chameleon" / "profile.json"
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    discovery = data.get("discovery") or {}
    t(
        "profile.json carries discovery.paths_glob",
        discovery.get("paths_glob") == "src/**/*.ts",
        f"discovery={discovery}",
    )

    t(
        "_persisted_paths_glob returns the saved value",
        _persisted_paths_glob(repo / ".chameleon") == "src/**/*.ts",
    )


section("refresh_repo on a scoped bootstrap stays scoped")
with tempfile.TemporaryDirectory() as td:
    repo = _build_repo_with_scope_pollution(Path(td))
    boot = bootstrap_repo(str(repo), paths_glob="src/**/*.ts")
    boot_files = boot.get("data", {}).get("files_processed") or 0

    refresh = refresh_repo(str(repo), force=True)
    refresh_files = refresh.get("data", {}).get("files_processed") or 0

    t("bootstrap saw the in-scope file count (5)", boot_files == 5, f"got {boot_files}")
    t(
        "refresh saw the SAME in-scope file count (no scope loss)",
        refresh_files == boot_files,
        f"boot={boot_files} refresh={refresh_files}",
    )


section("refresh_repo without persisted paths_glob keeps default behavior")
with tempfile.TemporaryDirectory() as td:
    repo = _build_repo_with_scope_pollution(Path(td))
    # No paths_glob → uses default discovery
    boot = bootstrap_repo(str(repo))
    boot_files = boot.get("data", {}).get("files_processed") or 0
    refresh = refresh_repo(str(repo), force=True)
    refresh_files = refresh.get("data", {}).get("files_processed") or 0

    t(
        "refresh without persisted paths_glob walks the same default set",
        boot_files == refresh_files,
        f"boot={boot_files} refresh={refresh_files}",
    )


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
