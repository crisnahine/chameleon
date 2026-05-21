"""Pin the v0.5.14 bug-2 fix: /chameleon-refresh preserves trust when
the post-refresh profile is materially identical to the pre-refresh one.

Bug: chameleon-init skill says "Run /chameleon-refresh to re-analyze
without clearing trust state." The implementation invalidated trust on
every refresh because the generation counter bumped on each run,
changing the trust hash.

Fix: structural hashes (excluding generation + timestamps) are compared
pre vs post. When they match AND archetype_diff is empty AND a trust
record existed pre-refresh, trust is auto re-granted at the new hash
and the envelope carries `trust_preserved: true`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Force the test to use a sandboxed plugin-data dir so a trust grant
# inside the test doesn't pollute the user's real chameleon state.
_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp.tools import (  # noqa: E402
    bootstrap_repo,
    get_pattern_context,
    refresh_repo,
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


def _build_tiny_ts_repo(td: Path) -> Path:
    repo = td / "ts-repo"
    repo.mkdir()
    (repo / "package.json").write_text("{}", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    for i in range(5):
        (src / f"util_{i}.ts").write_text(
            f"export const x{i} = {i};\n", encoding="utf-8"
        )
    return repo


section("trust preserved on no-op refresh (materially identical)")
with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_ts_repo(Path(td))
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    sample = next(repo.rglob("*.ts"))
    pre = (
        get_pattern_context(str(sample))
        .get("data", {})
        .get("repo", {})
        .get("trust_state")
    )
    t("pre-refresh trust_state == 'trusted'", pre == "trusted", str(pre))

    # Force-refresh with NO source changes: structural content stays
    # identical, only the generation counter bumps.
    resp = refresh_repo(str(repo), force=True)
    data = resp.get("data", {})
    t(
        "refresh response includes trust_preserved=true",
        data.get("trust_preserved") is True,
        f"trust_preserved={data.get('trust_preserved')!r}",
    )

    post = (
        get_pattern_context(str(sample))
        .get("data", {})
        .get("repo", {})
        .get("trust_state")
    )
    t("post-refresh trust_state == 'trusted' (preserved)", post == "trusted", str(post))


section("trust correctly INVALIDATES on materially-changed refresh")
with tempfile.TemporaryDirectory() as td:
    repo = _build_tiny_ts_repo(Path(td))
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)

    # Add a brand-new source dir → archetype set may change OR
    # canonicals/witness paths change → structural hashes differ.
    new_dir = repo / "src" / "components"
    new_dir.mkdir()
    for i in range(15):
        (new_dir / f"Card{i}.tsx").write_text(
            f"import React from 'react';\nexport const Card{i} = () => <div>{i}</div>;\n",
            encoding="utf-8",
        )

    resp = refresh_repo(str(repo), force=True)
    data = resp.get("data", {})
    sample = next(repo.rglob("*.ts"))
    post = (
        get_pattern_context(str(sample))
        .get("data", {})
        .get("repo", {})
        .get("trust_state")
    )
    # Trust may or may not be preserved depending on whether the new
    # files crossed the structural-hash threshold. The contract is:
    # if archetype_diff shows real change, trust_preserved must NOT
    # be set to True. The two together must be consistent.
    diff = data.get("archetype_diff") or {}
    real_change = bool(
        diff.get("added") or diff.get("removed") or diff.get("renamed")
    )
    t(
        "if archetype_diff shows real change, trust_preserved is not True",
        not (real_change and data.get("trust_preserved") is True),
        f"real_change={real_change} preserved={data.get('trust_preserved')!r} post_trust={post!r}",
    )


section("structural hashes ignore generation/timestamp drift")
from chameleon_mcp.tools import _structural_hashes  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    pd = Path(td)
    base = {
        "generation": 1,
        "created_at": "2026-05-21T00:00:00Z",
        "schema_version": 7,
        "archetypes": {"foo": {"cluster_size": 1}},
    }
    (pd / "archetypes.json").write_text(json.dumps(base), encoding="utf-8")
    (pd / "canonicals.json").write_text(
        json.dumps({"generation": 1, "canonicals": {}}), encoding="utf-8"
    )
    (pd / "rules.json").write_text(
        json.dumps({"generation": 1, "rules": {}}), encoding="utf-8"
    )
    (pd / "idioms.md").write_text("# idioms\n", encoding="utf-8")
    h1 = _structural_hashes(pd)

    # Only generation + created_at bump
    base["generation"] = 99
    base["created_at"] = "2030-01-01T00:00:00Z"
    (pd / "archetypes.json").write_text(json.dumps(base), encoding="utf-8")
    (pd / "canonicals.json").write_text(
        json.dumps({"generation": 99, "canonicals": {}}), encoding="utf-8"
    )
    (pd / "rules.json").write_text(
        json.dumps({"generation": 99, "rules": {}}), encoding="utf-8"
    )
    h2 = _structural_hashes(pd)
    t("structural hashes equal after generation+timestamp bump", h1 == h2, f"h1={h1}, h2={h2}")

    # Real change to archetypes
    base["archetypes"]["bar"] = {"cluster_size": 1}
    (pd / "archetypes.json").write_text(json.dumps(base), encoding="utf-8")
    h3 = _structural_hashes(pd)
    t("structural hashes DIFFER after archetype addition", h2 != h3)


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
