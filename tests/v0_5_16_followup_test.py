"""Tests for the three v0.5.15 residual issues fixed in v0.5.16.

1. get_rules parameter rename: archetype → source with deprecation alias.
2. list_profiles broader prune: removes rows whose .chameleon/profile.json
   is gone even when repo_root still exists.
3. disable_session defenses: requires trust grant + warns on unknown
   session_id.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp import index_db  # noqa: E402
from chameleon_mcp.tools import (  # noqa: E402
    _is_dead_chameleon_profile,
    _prune_dead_temp_repos,
    bootstrap_repo,
    disable_session,
    get_rules,
    trust_profile,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _build_tiny_repo(td: Path) -> Path:
    repo = td / "r"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"u_{i}.ts").write_text(f"export const x{i} = {i};\n", encoding="utf-8")
    return repo


section("Bug 1: get_rules rename — source= is the new canonical kwarg")
with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_repo(Path(td))
    bootstrap_repo(str(repo))

    # New canonical: source=None → all rules
    r = get_rules(str(repo))["data"]
    t("get_rules(repo) → rules list", isinstance(r.get("rules"), list))
    t("no deprecation when no kwarg used", "deprecation" not in r)

    # New canonical: source="eslint"
    r = get_rules(str(repo), "eslint")["data"]
    t("get_rules(repo, 'eslint') accepts source positionally", isinstance(r.get("rules"), list))
    t("no deprecation when source= used", "deprecation" not in r)

    # Back-compat: archetype= still works but warns
    r = get_rules(str(repo), archetype="eslint")["data"]
    t(
        "get_rules(repo, archetype='eslint') still works",
        isinstance(r.get("rules"), list),
    )
    t(
        "deprecation field emitted when archetype= used",
        "deprecation" in r and "rename to 'source'" in r["deprecation"],
        r.get("deprecation", "")[:80],
    )


section("Bug 7+: prune dead profiles when .chameleon/profile.json is gone")
with tempfile.TemporaryDirectory() as td:
    real = Path(td) / "real_repo"
    real.mkdir()
    (real / "package.json").write_text("{}", encoding="utf-8")
    # Plant index row pointing at real path WITHOUT a .chameleon/ dir
    index_db.upsert_repo("deadprofile1234", str(real), archetype_count=1)
    t(
        "_is_dead_chameleon_profile detects real-path-no-profile",
        _is_dead_chameleon_profile(str(real)),
    )
    removed = _prune_dead_temp_repos()
    t("at least 1 row pruned", removed >= 1, f"removed={removed}")
    rows, _next, _total = index_db.list_repos(None, 100)
    roots = [r.get("repo_root") for r in rows]
    t("dead-profile row removed", str(real) not in roots)

with tempfile.TemporaryDirectory() as td:
    real = Path(td) / "real_repo_with_profile"
    real.mkdir()
    (real / "package.json").write_text("{}", encoding="utf-8")
    chameleon = real / ".chameleon"
    chameleon.mkdir()
    (chameleon / "profile.json").write_text("{}", encoding="utf-8")
    t(
        "_is_dead_chameleon_profile False when profile exists",
        not _is_dead_chameleon_profile(str(real)),
    )


section("Bug 8 follow-up: disable_session requires trust + warns on unknown session")
with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_repo(Path(td))
    bootstrap_repo(str(repo))

    # No trust yet → disable_session must REFUSE
    r = disable_session(str(repo), "stranger-session-id")["data"]
    t(
        "disable_session refused without trust grant",
        r.get("status") == "failed" and "trust grant" in r.get("error", "").lower(),
        r.get("error", "")[:80],
    )

    # Grant trust
    trust_profile(str(repo), repo.name)
    # Now disable_session succeeds BUT warns because session is unseen
    r = disable_session(str(repo), "never-seen-this-session-id")["data"]
    t("disable_session succeeds after trust grant", r.get("status") == "success")
    t(
        "session_unknown_to_chameleon=True for unseen session",
        r.get("session_unknown_to_chameleon") is True,
        str(r.get("session_unknown_to_chameleon")),
    )
    t(
        "warning string explains the surface",
        "warning" in r and "planted" in r["warning"].lower(),
        r.get("warning", "")[:80],
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
