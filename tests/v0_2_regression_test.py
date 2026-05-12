"""Regression tests for the v0.2.0 fixes.

Each test corresponds to a finding from the chameleon-test-report.md audit
that v0.1.1 shipped with. The test fails on v0.1.1 code; passes on v0.2.0.

Run:
    cd mcp && PYTHONPATH=.:../tests .venv/bin/python ../tests/v0_2_regression_test.py
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


# Use isolated plugin data dir per run.
TMPDATA = tempfile.mkdtemp(prefix="chameleon_v02_data_")
os.environ["CHAMELEON_PLUGIN_DATA"] = TMPDATA

from chameleon_mcp.tools import (  # noqa: E402
    bootstrap_repo,
    refresh_repo,
    teach_profile,
    trust_profile,
)


def _make_tiny_ts_repo() -> Path:
    """Create a tiny TS repo with enough files for two distinguishable archetypes."""
    root = Path(tempfile.mkdtemp(prefix="chameleon_v02_repo_"))
    (root / "package.json").write_text('{"name":"x","dependencies":{"typescript":"5.0.0"}}')
    (root / "tsconfig.json").write_text("{}")

    # Two "app/" controllers
    app_dir = root / "app" / "controllers" / "api" / "v1"
    app_dir.mkdir(parents=True)
    for i in range(6):
        (app_dir / f"r{i}.ts").write_text(
            f"export class Resource{i} {{ get() {{ return {i}; }} }}\n"
        )

    # Two "spec/" tests
    spec_dir = root / "spec" / "controllers" / "api" / "v1"
    spec_dir.mkdir(parents=True)
    for i in range(6):
        (spec_dir / f"r{i}.test.ts").write_text(
            f"import {{ Resource{i} }} from '../../app/controllers/api/v1/r{i}';\n"
            f"test('r{i}', () => {{ expect(new Resource{i}().get()).toBe({i}); }});\n"
        )

    return root


section("Critical: refresh_repo preserves user idioms")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    teach_profile(str(repo), "always use frozen string literals in Rails files")
    idioms_before = (repo / ".chameleon" / "idioms.md").read_text()
    t("teach_profile actually wrote the idiom", "frozen string literals" in idioms_before)

    refresh_repo(str(repo))
    idioms_after = (repo / ".chameleon" / "idioms.md").read_text()
    t(
        "refresh_repo preserves the captured idiom (v0.1.1 wiped this)",
        "frozen string literals" in idioms_after,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("High: teach_profile rejects empty feedback")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    r = teach_profile(str(repo), "")
    t(
        "empty feedback returns failed (v0.1.1 created orphan idiom)",
        r["data"]["status"] == "failed",
        json.dumps(r["data"]),
    )
    r = teach_profile(str(repo), "   \n\n  ")
    t("whitespace-only feedback returns failed", r["data"]["status"] == "failed")
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("High: teach_profile honors user-supplied ### slug")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    teach_profile(
        str(repo),
        "### custom-slug\nStatus: active\nBody of the idiom.",
    )
    idioms = (repo / ".chameleon" / "idioms.md").read_text()
    t("custom slug appears in idioms.md", "### custom-slug" in idioms)
    t(
        "no auto-wrapper prepended above custom slug (v0.1.1 always wrapped)",
        idioms.count("### idiom-") == 0 and idioms.count("### custom-slug") == 1,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("High: teach_profile escapes ## headings in body")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    teach_profile(
        str(repo),
        "Some text\n## deprecated\nThis would have broken the section structure.",
    )
    idioms = (repo / ".chameleon" / "idioms.md").read_text()
    # Should be only ONE "## deprecated" — the section marker. The injected one
    # in the body must be escaped.
    deprecated_count = idioms.count("\n## deprecated")
    t(
        "exactly one '## deprecated' section header survives (v0.1.1 had two)",
        deprecated_count == 1,
        f"got {deprecated_count}",
    )
    t("escaped '\\## deprecated' present in body", r"\## deprecated" in idioms)
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("High: teach_profile drops 'no idioms yet' placeholder on first add")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    placeholder = "_(no idioms yet"
    before = (repo / ".chameleon" / "idioms.md").read_text()
    t("placeholder present before first idiom", placeholder in before)

    teach_profile(str(repo), "first real idiom")
    after = (repo / ".chameleon" / "idioms.md").read_text()
    t(
        "placeholder removed after first idiom (v0.1.1 left it)",
        placeholder not in after,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("High security: profile.summary.md surfaces idiom content")
repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
    teach_profile(
        str(repo),
        "this idiom-body-marker must appear in the trust review",
    )
    refresh_repo(str(repo))
    summary = (repo / ".chameleon" / "profile.summary.md").read_text()
    t(
        "summary contains idiom body so the trust gate has something to review",
        "idiom-body-marker" in summary,
    )
    t(
        "Idioms section is no longer just a placeholder",
        "_Phase 2D: interview-driven" not in summary,
    )
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("Medium: path_pattern_bucket_for disambiguates app/ from spec/")
# The original report blamed tools.py:127's substring fallback, but the real
# bug is in signatures.path_pattern_bucket_for — v0.1.1 used parts[-3:-1]
# which collapsed both `app/controllers/api/v1/foo.rb` and
# `spec/controllers/api/v1/foo_spec.rb` into the same `"api/v1"` bucket.
from chameleon_mcp.signatures import path_pattern_bucket_for  # noqa: E402

cases = [
    (
        "app/controllers/api/v1/addresses_controller.rb",
        "spec/controllers/api/v1/addresses_controller_spec.rb",
    ),
    (
        "app/models/listing.rb",
        "spec/models/listing_spec.rb",
    ),
    (
        "app/admin/listings/foo.rb",
        "spec/admin/listings/foo_spec.rb",
    ),
]
for app_path, spec_path in cases:
    app_bucket = path_pattern_bucket_for(app_path)
    spec_bucket = path_pattern_bucket_for(spec_path)
    t(
        f"{app_path!r} ≠ {spec_path!r} buckets",
        app_bucket != spec_bucket,
        f"both bucketed as {app_bucket!r}",
    )

# Spot-check that shallow paths still produce a coherent bucket and that
# the bucket includes the top-level segment (the v4 → v5 fix).
t(
    "shallow path keeps top-level segment",
    path_pattern_bucket_for("app/controllers/foo.rb").startswith("app/"),
    path_pattern_bucket_for("app/controllers/foo.rb"),
)
t(
    "deep path keeps top-level segment",
    path_pattern_bucket_for("app/controllers/api/v1/foo.rb").startswith("app/"),
    path_pattern_bucket_for("app/controllers/api/v1/foo.rb"),
)


section("Validation: list_profiles rejects invalid params")
from chameleon_mcp.tools import list_profiles  # noqa: E402

r = list_profiles(limit=0)["data"]
t("limit=0 returns failed", r.get("status") == "failed", json.dumps(r))
r = list_profiles(limit=-5)["data"]
t("limit=-5 returns failed", r.get("status") == "failed")
r = list_profiles(limit=10000)["data"]
t("limit=10000 returns failed", r.get("status") == "failed")
r = list_profiles(cursor="not-a-real-cursor")["data"]
t("invalid cursor returns failed", r.get("status") == "failed")


section("Validation: trust_profile distinguishes path errors")
r = trust_profile("/tmp/definitely-does-not-exist-xyz", "x")["data"]
t(
    "non-existent abs path returns 'does not exist' (not 'must be absolute')",
    "does not exist" in r.get("error", ""),
    r.get("error"),
)
r = trust_profile("relative/path", "x")["data"]
t(
    # BUG-004 (v0.5.6): trust_profile now accepts an absolute path OR a
    # 64-char repo_id hex digest; the relative-path rejection message
    # changed accordingly.
    "relative (non-absolute, non-repo_id) input is rejected",
    r.get("status") == "failed" and (
        "must be absolute" in r.get("error", "")
        or "absolute repo path" in r.get("error", "")
    ),
    r.get("error"),
)


section("Schema: v0.2 profile is loadable by the validator")
# Catches the kind of half-done bump where the orchestrator writes
# schema_version=5 but `profile/schema.py` still claims SUPPORTED_SCHEMA_RANGE=(3,4),
# which would make the same engine that wrote the file unable to validate it.
from chameleon_mcp.profile.loader import load_profile_dir  # noqa: E402
from chameleon_mcp.profile.schema import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_RANGE,
)

repo = _make_tiny_ts_repo()
try:
    bootstrap_repo(str(repo))
    written = json.loads((repo / ".chameleon" / "profile.json").read_text())
    t(
        "bootstrap writes schema_version matching CURRENT_SCHEMA_VERSION",
        written.get("schema_version") == CURRENT_SCHEMA_VERSION,
        f"written={written.get('schema_version')}, current={CURRENT_SCHEMA_VERSION}",
    )
    t(
        "CURRENT_SCHEMA_VERSION is inside SUPPORTED_SCHEMA_RANGE",
        SUPPORTED_SCHEMA_RANGE[0] <= CURRENT_SCHEMA_VERSION <= SUPPORTED_SCHEMA_RANGE[1],
        str(SUPPORTED_SCHEMA_RANGE),
    )
    loaded = load_profile_dir(repo / ".chameleon")
    t("freshly-bootstrapped profile loads without ProfileLoadError", loaded is not None)
finally:
    shutil.rmtree(repo, ignore_errors=True)


section("Validation: lint_file response carries stub flag")
from chameleon_mcp.tools import lint_file  # noqa: E402

r = lint_file("/tmp/x", "fake_archetype", "anything")["data"]
t("lint_file response includes 'stub': true", r.get("stub") is True)
t("lint_file response includes stub_reason", isinstance(r.get("stub_reason"), str))


section("v0.3.1: PID-aware orphan-txn cleanup")
from chameleon_mcp.bootstrap.transaction import cleanup_orphan_tmp_dirs  # noqa: E402

cleanup_parent = Path(tempfile.mkdtemp(prefix="chameleon_v031_cleanup_"))
try:
    tmp_root = cleanup_parent / ".chameleon.tmp"
    tmp_root.mkdir()

    legacy = tmp_root / "no-pid-style"
    legacy.mkdir()
    (legacy / "profile.json").write_text("{}")

    dead = tmp_root / "999999-deadbeef-1700000000"
    dead.mkdir()
    (dead / "profile.json").write_text("{}")

    live = tmp_root / f"{os.getpid()}-livepid01-1700000000"
    live.mkdir()
    (live / "profile.json").write_text("{}")

    n = cleanup_orphan_tmp_dirs(cleanup_parent, "chameleon")
    t("legacy txn (no PID prefix) is cleaned", not legacy.is_dir())
    t("dead-PID txn is cleaned", not dead.is_dir())
    t("live-PID txn is preserved (no race with concurrent writer)", live.is_dir())
    t("cleanup count matches removed dirs", n == 2, f"got {n}")
finally:
    shutil.rmtree(cleanup_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
print("\n=== Summary ===")
print(f"  Total: {PASS + FAIL}")
print(f"  Pass: {PASS}")
print(f"  Fail: {FAIL}")
shutil.rmtree(TMPDATA, ignore_errors=True)
sys.exit(0 if FAIL == 0 else 1)
