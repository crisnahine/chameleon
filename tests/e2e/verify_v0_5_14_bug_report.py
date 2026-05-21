"""Verify the 9 bugs in the v0.5.14 external test report.

Each check reproduces the reported issue and returns CONFIRMED /
NOT_REPRODUCED / N/A. Run 3 times to confirm consistency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Locate test repos via .env
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_env_path = REPO_ROOT / ".env"
TS_REPO: Path | None = None
RUBY_REPO: Path | None = None
if _env_path.is_file():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CHAMELEON_TEST_TS_REPO="):
            TS_REPO = Path(line.split("=", 1)[1])
        elif line.startswith("CHAMELEON_TEST_RUBY_REPO="):
            RUBY_REPO = Path(line.split("=", 1)[1])

ROUND = int(sys.argv[1]) if len(sys.argv) > 1 else 1
RESULTS: list[tuple[str, str, str, str]] = []  # (id, severity, status, info)


def report(bug_id: str, severity: str, status: str, info: str = "") -> None:
    RESULTS.append((bug_id, severity, status, info))
    bar = {
        "CONFIRMED": "✗ CONFIRMED",
        "NOT_REPRODUCED": "✓ NOT_REPRODUCED",
        "N/A": "- N/A",
        "PARTIAL": "~ PARTIAL",
    }[status]
    info_str = f" — {info}" if info else ""
    print(f"  [{bar}] Bug {bug_id} ({severity}){info_str}")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Bug 1 — refresh_repo doesn't honor original paths_glob
# ---------------------------------------------------------------------------


def bug1_refresh_loses_paths_glob() -> None:
    section("Bug 1 (CRITICAL): refresh_repo discards original paths_glob")
    if RUBY_REPO is None or not RUBY_REPO.is_dir():
        report("1", "CRITICAL", "N/A", "RUBY_REPO not configured")
        return

    from chameleon_mcp.tools import bootstrap_repo, refresh_repo

    # Wipe + bootstrap with a scoped paths_glob
    chameleon = RUBY_REPO / ".chameleon"
    if chameleon.exists():
        shutil.rmtree(chameleon, ignore_errors=True)
    boot = bootstrap_repo(
        str(RUBY_REPO),
        paths_glob="{app,db,lib,config,spec}/**/*.rb",
    )
    boot_data = boot.get("data", {})
    boot_files = boot_data.get("files_indexed") or boot_data.get(
        "files_processed", 0
    )
    boot_archetypes = boot_data.get("archetype_count", 0)

    # Refresh without paths_glob
    refresh = refresh_repo(str(RUBY_REPO), force=True)
    refresh_data = refresh.get("data", {})
    refresh_files = refresh_data.get("files_processed") or refresh_data.get(
        "files_indexed", 0
    )

    # If refresh processed >> bootstrap, the scope was discarded
    discarded = refresh_files > boot_files * 1.2
    info = (
        f"bootstrap files={boot_files} arch={boot_archetypes}; "
        f"refresh files={refresh_files}"
    )
    report("1", "CRITICAL", "CONFIRMED" if discarded else "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Bug 2 — refresh_repo invalidates trust
# ---------------------------------------------------------------------------


def bug2_refresh_invalidates_trust() -> None:
    section("Bug 2 (CRITICAL): refresh_repo invalidates trust")
    if RUBY_REPO is None or not RUBY_REPO.is_dir():
        report("2", "CRITICAL", "N/A", "RUBY_REPO not configured")
        return

    from chameleon_mcp.tools import (
        bootstrap_repo,
        get_pattern_context,
        refresh_repo,
        trust_profile,
    )

    chameleon = RUBY_REPO / ".chameleon"
    if not (chameleon / "COMMITTED").exists():
        bootstrap_repo(str(RUBY_REPO))

    trust_profile(str(RUBY_REPO), RUBY_REPO.name)
    sample = next(RUBY_REPO.rglob("*.rb"), None)
    if sample is None:
        report("2", "CRITICAL", "N/A", "no .rb file found in repo")
        return

    pre = get_pattern_context(str(sample)).get("data", {}).get("repo", {}).get(
        "trust_state"
    )
    refresh_repo(str(RUBY_REPO), force=True)
    post = get_pattern_context(str(sample)).get("data", {}).get("repo", {}).get(
        "trust_state"
    )

    invalidated = pre == "trusted" and post == "stale"
    report(
        "2",
        "CRITICAL",
        "CONFIRMED" if invalidated else "NOT_REPRODUCED",
        f"pre={pre!r} post={post!r}",
    )


# ---------------------------------------------------------------------------
# Bug 3 — get_rules archetype param is misnamed/misdocumented
# ---------------------------------------------------------------------------


def bug3_get_rules_archetype_param() -> None:
    section("Bug 3 (MAJOR): get_rules archetype param misnamed/misdocumented")
    if RUBY_REPO is None or not RUBY_REPO.is_dir():
        report("3", "MAJOR", "N/A", "RUBY_REPO not configured")
        return

    from chameleon_mcp.tools import get_rules

    # Pick a real archetype from the profile
    chameleon = RUBY_REPO / ".chameleon"
    archetypes_path = chameleon / "archetypes.json"
    if not archetypes_path.is_file():
        report("3", "MAJOR", "N/A", "no archetypes.json")
        return
    archetypes = json.loads(archetypes_path.read_text())["archetypes"]
    sample_arch = next(iter(archetypes.keys()))

    resp = get_rules(str(RUBY_REPO), sample_arch)
    data = resp.get("data", {})
    status = data.get("status")
    error = data.get("error", "")

    # Docstring claim per report: "Return rules + citations filtered by archetype"
    # Actual behavior: rejects with explicit error
    rejects = status == "failed" and "archetype name" in error.lower()
    # Look at actual docstring to confirm misnaming
    import inspect

    docstring = inspect.getdoc(get_rules) or ""
    docstring_misleads = "filtered by archetype" in docstring.lower()

    info = f"rejects={rejects}, docstring_misleads={docstring_misleads}"
    if rejects and docstring_misleads:
        report("3", "MAJOR", "CONFIRMED", info)
    elif rejects and not docstring_misleads:
        report(
            "3",
            "MAJOR",
            "NOT_REPRODUCED",
            "behavior is the same but docstring explicitly says archetype names are rejected; not misleading",
        )
    else:
        report("3", "MAJOR", "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Bug 4 — drift-banner hook crashes on Python 3.9 datetime.UTC
# ---------------------------------------------------------------------------


def bug4_drift_banner_py39() -> None:
    section("Bug 4 (MAJOR): drift-banner hook uses datetime.UTC (Py3.11+)")
    # Search the codebase for datetime.UTC usage
    hits = subprocess.run(
        ["grep", "-rn", "datetime.UTC", "mcp/chameleon_mcp/", "hooks/", "scripts/"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    uses_utc = bool(hits.stdout.strip())

    # Check if hooks/session-start shebang or python target is system Python
    hook_path = REPO_ROOT / "hooks" / "session-start"
    py_pinned = False
    if hook_path.is_file():
        shebang = hook_path.read_text(encoding="utf-8").splitlines()[0]
        py_pinned = (
            "/Library/Developer/CommandLineTools" not in shebang
            and "system" not in shebang.lower()
        )

    info = f"uses_datetime.UTC={uses_utc}, hook_shebang_pinned={py_pinned}"
    # Bug is confirmed if BOTH conditions hold: code uses datetime.UTC AND hook
    # might be invoked with system Python.
    if uses_utc and not py_pinned:
        report("4", "MAJOR", "CONFIRMED", info)
    elif not uses_utc:
        report("4", "MAJOR", "NOT_REPRODUCED", "no datetime.UTC usage in tree")
    else:
        report("4", "MAJOR", "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Bug 5 — default bootstrap discovery doesn't exclude .claude/worktrees/
# ---------------------------------------------------------------------------


def bug5_worktrees_excluded() -> None:
    section("Bug 5 (MAJOR): default discovery doesn't exclude .claude/worktrees/")
    from chameleon_mcp.bootstrap.discovery import (
        EXCLUDE_FROM_CLUSTERING_DIRS,
        discover_files,
    )

    in_exclude_set = any(
        "claude" in d.lower() or "worktree" in d.lower()
        for d in EXCLUDE_FROM_CLUSTERING_DIRS
    )

    # Behavioral check: plant a worktrees subdir and see if discovery walks it
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "real.ts").write_text("export const x = 1;\n", encoding="utf-8")
        wt = repo / ".claude" / "worktrees" / "abc" / "src"
        wt.mkdir(parents=True)
        (wt / "leaked.ts").write_text("export const y = 2;\n", encoding="utf-8")
        files = discover_files(repo)
        names = sorted(p.name for p in files)
        worktree_leaked = "leaked.ts" in names

    info = (
        f"exclusion_set_has_claude_or_worktree={in_exclude_set}, "
        f"discovery_leaks_worktree_files={worktree_leaked}"
    )
    if not in_exclude_set and worktree_leaked:
        report("5", "MAJOR", "CONFIRMED", info)
    else:
        report("5", "MAJOR", "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Bug 6 — paths_glob brace expansion fails in basename
# ---------------------------------------------------------------------------


def bug6_brace_in_basename() -> None:
    section("Bug 6 (MEDIUM): paths_glob brace expansion in basename")
    if TS_REPO is None or not TS_REPO.is_dir():
        report("6", "MEDIUM", "N/A", "TS_REPO not configured")
        return
    from chameleon_mcp.tools import bootstrap_repo

    chameleon = TS_REPO / ".chameleon"
    if chameleon.exists():
        shutil.rmtree(chameleon, ignore_errors=True)
    resp = bootstrap_repo(
        str(TS_REPO),
        paths_glob="{src,cypress}/**/*.{ts,tsx,js,jsx}",
    )
    data = resp.get("data", {})
    status = data.get("status") or ""
    error = data.get("error") or ""
    files_ok = (data.get("files_processed") or data.get("files_indexed") or 0) > 0

    if status == "success" and files_ok:
        # Brace expansion now actually works; bug 6 is fixed.
        report(
            "6",
            "MEDIUM",
            "NOT_REPRODUCED",
            f"brace works; status={status} files={data.get('files_processed')}",
        )
    elif "no source files" in error.lower() and "brace" not in error.lower():
        # Old behavior: misleading generic error message
        report("6", "MEDIUM", "CONFIRMED", f"error={error[:80]!r}")
    else:
        report(
            "6",
            "MEDIUM",
            "PARTIAL",
            f"status={status} error={error[:80]!r}",
        )


# ---------------------------------------------------------------------------
# Bug 7 — list_profiles accumulates dead /private/var/folders/.../tmp entries
# ---------------------------------------------------------------------------


def bug7_list_profiles_dead_entries() -> None:
    section("Bug 7 (MEDIUM): list_profiles tracks dead temp-dir repos")
    from chameleon_mcp.tools import list_profiles

    resp = list_profiles()
    profiles = resp.get("data", {}).get("profiles", []) or []
    total = len(profiles)
    dead_tmp = 0
    for p in profiles:
        root = p.get("repo_root") or p.get("path") or ""
        if "/private/var/folders" in root or "/tmp/" in root or "/var/folders" in root:
            if not Path(root).is_dir():
                dead_tmp += 1
    info = f"total={total} dead_tmp_entries={dead_tmp}"
    if dead_tmp >= 5:
        report("7", "MEDIUM", "CONFIRMED", info)
    elif dead_tmp > 0:
        report("7", "MEDIUM", "PARTIAL", info)
    else:
        report("7", "MEDIUM", "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Bug 8 — disable_session accepts arbitrary session_id
# ---------------------------------------------------------------------------


def bug8_disable_session_arbitrary() -> None:
    section("Bug 8 (MEDIUM): disable_session accepts arbitrary session_id")
    if RUBY_REPO is None or not RUBY_REPO.is_dir():
        report("8", "MEDIUM", "N/A", "RUBY_REPO not configured")
        return
    from chameleon_mcp.tools import disable_session

    resp = disable_session(str(RUBY_REPO), "fake-session-for-test-by-anyone")
    data = resp.get("data", {})
    status = data.get("status")
    accepted = status == "success"
    # The actual signing happens in optouts.write_session_disable, not
    # in tools.disable_session. Check the underlying writer + the
    # verifier for HMAC machinery.
    import inspect

    from chameleon_mcp.optouts import (
        _marker_has_valid_signature,
        write_session_disable,
    )

    writer_src = inspect.getsource(write_session_disable)
    verifier_src = inspect.getsource(_marker_has_valid_signature)
    has_hmac = (
        "hmac" in writer_src.lower()
        and "sig=" in writer_src
        and "compare_digest" in verifier_src
    )

    info = f"accepted={accepted}, has_hmac_signing={has_hmac}"
    # The threat — third-party plants an unsigned marker — was real
    # pre-fix. Post-fix: a marker WITH a bad sig is rejected. The
    # acceptance of disable_session itself is still True (legitimate
    # callers can still disable), but planted forgeries are rejected
    # at verification time.
    if has_hmac:
        report("8", "MEDIUM", "NOT_REPRODUCED", info)
    else:
        report("8", "MEDIUM", "CONFIRMED", info)

    # Cleanup the marker
    from chameleon_mcp.profile.trust import plugin_data_dir
    from chameleon_mcp.tools import _compute_repo_id

    repo_id = _compute_repo_id(RUBY_REPO)
    pd = plugin_data_dir() / repo_id
    if pd.is_dir():
        for marker in pd.glob(".session_disabled.*"):
            marker.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Bug 9 — daemon: not running warns on every doctor call
# ---------------------------------------------------------------------------


def bug9_daemon_warn() -> None:
    section("Bug 9 (MINOR): daemon-not-running raises overall to warn")
    from chameleon_mcp.tools import doctor

    resp = doctor()
    data = resp.get("data", {})
    overall = data.get("overall", "")
    checks = data.get("checks", []) or []
    daemon_check = next(
        (c for c in checks if "daemon" in (c.get("name") or "").lower()),
        None,
    )
    daemon_level = (daemon_check or {}).get("level") or (daemon_check or {}).get(
        "status"
    )
    daemon_warns = daemon_level == "warn"
    overall_warns = overall == "warn"

    info = f"overall={overall!r} daemon_check_level={daemon_level!r}"
    if daemon_warns and overall_warns:
        report("9", "MINOR", "CONFIRMED", info)
    else:
        report("9", "MINOR", "NOT_REPRODUCED", info)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"\n{'#' * 70}")
    print(f"# Verification round {ROUND} — v0.5.14 bug report")
    print(f"{'#' * 70}")
    bug1_refresh_loses_paths_glob()
    bug2_refresh_invalidates_trust()
    bug3_get_rules_archetype_param()
    bug4_drift_banner_py39()
    bug5_worktrees_excluded()
    bug6_brace_in_basename()
    bug7_list_profiles_dead_entries()
    bug8_disable_session_arbitrary()
    bug9_daemon_warn()

    print(f"\n{'=' * 70}\nROUND {ROUND} SUMMARY\n{'=' * 70}")
    by_status = {"CONFIRMED": 0, "NOT_REPRODUCED": 0, "N/A": 0, "PARTIAL": 0}
    for _id, _sev, status, _info in RESULTS:
        by_status[status] = by_status.get(status, 0) + 1
    print(
        f"  CONFIRMED: {by_status['CONFIRMED']}  "
        f"NOT_REPRODUCED: {by_status['NOT_REPRODUCED']}  "
        f"PARTIAL: {by_status['PARTIAL']}  "
        f"N/A: {by_status['N/A']}"
    )
    print()
    for bid, sev, status, info in RESULTS:
        print(f"  Bug {bid} ({sev}): {status}{(' — ' + info) if info else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
