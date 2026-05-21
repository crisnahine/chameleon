"""End-to-end test for v0.6.0 branch pinning (canonical_ref).

Bootstraps a profile on main, commits it, switches to a feature branch
that modifies the profile, and asserts that get_pattern_context still
returns main's archetype (because config.canonical_ref points at main).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP_PD = tempfile.TemporaryDirectory()
os.environ["CHAMELEON_PLUGIN_DATA"] = _TMP_PD.name
os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"

from chameleon_mcp.profile.canonical_loader import (  # noqa: E402
    _resolve_ref,
    materialize_canonical,
)
from chameleon_mcp.tools import (  # noqa: E402
    _compute_repo_id,
    _effective_profile_dir,
    bootstrap_repo,
    get_pattern_context,
)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _make_tiny_ts_repo(td: Path) -> Path:
    repo = td / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "package.json").write_text(
        json.dumps({
            "name": "tiny-ts",
            "devDependencies": {"typescript": "5.0.0"},
        }),
        encoding="utf-8",
    )
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    for i in range(6):
        (src / f"util_{i}.ts").write_text(
            f"export const x{i} = {i};\n", encoding="utf-8"
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


section("Setup: bootstrap on main, commit profile")
_outer = tempfile.TemporaryDirectory()
try:
    repo = _make_tiny_ts_repo(Path(_outer.name))
    boot = bootstrap_repo(str(repo))
    boot_status = boot.get("data", {}).get("status")
    t("bootstrap succeeded", boot_status == "success", f"status={boot_status!r}")

    chameleon = repo / ".chameleon"
    t(
        ".chameleon/profile.json written",
        (chameleon / "profile.json").is_file(),
    )

    # Write config.json with canonical_ref → HEAD (which is main).
    (chameleon / "config.json").write_text(
        json.dumps({
            "$schema": "chameleon-config-0.6.0",
            "canonical_ref": "HEAD",
        }),
        encoding="utf-8",
    )
    _git(repo, "add", ".chameleon")
    _git(repo, "commit", "-m", "add chameleon profile + config")

    main_sha = _git(repo, "rev-parse", "HEAD")
    main_archetypes = sorted(
        json.loads((chameleon / "archetypes.json").read_text())["archetypes"].keys()
    )
    t("main has archetypes", len(main_archetypes) >= 1, ", ".join(main_archetypes))


    section("canonical_loader materializes the ref to a cache dir")
    repo_id = _compute_repo_id(repo)
    ref_sha = _resolve_ref(repo, "HEAD")
    t("HEAD resolves to a SHA", ref_sha == main_sha)

    cache_dir = materialize_canonical(repo, repo_id, "HEAD")
    t("materialize returned a cache dir", cache_dir is not None, str(cache_dir))
    t(
        "cache has COMMITTED sentinel",
        (cache_dir / "COMMITTED").is_file(),
    )
    cached_archetypes = sorted(
        json.loads((cache_dir / "archetypes.json").read_text())["archetypes"].keys()
    )
    t(
        "cache archetypes match main",
        cached_archetypes == main_archetypes,
        f"{cached_archetypes}",
    )


    section("Branch pinning: feature branch sees main's profile")
    # Switch to a feature branch and DELETE the .chameleon/ dir to
    # prove the canonical pin works even when the working tree has no
    # profile of its own. (Then we restore config.json so the pin
    # remains active.)
    _git(repo, "checkout", "-b", "feature/wip")
    # Wipe everything except keep config.json (the pin)
    cfg_content = (chameleon / "config.json").read_text(encoding="utf-8")
    import shutil

    shutil.rmtree(chameleon)
    chameleon.mkdir()
    (chameleon / "config.json").write_text(cfg_content, encoding="utf-8")

    # _effective_profile_dir should resolve to the canonical cache,
    # NOT the (now-empty) working tree.
    eff = _effective_profile_dir(repo)
    t(
        "_effective_profile_dir on feature branch → canonical cache",
        eff == cache_dir,
        f"eff={eff}, cache={cache_dir}",
    )

    # get_pattern_context follows the pin: should still see main's archetypes.
    sample = repo / "src" / "util_0.ts"
    ctx = get_pattern_context(str(sample))["data"]
    arch = ctx.get("archetype", {}).get("archetype")
    t(
        "get_pattern_context returns an archetype from main's profile",
        arch in main_archetypes,
        f"got archetype={arch!r}; main has {main_archetypes}",
    )


    section("No config.json → falls back to working tree (v0.5.x behavior)")
    # Remove config.json; effective should now point at working tree.
    (chameleon / "config.json").unlink()
    eff_no_config = _effective_profile_dir(repo)
    t(
        "without config.json, effective = working tree",
        eff_no_config == chameleon,
        f"got {eff_no_config}",
    )


    section("Bad canonical_ref → falls back to working tree")
    (chameleon / "config.json").write_text(
        json.dumps({"canonical_ref": "definitely-not-a-real-ref"}),
        encoding="utf-8",
    )
    eff_bad_ref = _effective_profile_dir(repo)
    t(
        "unresolvable ref → effective = working tree",
        eff_bad_ref == chameleon,
        f"got {eff_bad_ref}",
    )

finally:
    _outer.cleanup()


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
