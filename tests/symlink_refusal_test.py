"""Tests for rec 13: symlink refusal in discovery + AST extractors.

discover_files / _count_candidates drop symlinks from the candidate set so
the AST extractor scripts never see them. ts_dump.mjs and prism_dump.rb
still defend independently (belt-and-suspenders for direct-CLI use) by
returning {path, error: "symlink_refused"} when handed a symlink path.

The threat: a teammate-planted source-tree symlink (.ts -> /etc/passwd)
would otherwise have its target's first MAX_FILE_SIZE bytes read into
content_signal_match_for and into the canonical excerpt cache.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from chameleon_mcp.bootstrap.discovery import discover_files, discovery_stats

# Windows requires admin or developer mode for symlink creation. Skip
# cleanly rather than false-fail on a Windows runner.
if sys.platform == "win32":
    print("symlink_refusal_test: skipped on win32 (symlink_to requires admin)")
    sys.exit(0)

PASS: list[tuple[str, str]] = []
FAIL: list[tuple[str, str]] = []


def t(name: str, condition: bool, info: str = "") -> None:
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + info) if info else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


REPO_ROOT = Path(__file__).resolve().parent.parent


section("discover_files / discovery_stats skip symlinks")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "real.ts").write_text("export const x = 1;\n", encoding="utf-8")
    # Symlink to a nonexistent (broken) in-tempdir target. The threat
    # the rec closes is "the symlink itself is read"; we don't need to
    # actually point at a sensitive file to verify refusal. Avoiding
    # /etc/passwd keeps the test hermetic on systems where that path
    # is absent (containers, minimal images).
    outside = Path(td) / "_nonexistent_target"
    (repo / "evil.ts").symlink_to(outside)
    # Also a symlink to a real in-repo file (still must be refused —
    # the threat is the link itself, regardless of target)
    (repo / "alias.ts").symlink_to(repo / "real.ts")

    files = discover_files(repo)
    names = sorted(p.name for p in files)
    t("only real.ts returned", names == ["real.ts"], str(names))

    counts = discovery_stats(repo)
    # Glob walked 3 candidates; symlinks dropped before counting.
    t(
        "discovery_stats does NOT count symlinks pre-exclusion",
        counts["pre_exclusion"] == 1,
        f"pre={counts['pre_exclusion']}",
    )
    t(
        "discovery_stats does NOT count symlinks post-exclusion",
        counts["post_exclusion"] == 1,
        f"post={counts['post_exclusion']}",
    )


section("ts_dump.mjs refuses symlink with symlink_refused reason")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    target = repo / "real.ts"
    target.write_text("export const x = 1;\n", encoding="utf-8")
    link = repo / "alias.ts"
    link.symlink_to(target)

    script = REPO_ROOT / "scripts" / "ts_dump.mjs"
    node = shutil.which("node")
    if not script.is_file():
        t("ts_dump.mjs present", False, str(script))
    elif node is None:
        print("  [SKIP] node not on PATH — script-side defense unverified here")
    else:
        proc = subprocess.run(
            [node, str(script)],
            input=f"{link}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # ts_dump.mjs streams NDJSON
        records = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        link_record = next((r for r in records if r.get("path") == str(link)), None)
        t("ts_dump.mjs produced a record for the symlink", link_record is not None)
        if link_record is not None:
            t(
                "ts_dump.mjs reason is symlink_refused",
                link_record.get("error") == "symlink_refused",
                str(link_record),
            )


section("prism_dump.rb refuses symlink with symlink_refused reason")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    target = repo / "real.rb"
    target.write_text("class X; end\n", encoding="utf-8")
    link = repo / "alias.rb"
    link.symlink_to(target)

    script = REPO_ROOT / "scripts" / "prism_dump.rb"
    ruby = shutil.which("ruby")
    if not script.is_file():
        t("prism_dump.rb present", False, str(script))
    elif ruby is None:
        print("  [SKIP] ruby not on PATH — script-side defense unverified here")
    else:
        proc = subprocess.run(
            [ruby, str(script)],
            input=f"{link}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        records = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        link_record = next((r for r in records if r.get("path") == str(link)), None)
        t("prism_dump.rb produced a record for the symlink", link_record is not None)
        if link_record is not None:
            t(
                "prism_dump.rb reason is symlink_refused",
                link_record.get("error") == "symlink_refused",
                str(link_record),
            )


section("real (non-symlink) files unaffected by rec 13")
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    p = repo / "real.ts"
    p.write_text("export const x = 1;\n", encoding="utf-8")
    files = discover_files(repo)
    t("real file still returned", any(f.name == "real.ts" for f in files))


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
