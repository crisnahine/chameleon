"""Regression tests for v0.5.3 dogfood-cycle-2 bugs B, D, E.

Bug B (HIGH) — bootstrap_repo returns failed_unsupported_language on
    Turborepo / pnpm-workspaces / Nx style monorepos when the root
    package.json has only `scripts` (no `dependencies`/`devDependencies`)
    AND no root `tsconfig.json`. Workspaces live under `apps/*`,
    `packages/*`, `services/*`, or `workspaces/*` and carry their own
    `tsconfig.json` / TS-flavored `package.json`. v0.5.3 drills one
    level down into those first-level dirs (bounded at 50 entries) to
    detect TS at the monorepo root and surfaces the matching workspace
    dirs through a new envelope field `workspace_roots`.

Bug D (MED) — files_processed reported the post-clustering count alone.
    Coverage analysis is impossible (gitlabhq: 6,574 of ~125k disk
    files surfaced, with no visibility into where the rest went).
    v0.5.3 augments the bootstrap_repo envelope with four counters:
      - discovered_files_pre_exclusion
      - discovered_files_post_exclusion
      - clustered_files (alias for files_processed)
      - sparse_dropped_files
    The existing files_processed field is preserved for back-compat.

Bug E (LOW) — _is_rails_with_frontend only accepted `app/javascript/`
    (Rails 6+ webpacker/esbuild). gitlabhq uses the older
    `app/assets/javascripts/` (Rails 5 sprockets) layout and was
    misclassified. v0.5.3 broadens the predicate to also accept
    `app/assets/javascripts/` and `app/frontend/` (Rails 7).

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_5_3_monorepo_bootstrap_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the in-repo chameleon_mcp importable without installing.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "mcp"))


# Isolate plugin data so trust grants / drift dbs don't leak across runs.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v053_monorepo_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA


PASS = 0
FAIL = 0


def t(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# Late imports so sys.path manipulation above takes effect.
from chameleon_mcp.bootstrap.discovery import (  # noqa: E402
    discovery_stats,
)
from chameleon_mcp.bootstrap.orchestrator import (  # noqa: E402
    _is_rails_with_frontend,
    bootstrap_repo,
)

_MIN_TS_PKG_JSON = json.dumps(
    {
        "name": "example",
        "version": "1.0.0",
        "dependencies": {"typescript": "5.0.0"},
    },
    indent=2,
)
_MIN_TS_TSCONFIG = json.dumps({"compilerOptions": {"strict": True}}, indent=2)


def _write_scripts_only_pkg(root: Path) -> None:
    """Write a root package.json that has only `scripts` (Bug B repro)."""
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "repo-root",
                "private": True,
                "scripts": {"build": "echo build"},
            },
            indent=2,
        )
    )


def _make_ts_workspace(ws_root: Path, *, file_count: int = 4) -> None:
    """Create a workspace dir with tsconfig + a package.json carrying TS deps."""
    ws_root.mkdir(parents=True, exist_ok=True)
    (ws_root / "tsconfig.json").write_text(_MIN_TS_TSCONFIG)
    (ws_root / "package.json").write_text(_MIN_TS_PKG_JSON)
    src_dir = ws_root / "src"
    src_dir.mkdir(exist_ok=True)
    # Several files so clustering has something to bucket on.
    for i in range(file_count):
        (src_dir / f"mod_{i:02d}.ts").write_text(
            "export const value = "
            f"{i}"
            ";\nexport function compute() {\n  return value;\n}\n"
        )


# ---------------------------------------------------------------------------
# Bug B — monorepo workspace detection
# ---------------------------------------------------------------------------
section("Bug B — Turborepo (apps/*) detection")

# Verify-before: a root package.json with only `scripts` AND no root
# tsconfig.json AND TS-bearing workspaces under apps/ MUST bootstrap
# successfully. Before v0.5.3 this returned failed_unsupported_language.
bug_b_root = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugB_turbo_"))
_write_scripts_only_pkg(bug_b_root)
_make_ts_workspace(bug_b_root / "apps" / "web")
_make_ts_workspace(bug_b_root / "apps" / "api")

report = bootstrap_repo(bug_b_root.resolve())
report_dict = report.to_dict()

# Verify-after: succeeds and workspace_roots envelope field lists both dirs.
t(
    "Turborepo apps/* bootstrap succeeds",
    report.status == "success",
    f"got {report.status} ({report.error})",
)
t(
    "envelope carries workspace_roots key",
    "workspace_roots" in report_dict,
)
wsr = report_dict.get("workspace_roots") or []
t(
    "workspace_roots lists apps/web",
    "apps/web" in wsr,
    f"got {wsr}",
)
t(
    "workspace_roots lists apps/api",
    "apps/api" in wsr,
    f"got {wsr}",
)
t(
    "fanout_capped is False (only 2 dirs, well under 50)",
    report_dict.get("fanout_capped") is False,
    f"got {report_dict.get('fanout_capped')!r}",
)

shutil.rmtree(bug_b_root, ignore_errors=True)


section("Bug B — pnpm packages/* detection")

bug_b_pnpm = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugB_pnpm_"))
_write_scripts_only_pkg(bug_b_pnpm)
_make_ts_workspace(bug_b_pnpm / "packages" / "foo")
_make_ts_workspace(bug_b_pnpm / "packages" / "bar")

report = bootstrap_repo(bug_b_pnpm.resolve())
report_dict = report.to_dict()

# Verify-after: detects both workspaces under packages/.
t(
    "pnpm packages/* bootstrap succeeds",
    report.status == "success",
    f"got {report.status} ({report.error})",
)
wsr = report_dict.get("workspace_roots") or []
t(
    "workspace_roots lists packages/foo",
    "packages/foo" in wsr,
    f"got {wsr}",
)
t(
    "workspace_roots lists packages/bar",
    "packages/bar" in wsr,
    f"got {wsr}",
)

shutil.rmtree(bug_b_pnpm, ignore_errors=True)


section("Bug B — misconfigured tree stays failed_unsupported_language")

# Verify-before: an empty root package.json with NO workspaces that carry
# TS signals must still report failed_unsupported_language. The fanout
# must not "drill into garbage" and invent a TS repo where none exists.
bug_b_bad = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugB_bad_"))
_write_scripts_only_pkg(bug_b_bad)
# Empty apps/ and packages/ dirs — discovered but carry no TS signals.
(bug_b_bad / "apps" / "noop").mkdir(parents=True)
(bug_b_bad / "apps" / "noop" / "README.md").write_text("nothing here\n")
(bug_b_bad / "packages" / "empty").mkdir(parents=True)

report = bootstrap_repo(bug_b_bad.resolve())

# Verify-after: still fails with the unsupported-language envelope.
t(
    "misconfigured tree returns failed_unsupported_language",
    report.status == "failed_unsupported_language",
    f"got {report.status} ({report.error})",
)

shutil.rmtree(bug_b_bad, ignore_errors=True)


section("Bug B — single-root TS repo unchanged (no regression)")

# Verify-before: a repo with root tsconfig.json AND TS deps must use the
# root pipeline as before (workspace_roots should be empty or absent).
bug_b_single = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugB_single_"))
(bug_b_single / "package.json").write_text(_MIN_TS_PKG_JSON)
(bug_b_single / "tsconfig.json").write_text(_MIN_TS_TSCONFIG)
src_dir = bug_b_single / "src"
src_dir.mkdir()
for i in range(4):
    (src_dir / f"f{i}.ts").write_text(
        f"export const x = {i};\nexport function f() {{ return x; }}\n"
    )
# Even if there are apps/ or packages/ dirs with TS, the root takes
# precedence because the root itself satisfies _select_extractor.
(bug_b_single / "apps").mkdir()
_make_ts_workspace(bug_b_single / "apps" / "extra")

report = bootstrap_repo(bug_b_single.resolve())
report_dict = report.to_dict()
# Verify-after: bootstraps from the root; workspace_roots is empty/None.
t(
    "single-root TS repo bootstrap succeeds",
    report.status == "success",
    f"got {report.status} ({report.error})",
)
t(
    "single-root TS repo: workspace_roots empty (root wins)",
    not report_dict.get("workspace_roots"),
    f"got {report_dict.get('workspace_roots')!r}",
)

shutil.rmtree(bug_b_single, ignore_errors=True)


section("Bug B — fanout cap at 50 first-level dirs")

# Verify-before: a misconfigured tree with 51+ first-level apps/* dirs
# (all empty / non-TS) MUST cap the scan at 50 and set fanout_capped=True
# in the envelope. The scan is bounded so a pathological tree can't walk
# forever.
bug_b_overflow = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugB_overflow_"))
_write_scripts_only_pkg(bug_b_overflow)
apps_dir = bug_b_overflow / "apps"
apps_dir.mkdir()
# 51 dirs: 50 empty + 1 with TS at index 50 (sorted alphabetically the
# real TS dir would NOT win because the cap drops it). We use ws_00..ws_50
# names; ws_50 is the one carrying TS. The cap should keep dirs ws_00..ws_49,
# meaning the only TS dir is NOT included → bootstrap should still fail.
# The contract is "cap, then check". Names < 50 = empty, name 50 = TS.
for i in range(50):
    d = apps_dir / f"ws_{i:02d}"
    d.mkdir()
# 51st entry: the real TS workspace.
_make_ts_workspace(apps_dir / "ws_50")

report = bootstrap_repo(bug_b_overflow.resolve())
report_dict = report.to_dict()
# Verify-after: scan capped; fanout_capped=True. Bootstrap may succeed or
# fail depending on whether the cap caught the TS dir, but the cap flag
# must be set regardless.
t(
    "fanout_capped envelope flag present",
    "fanout_capped" in report_dict,
)
t(
    "fanout_capped is True when first-level dir count > 50",
    report_dict.get("fanout_capped") is True,
    f"got {report_dict.get('fanout_capped')!r}, status={report.status}",
)

shutil.rmtree(bug_b_overflow, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bug D — instrumentation counters
# ---------------------------------------------------------------------------
section("Bug D — four new envelope counters")

# Verify-before: bootstrap a small synthetic TS repo and check the four
# counters are present in the envelope and have the expected ordering.
bug_d_root = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugD_"))
(bug_d_root / "package.json").write_text(_MIN_TS_PKG_JSON)
(bug_d_root / "tsconfig.json").write_text(_MIN_TS_TSCONFIG)
src_dir = bug_d_root / "src"
src_dir.mkdir()
for i in range(6):
    (src_dir / f"file_{i:02d}.ts").write_text(
        f"export const v = {i};\nexport function fn() {{ return v; }}\n"
    )

report = bootstrap_repo(bug_d_root.resolve())
report_dict = report.to_dict()

t(
    "Bug D synthetic repo bootstrap succeeds",
    report.status == "success",
    f"got {report.status} ({report.error})",
)
# Verify-after: all four counter keys present.
for key in (
    "discovered_files_pre_exclusion",
    "discovered_files_post_exclusion",
    "clustered_files",
    "sparse_dropped_files",
):
    t(
        f"envelope carries `{key}`",
        key in report_dict,
        f"keys={sorted(report_dict.keys())}",
    )
    t(
        f"`{key}` is an int",
        isinstance(report_dict.get(key), int),
        f"got {type(report_dict.get(key)).__name__}",
    )
pre = report_dict.get("discovered_files_pre_exclusion", -1)
post = report_dict.get("discovered_files_post_exclusion", -1)
clustered = report_dict.get("clustered_files", -1)
sparse = report_dict.get("sparse_dropped_files", -1)
t(
    "ordering: pre >= post",
    pre >= post,
    f"pre={pre}, post={post}",
)
t(
    "ordering: post >= clustered",
    post >= clustered,
    f"post={post}, clustered={clustered}",
)
t(
    "clustered >= 0",
    clustered >= 0,
    f"got {clustered}",
)
t(
    "sparse_dropped_files >= 0",
    sparse >= 0,
    f"got {sparse}",
)
t(
    "clustered_files alias matches files_processed",
    clustered == report_dict.get("files_processed"),
    f"clustered={clustered}, files_processed={report_dict.get('files_processed')}",
)

shutil.rmtree(bug_d_root, ignore_errors=True)


section("Bug D — node_modules exclusion shows up in pre-post delta")

# Verify-before: drop 1000 stub files into node_modules. Discovery must
# walk them (pre count includes them) but exclusion must drop them
# (post count excludes them). Delta >= 1000.
bug_d_excl = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugD_excl_"))
(bug_d_excl / "package.json").write_text(_MIN_TS_PKG_JSON)
(bug_d_excl / "tsconfig.json").write_text(_MIN_TS_TSCONFIG)
# Hand-authored files.
src = bug_d_excl / "src"
src.mkdir()
for i in range(4):
    (src / f"f{i}.ts").write_text(
        f"export const v = {i};\nexport function fn() {{ return v; }}\n"
    )
# 1000 stub node_modules .ts files.
node_modules = bug_d_excl / "node_modules" / "fake-pkg"
node_modules.mkdir(parents=True)
for i in range(1000):
    (node_modules / f"stub_{i:04d}.ts").write_text("export const x = 0;\n")

report = bootstrap_repo(bug_d_excl.resolve())
report_dict = report.to_dict()
pre = report_dict.get("discovered_files_pre_exclusion", -1)
post = report_dict.get("discovered_files_post_exclusion", -1)

t(
    "Bug D node_modules bootstrap succeeds",
    report.status == "success",
    f"got {report.status} ({report.error})",
)
t(
    "node_modules exclusion: pre - post >= 1000",
    pre - post >= 1000,
    f"pre={pre}, post={post}, delta={pre - post}",
)

shutil.rmtree(bug_d_excl, ignore_errors=True)


section("Bug D — discovery_stats helper returns pre/post counters")

# Verify-before: the underlying helper exposes the same counters so other
# callers (refresh, status) can reuse them without re-walking the tree.
bug_d_stats = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugD_stats_"))
(bug_d_stats / "package.json").write_text(_MIN_TS_PKG_JSON)
(bug_d_stats / "tsconfig.json").write_text(_MIN_TS_TSCONFIG)
(bug_d_stats / "src").mkdir()
for i in range(3):
    (bug_d_stats / "src" / f"x{i}.ts").write_text(f"export const v = {i};\n")
# Add a node_modules pile so pre-post differ.
(bug_d_stats / "node_modules" / "ignored").mkdir(parents=True)
for i in range(5):
    (bug_d_stats / "node_modules" / "ignored" / f"y{i}.ts").write_text(
        "export const v = 1;\n"
    )

stats = discovery_stats(bug_d_stats, glob="**/*.{ts,tsx,js,jsx,mjs,cjs}")
t(
    "discovery_stats returns dict with pre/post keys",
    isinstance(stats, dict)
    and "pre_exclusion" in stats
    and "post_exclusion" in stats,
    f"got {stats!r}",
)
t(
    "discovery_stats pre_exclusion >= post_exclusion",
    stats.get("pre_exclusion", 0) >= stats.get("post_exclusion", 0),
)
t(
    "discovery_stats observes the node_modules stubs",
    stats.get("pre_exclusion", 0) >= 8,  # 3 src + 5 stubs
)

shutil.rmtree(bug_d_stats, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bug E — _is_rails_with_frontend accepts legacy / modern / new layouts
# ---------------------------------------------------------------------------
section("Bug E — broadened Rails+JS frontend detection")


def _make_rails_root(root: Path) -> None:
    (root / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "application.rb").write_text("# Rails\n")


# Legacy Rails 5 sprockets layout.
bug_e_legacy = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugE_legacy_"))
_make_rails_root(bug_e_legacy)
(bug_e_legacy / "app" / "assets" / "javascripts").mkdir(parents=True)
(bug_e_legacy / "app" / "assets" / "javascripts" / "foo.js").write_text(
    "console.log('hi');\n"
)
t(
    "legacy app/assets/javascripts/ triggers _is_rails_with_frontend",
    _is_rails_with_frontend(bug_e_legacy) is True,
)
shutil.rmtree(bug_e_legacy, ignore_errors=True)

# Modern Rails 6+ webpacker / esbuild layout.
bug_e_modern = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugE_modern_"))
_make_rails_root(bug_e_modern)
(bug_e_modern / "app" / "javascript").mkdir(parents=True)
(bug_e_modern / "app" / "javascript" / "foo.js").write_text("export {};\n")
t(
    "modern app/javascript/ triggers _is_rails_with_frontend",
    _is_rails_with_frontend(bug_e_modern) is True,
)
shutil.rmtree(bug_e_modern, ignore_errors=True)

# Rails 7 newer app/frontend/ convention.
bug_e_new = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugE_new_"))
_make_rails_root(bug_e_new)
(bug_e_new / "app" / "frontend").mkdir(parents=True)
(bug_e_new / "app" / "frontend" / "main.tsx").write_text(
    "export const App = () => null;\n"
)
t(
    "new app/frontend/ triggers _is_rails_with_frontend",
    _is_rails_with_frontend(bug_e_new) is True,
)
shutil.rmtree(bug_e_new, ignore_errors=True)

# Rails repo with no JS dir at all.
bug_e_none = Path(tempfile.mkdtemp(prefix="chameleon_v053_bugE_none_"))
_make_rails_root(bug_e_none)
t(
    "Rails without any JS dir does NOT trigger _is_rails_with_frontend",
    _is_rails_with_frontend(bug_e_none) is False,
)
shutil.rmtree(bug_e_none, ignore_errors=True)


section("Bug E — real gitlabhq path (legacy assets/javascripts layout)")

# Verify-after on the real repo: confirms the broadened predicate fires
# on the legacy Rails 5 layout. Set CHAMELEON_TEST_APPS_DIR to a directory
# containing a `gitlabhq` checkout to exercise this; skips gracefully when
# unset or absent.
_apps_dir = os.environ.get("CHAMELEON_TEST_APPS_DIR")
gitlabhq = Path(_apps_dir) / "gitlabhq" if _apps_dir else None
if gitlabhq and gitlabhq.is_dir():
    t(
        "real gitlabhq triggers _is_rails_with_frontend",
        _is_rails_with_frontend(gitlabhq) is True,
    )
else:
    print("  [SKIP] CHAMELEON_TEST_APPS_DIR/gitlabhq not available; skipping real-repo check")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section("Summary")
print(f"\n  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
sys.exit(1 if FAIL else 0)
