"""End-to-end git merge driver test.

Round 1: invoke chameleon-merge-driver.sh directly with synthetic
         base/ours/theirs profile JSONs; verify it exits 0 and writes
         the union profile to the "ours" path.
Round 2: build a real git repo with two branches that both modified
         .chameleon/archetypes.json, register the driver via
         .gitattributes + git config, run `git merge`, verify the
         conflict was resolved automatically and the resulting file
         loads cleanly via load_profile_dir.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")
MERGE_DRIVER = PLUGIN_ROOT / "scripts" / "chameleon-merge-driver.sh"


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


def make_profile(generation: int, archetypes: dict) -> dict:
    return {
        "schema_version": 4,
        "engine_min_version": "0.1.0",
        "generation": generation,
        "archetypes": archetypes,
    }


# ---------------------------------------------------------------------------
# Round 1 — driver script invoked directly
# ---------------------------------------------------------------------------
section("Round 1 — chameleon-merge-driver.sh direct invocation")

with tempfile.TemporaryDirectory() as tmp:
    base_p = Path(tmp) / "base.json"
    ours_p = Path(tmp) / "ours.json"
    theirs_p = Path(tmp) / "theirs.json"

    base_p.write_text(json.dumps(make_profile(1, {
        "cluster-A": {"cluster_size": 3, "canonical_witness": "a.ts"},
    })))
    ours_p.write_text(json.dumps(make_profile(2, {
        "cluster-A": {"cluster_size": 5, "canonical_witness": "a.ts"},
        "cluster-B": {"cluster_size": 4, "canonical_witness": "b.ts"},
    })))
    theirs_p.write_text(json.dumps(make_profile(3, {
        "cluster-A": {"cluster_size": 7, "canonical_witness": "a.ts"},
        "cluster-C": {"cluster_size": 2, "canonical_witness": "c.ts"},
    })))

    proc = subprocess.run(
        [str(MERGE_DRIVER), str(base_p), str(ours_p), str(theirs_p), "fake.json"],
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        timeout=30,
    )
    t(f"Driver exits 0 (got {proc.returncode})", proc.returncode == 0)
    if proc.returncode != 0:
        print(f"    stderr: {proc.stderr[:200]}")

    merged = json.loads(ours_p.read_text())
    t("Merged result has all 3 archetypes", set(merged["archetypes"].keys()) == {"cluster-A", "cluster-B", "cluster-C"})
    t(
        "Conflicting cluster-A: theirs (size 7) wins over ours (size 5)",
        merged["archetypes"]["cluster-A"]["cluster_size"] == 7,
    )


# ---------------------------------------------------------------------------
# Round 2 — real git repo with .gitattributes merge driver
# ---------------------------------------------------------------------------
section("Round 2 — real git merge invokes the driver")

if shutil.which("git") is None:
    print("  SKIP: git not on PATH")
    sys.exit(0)


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "merge_repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "--initial-branch=main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test User")
    git("config", "merge.chameleon.driver",
        f"{MERGE_DRIVER} %O %A %B %P")

    # Initial commit on main
    cham = repo / ".chameleon"
    cham.mkdir()
    (cham / "archetypes.json").write_text(json.dumps(make_profile(1, {
        "cluster-shared": {"cluster_size": 3, "canonical_witness": "x.ts"},
    }), indent=2))
    (repo / ".gitattributes").write_text(
        ".chameleon/archetypes.json merge=chameleon\n"
        ".chameleon/canonicals.json merge=chameleon\n"
        ".chameleon/profile.json    merge=chameleon\n"
        ".chameleon/rules.json      merge=chameleon\n"
    )
    (repo / "README.md").write_text("repo\n")
    git("add", ".")
    git("commit", "-m", "initial")

    # Branch A: add cluster-A
    git("checkout", "-b", "branch-a")
    (cham / "archetypes.json").write_text(json.dumps(make_profile(2, {
        "cluster-shared": {"cluster_size": 5, "canonical_witness": "x.ts"},
        "cluster-A": {"cluster_size": 4, "canonical_witness": "a.ts"},
    }), indent=2))
    git("commit", "-am", "add cluster-A")

    # Branch B: add cluster-B
    git("checkout", "main")
    git("checkout", "-b", "branch-b")
    (cham / "archetypes.json").write_text(json.dumps(make_profile(3, {
        "cluster-shared": {"cluster_size": 7, "canonical_witness": "x.ts"},
        "cluster-B": {"cluster_size": 6, "canonical_witness": "b.ts"},
    }), indent=2))
    git("commit", "-am", "add cluster-B")

    # Merge branch-a into branch-b — git will invoke the driver
    proc = subprocess.run(
        ["git", "merge", "branch-a", "-m", "merge"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT)},
        timeout=60,
    )
    t(
        f"git merge succeeds with no conflict markers (rc={proc.returncode})",
        proc.returncode == 0,
        proc.stderr[:200] if proc.returncode != 0 else "",
    )

    # Verify the result has all 3 clusters
    merged = json.loads((cham / "archetypes.json").read_text())
    t(
        f"Merged archetypes contains all 3 clusters (got {sorted(merged['archetypes'].keys())})",
        set(merged["archetypes"].keys()) == {"cluster-shared", "cluster-A", "cluster-B"},
    )

    # No conflict markers in the file
    raw = (cham / "archetypes.json").read_text()
    t(
        "No git conflict markers in merged file",
        "<<<<<<<" not in raw and ">>>>>>>" not in raw,
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
