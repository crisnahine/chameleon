"""Pin the v0.5.14 bug-7 fix: list_profiles prunes dead temp-dir entries.

Bug: list_profiles returned 533 total_known with the first ~85 all
/private/var/folders/.../tmp.../... paths from prior dogfood test runs
where the dirs are long gone but the DB still tracked them.

Fix: list_profiles now calls _prune_dead_temp_repos before serving the
query. It only prunes entries under temp prefixes (/private/var/folders,
/var/folders, /tmp, /private/tmp, or $TMPDIR) whose repo_root no longer
exists — a real repo the user moved or detached isn't touched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name

from chameleon_mcp import index_db  # noqa: E402
from chameleon_mcp.tools import (  # noqa: E402
    _is_dead_temp_repo_root,
    _prune_dead_temp_repos,
    list_profiles,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


section("_is_dead_temp_repo_root recognizes the right things")
t("None → False", not _is_dead_temp_repo_root(None))
t("empty string → False", not _is_dead_temp_repo_root(""))
t(
    "real existing path → False",
    not _is_dead_temp_repo_root(str(Path(__file__).resolve().parent)),
)
t(
    "/private/var/folders/x/y/T/tmpfoo/repo → True (dead temp)",
    _is_dead_temp_repo_root(
        "/private/var/folders/x/y/T/tmpfoo/repo_that_does_not_exist"
    ),
)
t(
    "/Users/me/code/real-repo (non-temp) but missing → False (not pruned)",
    not _is_dead_temp_repo_root("/Users/me/code/real-repo-that-does-not-exist"),
)


section("_prune_dead_temp_repos drops dead temp rows, keeps real-with-profile ones")
# Insert two rows: one fake temp, one real-WITH-profile
# v0.5.16: prune now ALSO removes rows whose real-path .chameleon/
# profile.json is gone (covers `rm -rf .chameleon` case), so the
# "preserve real path" test needs a real .chameleon/ to survive.
fake_temp = "/private/var/folders/zz/zz/T/tmpdead/never_existed"
real_dir = tempfile.mkdtemp(prefix="real_with_profile_")
import shutil as _shutil

(Path(real_dir) / ".chameleon").mkdir()
(Path(real_dir) / ".chameleon" / "profile.json").write_text("{}", encoding="utf-8")

index_db.upsert_repo("aaaa11111111", fake_temp, archetype_count=1)
index_db.upsert_repo("bbbb22222222", real_dir, archetype_count=1)

removed = _prune_dead_temp_repos()
t("at least 1 dead temp row pruned", removed >= 1, f"removed={removed}")

# After prune, the real-with-profile one should still be present
rows, _next, total = index_db.list_repos(None, 100)
remaining_roots = [r.get("repo_root") for r in rows]
t("real (non-temp, has .chameleon/profile.json) row preserved", real_dir in remaining_roots)
t("dead temp row removed", fake_temp not in remaining_roots)
_shutil.rmtree(real_dir, ignore_errors=True)


section("list_profiles runs the prune transparently")
# Plant another dead temp entry; calling list_profiles should remove it
index_db.upsert_repo("cccc33333333", "/private/tmp/another_dead_repo_xyz", archetype_count=1)
resp = list_profiles()
profiles = resp.get("data", {}).get("profiles", [])
roots = [p.get("repo_root") for p in profiles]
t(
    "list_profiles output excludes the dead temp row",
    "/private/tmp/another_dead_repo_xyz" not in roots,
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
