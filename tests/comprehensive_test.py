"""Comprehensive integration test for chameleon, exercising every component
against the real EF api (Ruby/Rails) and EF client (TypeScript) repositories.

Run with the venv python:
    cd mcp && PYTHONPATH=. .venv/bin/python ../tests/comprehensive_test.py

Goes beyond smoke_test.py — this hits every MCP tool, every hook bash script,
every helper, and every documented invariant against real EF code.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PASS, FAIL = [], []

EF_API = Path("/Users/crisn/Documents/Projects/empire-flippers/api")
EF_CLIENT = Path("/Users/crisn/Documents/Projects/empire-flippers/client")
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")
SCRIPTS = PLUGIN_ROOT / "scripts"
HOOKS = PLUGIN_ROOT / "hooks"


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. ts_dump.mjs direct invocation on real EF client files
# ---------------------------------------------------------------------------
section("ts_dump.mjs on real EF client files")

ef_client_files = [
    EF_CLIENT / "src" / "index.tsx",
    EF_CLIENT / "src" / "components" / "base" / "SelectVettingStatus.tsx",
    EF_CLIENT / "src" / "queries" / "admin" / "users" / "create.ts",
    EF_CLIENT / "src" / "utils" / "balanceTransaction.ts",
]
existing = [str(f) for f in ef_client_files if f.is_file()]
input_data = "\n".join(existing) + "\n"

proc = subprocess.run(
    ["node", str(SCRIPTS / "ts_dump.mjs")],
    input=input_data,
    capture_output=True,
    text=True,
    timeout=60,
)
records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
t("ts_dump.mjs returns one record per file", len(records) == len(existing))
t(
    "ts_dump.mjs emits expected fields",
    all("top_level_node_kinds" in r and "import_specifiers" in r for r in records if "error" not in r),
)
t(
    "ts_dump.mjs detects JSX in .tsx files",
    any(r.get("has_jsx") for r in records if r.get("path", "").endswith(".tsx")),
)


# ---------------------------------------------------------------------------
# 2. prism_dump.rb direct invocation on real EF api files
# ---------------------------------------------------------------------------
section("prism_dump.rb on real EF api files")

ef_api_files = [
    EF_API / "app" / "models" / "listing.rb",
    EF_API / "app" / "controllers" / "api" / "v1" / "addresses_controller.rb",
    EF_API / "app" / "services" / "api" / "v1" / "users" / "create.rb",
]
existing_api = [str(f) for f in ef_api_files if f.is_file()]
input_data = "\n".join(existing_api) + "\n"

proc = subprocess.run(
    ["ruby", str(SCRIPTS / "prism_dump.rb")],
    input=input_data,
    capture_output=True,
    text=True,
    timeout=60,
)
records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
t("prism_dump.rb returns one record per file", len(records) == len(existing_api))
t(
    "prism_dump.rb emits expected fields",
    all("top_level_node_kinds" in r for r in records if "error" not in r),
)
t(
    "prism_dump.rb detects ClassNode in models",
    any(
        "ClassNode" in r.get("top_level_node_kinds", [])
        for r in records
        if r.get("path", "").endswith("listing.rb")
    ),
)
t(
    "prism_dump.rb has_jsx is always false for Ruby",
    all(not r.get("has_jsx", False) for r in records),
)


# ---------------------------------------------------------------------------
# 3. Bootstrap idempotence — run twice, verify byte-identical profiles
# ---------------------------------------------------------------------------
section("Bootstrap idempotence (deterministic profiles)")

from chameleon_mcp.tools import bootstrap_repo

# Wipe + bootstrap EF client twice
import shutil

shutil.rmtree(EF_CLIENT / ".chameleon", ignore_errors=True)
r1 = bootstrap_repo(str(EF_CLIENT))["data"]
profile1_archetypes_json = (EF_CLIENT / ".chameleon" / "archetypes.json").read_text()

shutil.rmtree(EF_CLIENT / ".chameleon", ignore_errors=True)
r2 = bootstrap_repo(str(EF_CLIENT))["data"]
profile2_archetypes_json = (EF_CLIENT / ".chameleon" / "archetypes.json").read_text()

# Generation counter differs (it's a timestamp); but the archetype data should match.
import re

def strip_generation(text):
    return re.sub(r'"generation":\s*\d+', '"generation": 0', text)

a1 = strip_generation(profile1_archetypes_json)
a2 = strip_generation(profile2_archetypes_json)
t("EF client bootstrap is idempotent (same archetypes)", a1 == a2)
t("Both runs detect same archetype count", r1["archetypes_detected"] == r2["archetypes_detected"])


# ---------------------------------------------------------------------------
# 4. Workspace detection on real EF repos
# ---------------------------------------------------------------------------
section("Workspace detection (real EF repos)")

from chameleon_mcp.bootstrap.workspace import detect_workspace

ws_client = detect_workspace(EF_CLIENT)
ws_api = detect_workspace(EF_API)
t(
    "EF client not detected as workspace (single-package)",
    not ws_client.is_workspace,
)
t(
    "EF api not detected as workspace (Rails app)",
    not ws_api.is_workspace,
)


# ---------------------------------------------------------------------------
# 5. Tool config reading on real EF configs
# ---------------------------------------------------------------------------
section("Tool config reading (real EF configs)")

from chameleon_mcp.bootstrap.tool_config import read_tool_configs

tc_client = read_tool_configs(EF_CLIENT)
t("EF client: prettier config detected", tc_client.prettier is not None)
t(
    "EF client: prettier semi=false (matches .prettierrc)",
    tc_client.prettier.get("semi") is False,
)
t("EF client: tsconfig detected", tc_client.tsconfig is not None)
t(
    "EF client: tsconfig strict=true",
    tc_client.tsconfig.get("compilerOptions", {}).get("strict") is True,
)
t(
    "EF client: tsconfig path alias ~/* → src/* (per CLAUDE.md)",
    "~/*" in (tc_client.tsconfig.get("compilerOptions", {}).get("paths") or {}),
)
t(
    "EF client: ESLint JS plugins detected (warning surfaced)",
    tc_client.has_eslint_js_plugins,
)


# ---------------------------------------------------------------------------
# 6. Multi-file detect_repo + get_pattern_context (TS)
# ---------------------------------------------------------------------------
section("MCP tools across many EF client files")

from chameleon_mcp.tools import detect_repo, get_pattern_context

ts_test_files = [
    EF_CLIENT / "src" / "components" / "base" / "SelectVettingStatus.tsx",
    EF_CLIENT / "src" / "queries" / "admin" / "users" / "create.ts",
    EF_CLIENT / "src" / "utils" / "balanceTransaction.ts",
    EF_CLIENT / "src" / "types" / "AmazonProductLandedCosts.ts",
]
all_present = True
all_archetypes = True
for tf in ts_test_files:
    if not tf.is_file():
        continue
    r = detect_repo(str(tf))
    if r["data"]["profile_status"] != "profile_present":
        all_present = False
    r = get_pattern_context(str(tf))
    if r["data"]["archetype"]["archetype"] is None:
        all_archetypes = False

t("detect_repo: profile_present for all EF client test files", all_present)
t("get_pattern_context: archetype matched for all EF client test files", all_archetypes)


# ---------------------------------------------------------------------------
# 7. Multi-file detect_repo + get_pattern_context (Ruby/Rails)
# ---------------------------------------------------------------------------
section("MCP tools across many EF api files")

# Ensure .chameleon exists for EF api
if not (EF_API / ".chameleon" / "profile.json").is_file():
    bootstrap_repo(str(EF_API))

from chameleon_mcp.tools import trust_profile

trust_profile(str(EF_API), "api")

rb_test_files = [
    EF_API / "app" / "models" / "listing.rb",
    EF_API / "app" / "controllers" / "api" / "v1" / "addresses_controller.rb",
    EF_API / "app" / "services" / "api" / "v1" / "users" / "create.rb",
    EF_API / "app" / "workers" / "workers" / "listing_summaries" / "post_sale_summarize_feedback_worker.rb",
]
matched = 0
for tf in rb_test_files:
    if not tf.is_file():
        continue
    r = get_pattern_context(str(tf))
    if r["data"]["archetype"]["archetype"] is not None:
        matched += 1

t(
    "Ruby: get_pattern_context matched majority",
    matched >= 2,
    f"{matched} of {len(rb_test_files)} matched",
)


# ---------------------------------------------------------------------------
# 8. Profile loader (double-fstat + generation consistency)
# ---------------------------------------------------------------------------
section("Profile loader on real EF profiles")

from chameleon_mcp.profile.loader import ProfileLoadError, load_profile_dir

loaded_client = load_profile_dir(EF_CLIENT / ".chameleon")
loaded_api = load_profile_dir(EF_API / ".chameleon")
t("EF client profile loads", loaded_client is not None)
t("EF api profile loads", loaded_api is not None)
t("EF client mtime_token non-empty", bool(loaded_client.mtime_token))
t("EF api archetype names list populated", len(loaded_api.archetype_names) > 0)


# ---------------------------------------------------------------------------
# 9. Profile loader rejects malformed profile (missing COMMITTED sentinel)
# ---------------------------------------------------------------------------
section("Profile loader rejection paths")

with tempfile.TemporaryDirectory() as tmp:
    bad_dir = Path(tmp) / ".chameleon"
    bad_dir.mkdir()
    # Write all 4 artifacts but NO COMMITTED sentinel
    for name in ("profile.json", "archetypes.json", "rules.json", "canonicals.json"):
        (bad_dir / name).write_text(
            '{"schema_version": 4, "engine_min_version": "0.1.0", "generation": 1}'
        )
    try:
        load_profile_dir(bad_dir)
        t("Loader rejects missing COMMITTED sentinel", False, "expected exception")
    except ProfileLoadError:
        t("Loader rejects missing COMMITTED sentinel", True)


# ---------------------------------------------------------------------------
# 10. Profile loader rejects generation mismatch
# ---------------------------------------------------------------------------
section("Profile loader generation mismatch")

with tempfile.TemporaryDirectory() as tmp:
    bad_dir = Path(tmp) / ".chameleon"
    bad_dir.mkdir()
    # 4 artifacts with mismatched generation counters
    for name, gen in [
        ("profile.json", 1),
        ("archetypes.json", 1),
        ("rules.json", 2),  # mismatch!
        ("canonicals.json", 1),
    ]:
        (bad_dir / name).write_text(
            f'{{"schema_version": 4, "engine_min_version": "0.1.0", "generation": {gen}}}'
        )
    (bad_dir / "COMMITTED").write_text("ok")
    try:
        load_profile_dir(bad_dir)
        t("Loader rejects generation mismatch", False, "expected exception")
    except ProfileLoadError:
        t("Loader rejects generation mismatch", True)


# ---------------------------------------------------------------------------
# 11. Hook bash scripts end-to-end (all 4 events)
# ---------------------------------------------------------------------------
section("Hook bash scripts (end-to-end via subprocess)")

env = os.environ.copy()
env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)

# session-start
proc = subprocess.run(
    [str(HOOKS / "session-start")],
    input="",
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
out = json.loads(proc.stdout)
t("hooks/session-start emits valid JSON", isinstance(out, dict))
t(
    "hooks/session-start contains using-chameleon",
    "using-chameleon" in str(out),
)

# preflight-and-advise on real EF client file
hook_input = json.dumps({
    "tool_name": "Edit",
    "tool_input": {
        "file_path": str(EF_CLIENT / "src" / "components" / "base" / "SelectVettingStatus.tsx"),
    },
    "session_id": "comp-test-1",
})
proc = subprocess.run(
    [str(HOOKS / "preflight-and-advise")],
    input=hook_input,
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
out = json.loads(proc.stdout)
ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
t("hooks/preflight-and-advise injects archetype context", "[chameleon: archetype=" in ctx)

# posttool-recorder
hook_input = json.dumps({
    "tool_name": "Bash",
    "tool_input": {"command": "echo comprehensive-test"},
    "tool_response": {"returnCode": 0},
    "session_id": "comp-test-1",
})
proc = subprocess.run(
    [str(HOOKS / "posttool-recorder")],
    input=hook_input,
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
t("hooks/posttool-recorder runs cleanly", proc.returncode == 0)

# callout-detector with frustration
hook_input = json.dumps({"user_prompt": "ugh, chameleon is wrong again"})
proc = subprocess.run(
    [str(HOOKS / "callout-detector")],
    input=hook_input,
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
out = json.loads(proc.stdout)
t(
    "hooks/callout-detector surfaces hint on frustration",
    "/chameleon-disable" in str(out),
)


# ---------------------------------------------------------------------------
# 12. Material-change detection (modify profile, verify hash mismatch)
# ---------------------------------------------------------------------------
section("Trust material-change detection")

from chameleon_mcp.profile.trust import (
    grant_trust, hash_profile, is_material_change, repo_data_dir,
    trust_state_for,
)

repo_id = hashlib.sha256(str(EF_CLIENT.resolve()).encode()).hexdigest()
profile_dir = EF_CLIENT / ".chameleon"
record = grant_trust(repo_id, profile_dir)
t("grant_trust returns record", record.profile_sha256 != "")
t("trust_state_for returns record after grant", trust_state_for(repo_id) is not None)
t("is_material_change is False right after grant", not is_material_change(repo_id, profile_dir))

# Touch profile.json to force re-hash mismatch
profile_json = profile_dir / "profile.json"
original_content = profile_json.read_text()
profile_json.write_text(original_content + "\n")  # trailing newline change
t("is_material_change is True after profile modified", is_material_change(repo_id, profile_dir))
# Restore
profile_json.write_text(original_content)


# ---------------------------------------------------------------------------
# 13. Concurrent flock (two processes try to acquire same lock)
# ---------------------------------------------------------------------------
section("Concurrent advisory lock (flock)")

# Run two python subprocesses in parallel, one holds the lock, the other should fail.
lock_test_script = """
import sys, time
from pathlib import Path
from chameleon_mcp.locks import LockHeldError, acquire_advisory_lock

lock_path = Path(sys.argv[1])
hold_seconds = float(sys.argv[2])
try:
    with acquire_advisory_lock(lock_path):
        time.sleep(hold_seconds)
    print("ACQUIRED")
except LockHeldError:
    print("HELD")
"""

with tempfile.TemporaryDirectory() as tmp:
    lock_path = Path(tmp) / "comp.lock"
    # Start first process holding lock for 2s
    p1 = subprocess.Popen(
        [sys.executable, "-c", lock_test_script, str(lock_path), "2.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)  # let p1 acquire
    # Try to acquire from another process — should fail with LockHeldError
    p2 = subprocess.run(
        [sys.executable, "-c", lock_test_script, str(lock_path), "0.1"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    p1_out = p1.communicate(timeout=10)[0]
    t("First process acquired the lock", "ACQUIRED" in p1_out)
    t("Second process refused (lock held)", "HELD" in p2.stdout)


# ---------------------------------------------------------------------------
# 14. Bootstrap edge case: empty repo
# ---------------------------------------------------------------------------
section("Bootstrap edge cases")

with tempfile.TemporaryDirectory() as tmp:
    empty = Path(tmp) / "empty_repo"
    empty.mkdir()
    # No files; no language signals
    r = bootstrap_repo(str(empty))["data"]
    t(
        "Bootstrap on no-language repo returns failed_unsupported_language",
        r["status"] == "failed_unsupported_language",
    )

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "ts_repo"
    repo.mkdir()
    (repo / "tsconfig.json").write_text("{}")
    # No .ts files; tsconfig present
    r = bootstrap_repo(str(repo))["data"]
    t(
        "Bootstrap on TS repo with 0 files returns failed",
        r["status"] == "failed",
    )


# ---------------------------------------------------------------------------
# 15. SQLite hardening: pragmas applied
# ---------------------------------------------------------------------------
section("SQLite hardening (pragmas verified)")

from chameleon_mcp.drift.schema import init_drift_db

with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "drift.db"
    conn = init_drift_db(db_path)
    # Verify each pragma applied
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    t("WAL journal mode", journal_mode.lower() == "wal")
    t("busy_timeout >= 30000", busy_timeout >= 30000)
    t("synchronous = NORMAL (1)", synchronous == 1)
    # Schema tables created
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    t("files table created", "files" in tables)
    t("edit_observations table created", "edit_observations" in tables)
    t("schema_meta table created", "schema_meta" in tables)
    conn.close()


# ---------------------------------------------------------------------------
# 16. Discovery exclusions on EF client (node_modules, build, dist, etc.)
# ---------------------------------------------------------------------------
section("Discovery exclusions (real EF client)")

from chameleon_mcp.bootstrap.discovery import discover_files

ts_files = discover_files(EF_CLIENT, glob="**/*.{ts,tsx,js,jsx,mjs,cjs}")
contains_node_modules = any("node_modules" in str(f) for f in ts_files)
contains_build = any("/build/" in str(f) for f in ts_files)
contains_dist = any("/dist/" in str(f) for f in ts_files)
contains_chameleon_internal = any("/.chameleon/" in str(f) for f in ts_files)
t("Discovery excludes node_modules", not contains_node_modules)
t("Discovery excludes build/", not contains_build)
t("Discovery excludes dist/", not contains_dist)
t("Discovery excludes .chameleon/", not contains_chameleon_internal)
t(f"Discovery found {len(ts_files)} EF client files", len(ts_files) > 1000)


# ---------------------------------------------------------------------------
# 17. Sanitization on real EF canonical excerpts
# ---------------------------------------------------------------------------
section("Sanitization on real canonicals")

from chameleon_mcp.sanitization import sanitize_for_chameleon_context

# Pull a real canonical's content via get_pattern_context
r = get_pattern_context(
    str(EF_CLIENT / "src" / "components" / "base" / "SelectVettingStatus.tsx")
)
content = r["data"]["canonical_excerpt"]["content"]
# Whatever was injected MUST be sanitized (no inner </chameleon-context> beyond
# the wrapper). Count occurrences in the excerpt content (NOT including tag
# emitted by the hook helper).
t(
    "Real canonical content was sanitized",
    "</chameleon-context>" not in content,
)


# ---------------------------------------------------------------------------
# 18. teach_profile dedup + idiom retention across multiple calls
# ---------------------------------------------------------------------------
section("teach_profile across multiple invocations")

from chameleon_mcp.tools import teach_profile

idiom_a = "comprehensive-test idiom A: prefer ~/utils/* over relative imports"
idiom_b = "comprehensive-test idiom B: never import lodash whole-library"
teach_profile(str(EF_CLIENT), idiom_a)
teach_profile(str(EF_CLIENT), idiom_b)
idioms_text = (EF_CLIENT / ".chameleon" / "idioms.md").read_text()
t("First teach_profile idiom present", idiom_a in idioms_text)
t("Second teach_profile idiom present", idiom_b in idioms_text)


# ---------------------------------------------------------------------------
# 19. list_profiles surfaces both EF stacks
# ---------------------------------------------------------------------------
section("list_profiles (both EF stacks)")

from chameleon_mcp.tools import list_profiles

r = list_profiles()
profile_count = len(r["data"].get("profiles", []))
trusted_count = sum(
    1 for p in r["data"].get("profiles", []) if p.get("trust_state") == "trusted"
)
t(f"list_profiles returns ≥2 known repos (got {profile_count})", profile_count >= 2)
t(f"At least 2 profiles are trusted (got {trusted_count})", trusted_count >= 2)


# ---------------------------------------------------------------------------
# 20. refresh_repo end-to-end (idempotence under fixed input)
# ---------------------------------------------------------------------------
section("refresh_repo")

from chameleon_mcp.tools import refresh_repo

# refresh_repo currently just re-bootstraps in Phase 2D
r1 = refresh_repo(str(EF_CLIENT))["data"]
t("refresh_repo returns success", r1["status"] == "success")
# Re-running still produces same archetype count
r2 = refresh_repo(str(EF_CLIENT))["data"]
t("refresh_repo idempotent on archetype count", r1["archetypes_detected"] == r2["archetypes_detected"])


# ---------------------------------------------------------------------------
# 21. Performance benchmarks
# ---------------------------------------------------------------------------
section("Performance benchmarks (bootstrap timing)")

from chameleon_mcp.tools import bootstrap_repo as bs

# Wipe + measure EF client bootstrap
shutil.rmtree(EF_CLIENT / ".chameleon", ignore_errors=True)
start = time.time()
bs(str(EF_CLIENT))
client_duration = time.time() - start
t(
    f"EF client bootstrap completes in <30s ({client_duration:.1f}s)",
    client_duration < 30.0,
)
# Restore trust (it gets invalidated on profile re-write)
trust_profile(str(EF_CLIENT), "client")


# ---------------------------------------------------------------------------
# 22. detect_repo with material-change re-prompt expected
# ---------------------------------------------------------------------------
section("detect_repo material-change re-prompt")

# After re-bootstrapping above, the trust hash should mismatch — but we
# re-granted trust above, so it should match now. Verify state is consistent.
r = detect_repo(str(EF_CLIENT / "src" / "index.tsx"))
t("detect_repo finds EF client repo_id", r["data"]["repo_id"] is not None)
t("EF client trust_state == trusted", r["data"]["trust_state"] == "trusted")


# ---------------------------------------------------------------------------
# 23. Atomic transaction crash recovery (orphan cleanup)
# ---------------------------------------------------------------------------
section("Atomic transaction orphan cleanup")

from chameleon_mcp.bootstrap.transaction import (
    atomic_profile_commit,
    cleanup_orphan_tmp_dirs,
)

with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / ".chameleon"
    # Manually create an orphan tmp dir (no COMMITTED sentinel)
    tmp_root = target.parent / f".{target.name}.tmp"
    tmp_root.mkdir()
    orphan = tmp_root / "orphan-xyz"
    orphan.mkdir()
    (orphan / "f.json").write_text("{}")
    cleaned = cleanup_orphan_tmp_dirs(target.parent, profile_dir_name=target.name)
    t(f"Orphan cleanup removed {cleaned} txn dirs", cleaned >= 1)


# ---------------------------------------------------------------------------
# 24. lint_file size cap (Round 4 truncation contract)
# ---------------------------------------------------------------------------
section("lint_file size cap")

from chameleon_mcp.tools import lint_file

big_content = "x" * 200_000  # 200 KB > 100 KB cap
r = lint_file("dummy", "dummy", big_content)
t("lint_file marks large content as truncated", r.get("truncated") is True)


# ---------------------------------------------------------------------------
# 25. safe_open security — path traversal, symlinks, null bytes
# ---------------------------------------------------------------------------
section("safe_open security paths")

from chameleon_mcp.safe_open import UnsafeFileError, safe_open

with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "test_repo"
    repo.mkdir()
    (repo / "ok.ts").write_text("export const x = 1;")
    (repo / "subdir").mkdir()
    (repo / "subdir" / "nested.ts").write_text("nested")
    (repo / "evil_link").symlink_to(repo / "ok.ts")
    outside = Path(tmp) / "outside.txt"
    outside.write_text("escape")
    (repo / "escape_link").symlink_to(outside)

    def expect_unsafe(name, rel):
        try:
            safe_open(repo, rel)
            t(name, False, f"expected UnsafeFileError for {rel}")
        except UnsafeFileError:
            t(name, True)

    p = safe_open(repo, "ok.ts")
    t("safe_open accepts normal file", p.name == "ok.ts")
    p2 = safe_open(repo, "subdir/nested.ts")
    t("safe_open accepts nested file", p2.name == "nested.ts")

    expect_unsafe("safe_open rejects ../ traversal", "../outside.txt")
    expect_unsafe("safe_open rejects ../../ traversal", "../../etc/passwd")
    expect_unsafe("safe_open rejects null byte", "ok.ts\x00.png")
    expect_unsafe("safe_open rejects .git segment", ".git/config")
    expect_unsafe("safe_open rejects .ssh segment", ".ssh/id_rsa")
    expect_unsafe("safe_open rejects .aws segment", ".aws/credentials")
    expect_unsafe("safe_open rejects symlink at leaf", "evil_link")
    expect_unsafe("safe_open rejects symlink that escapes repo", "escape_link")
    expect_unsafe("safe_open rejects missing file", "does_not_exist.ts")


# ---------------------------------------------------------------------------
# 26. Sanitization — all 9 dangerous tokens
# ---------------------------------------------------------------------------
section("Sanitization across all dangerous tokens")

from chameleon_mcp.sanitization import sanitize_for_chameleon_context

dangerous_inputs = [
    ("</chameleon-context>", "</chameleon-context>"),
    ("<chameleon-context>", "<chameleon-context>"),
    ("</chameleon", "</chameleon"),
    ("</system>", "</system>"),
    ("<system>", "<system>"),
    ("<|im_start|>", "<|im_start|>"),
    ("<|im_end|>", "<|im_end|>"),
    ("<|endoftext|>", "<|endoftext|>"),
]
for label, payload in dangerous_inputs:
    out = sanitize_for_chameleon_context(f"prefix {payload} suffix")
    t(f"Sanitization neutralizes {label}", payload not in out)

ansi = "before\x1b[31mRED\x1b[0mafter"
out = sanitize_for_chameleon_context(ansi)
t("Sanitization strips ANSI escapes", "\x1b[" not in out)


# ---------------------------------------------------------------------------
# 27. MCP server: tool registration
# ---------------------------------------------------------------------------
section("MCP server tool registration")

from chameleon_mcp import server

mcp = server.mcp
tool_names = set()
try:
    tool_names = {tool.name for tool in mcp._tool_manager._tools.values()}
except Exception:
    try:
        tool_names = set(mcp._tool_manager.tools.keys())
    except Exception:
        pass

expected_tools = {
    "detect_repo", "get_archetype", "get_pattern_context",
    "get_canonical_excerpt", "get_rules", "lint_file",
    "get_drift_status", "refresh_repo", "bootstrap_repo",
    "list_profiles", "merge_profiles", "teach_profile", "trust_profile",
}
missing = expected_tools - tool_names
t(
    f"MCP server registers all 13 tools (missing: {missing})",
    not missing,
)


# ---------------------------------------------------------------------------
# 28. HMAC exec log integrity
# ---------------------------------------------------------------------------
section("HMAC exec log integrity")

from chameleon_mcp.exec_log import (
    _exec_log_dir, append_exec_log, gc_old_logs, verify_exec_log_line,
)

with tempfile.TemporaryDirectory() as tmp:
    os.environ["TMPDIR"] = tmp
    repo_id = "comp-test-repo"
    sess = "comp-test-sess"
    append_exec_log(repo_id, session_id=sess, command="echo one", exit_code=0)
    append_exec_log(repo_id, session_id=sess, command="echo two", exit_code=0)
    log_dir = _exec_log_dir(repo_id)
    log_files = list(log_dir.glob("*.jsonl"))
    t("HMAC log file written", len(log_files) >= 1)

    log_lines = log_files[0].read_text().splitlines()
    t("HMAC log contains 2 entries", len(log_lines) == 2)
    all_verify = all(verify_exec_log_line(line) for line in log_lines)
    t("All HMAC log lines verify", all_verify)
    tampered = log_lines[0].replace("echo one", "echo evil")
    t("Tampered HMAC log line is rejected", not verify_exec_log_line(tampered))

    del os.environ["TMPDIR"]


# ---------------------------------------------------------------------------
# 29. detect_repo on path outside any repo
# ---------------------------------------------------------------------------
section("detect_repo outside any repo")

with tempfile.TemporaryDirectory() as tmp:
    f = Path(tmp) / "ungoverned.ts"
    f.write_text("export const x = 1;")
    r = detect_repo(str(f))
    t(
        "detect_repo on file outside any chameleon repo returns no_repo",
        r["data"]["profile_status"] in ("no_repo", "no_profile"),
    )


# ---------------------------------------------------------------------------
# 30. preflight-and-advise on Ruby file
# ---------------------------------------------------------------------------
section("preflight-and-advise on Ruby file")

env = os.environ.copy()
env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)

ruby_file = EF_API / "app" / "services" / "api" / "v1" / "users" / "create.rb"
if ruby_file.is_file():
    hook_input = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(ruby_file)},
        "session_id": "comp-test-rb",
    })
    proc = subprocess.run(
        [str(HOOKS / "preflight-and-advise")],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "preflight-and-advise injects archetype context for Ruby file",
        "[chameleon: archetype=" in ctx,
    )


# ---------------------------------------------------------------------------
# 31. preflight-and-advise on file outside any repo (fail-open)
# ---------------------------------------------------------------------------
section("preflight-and-advise outside repo (fail-open)")

with tempfile.TemporaryDirectory() as tmp:
    f = Path(tmp) / "outside.ts"
    f.write_text("export const x = 1;")
    hook_input = json.dumps({
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f)},
        "session_id": "outside",
    })
    proc = subprocess.run(
        [str(HOOKS / "preflight-and-advise")],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout)
    t(
        "preflight-and-advise fails open on file outside any repo",
        out == {} or not out.get("hookSpecificOutput", {}).get("additionalContext"),
    )


# ---------------------------------------------------------------------------
# 32. preflight-and-advise on malformed input
# ---------------------------------------------------------------------------
section("preflight-and-advise malformed input")

hook_input = json.dumps({
    "tool_name": "Edit",
    "tool_input": {},
    "session_id": "malformed",
})
proc = subprocess.run(
    [str(HOOKS / "preflight-and-advise")],
    input=hook_input,
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
out = json.loads(proc.stdout)
t("preflight-and-advise emits empty on missing file_path", out == {})

proc = subprocess.run(
    [str(HOOKS / "preflight-and-advise")],
    input="not json at all",
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
)
out = json.loads(proc.stdout)
t("preflight-and-advise emits empty on garbage stdin", out == {})


# ---------------------------------------------------------------------------
# 33. callout-detector clean prompts (no false positives)
# ---------------------------------------------------------------------------
section("callout-detector clean prompts")

clean_prompts = [
    "Add a new component",
    "Refactor the user query",
    "Help me understand the codebase",
]
all_clean = True
for clean in clean_prompts:
    proc = subprocess.run(
        [str(HOOKS / "callout-detector")],
        input=json.dumps({"user_prompt": clean}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout)
    if out != {}:
        all_clean = False
        break
t("callout-detector no false positives on clean prompts", all_clean)


# ---------------------------------------------------------------------------
# 34. callout-detector frustration variants
# ---------------------------------------------------------------------------
section("callout-detector frustration variants")

frustration_prompts = [
    "ugh, why is this happening",
    "stop doing that",
    "this isn't right",
    "chameleon is slow",
    "WTF is this output",
]
matched_count = 0
for fp in frustration_prompts:
    proc = subprocess.run(
        [str(HOOKS / "callout-detector")],
        input=json.dumps({"user_prompt": fp}),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    out = json.loads(proc.stdout)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    if "/chameleon-disable" in ctx:
        matched_count += 1
t(
    f"callout-detector matches all frustration variants ({matched_count}/{len(frustration_prompts)})",
    matched_count == len(frustration_prompts),
)


# ---------------------------------------------------------------------------
# 35. exec_log GC behavior
# ---------------------------------------------------------------------------
section("exec_log GC")

with tempfile.TemporaryDirectory() as tmp:
    os.environ["TMPDIR"] = tmp
    repo_id = "gc-test-repo"
    log_dir = _exec_log_dir(repo_id)
    old_log = log_dir / "ancient.jsonl"
    old_log.write_text('{"ts": 1, "command": "old"}')
    old_time = time.time() - 31 * 86400
    os.utime(old_log, (old_time, old_time))
    fresh = log_dir / "fresh.jsonl"
    fresh.write_text('{"ts": 1, "command": "new"}')

    removed = gc_old_logs(max_age_seconds=30 * 86400)
    t(f"GC removed old log (count={removed})", removed >= 1)
    t("GC retained fresh log", fresh.is_file())
    t("GC removed ancient log", not old_log.is_file())
    del os.environ["TMPDIR"]


# ---------------------------------------------------------------------------
# 36. Cluster signature determinism
# ---------------------------------------------------------------------------
section("Cluster signature determinism")

from chameleon_mcp.signatures import compute_signature

args = dict(
    file_path="src/queries/admin/users/create.ts",
    content_first_200_bytes="export const create = ...",
    top_level_node_kinds=["FunctionDeclaration", "ExportAssignment"],
    default_export_kind="function",
    named_export_count=1,
    import_specifiers=[("react", "react"), ("~/queries/utils", "~/queries/utils")],
    has_jsx=False,
)
sig1 = compute_signature(**args)
sig2 = compute_signature(**args)
t("Cluster signature is deterministic across calls", sig1 == sig2)


# ---------------------------------------------------------------------------
# 37. Sanitization preserves clean code
# ---------------------------------------------------------------------------
section("Sanitization preserves clean code")

clean_code = (EF_CLIENT / "src" / "utils" / "balanceTransaction.ts").read_text()
sanitized = sanitize_for_chameleon_context(clean_code)
diff_chars = sum(1 for a, b in zip(clean_code, sanitized) if a != b)
t(
    f"Real EF code largely untouched ({diff_chars}/{len(clean_code)} char diff)",
    diff_chars < 50,
)


# ---------------------------------------------------------------------------
# 38. Concurrent SQLite reads
# ---------------------------------------------------------------------------
section("Concurrent SQLite reads on drift.db")

import sqlite3

drift_db_client = EF_CLIENT / ".chameleon" / "drift.db"
if drift_db_client.is_file():
    def query_drift(_):
        conn = sqlite3.connect(str(drift_db_client), timeout=5.0)
        rows = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        conn.close()
        return rows[0]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(query_drift, range(50)))
    t(f"50 concurrent SQLite reads succeed (got {len(results)} results)",
      all(r > 0 for r in results))


# ---------------------------------------------------------------------------
# 39. lint_file fail-open on missing profile dir
# ---------------------------------------------------------------------------
section("lint_file edge cases")

with tempfile.TemporaryDirectory() as tmp:
    no_profile_repo = Path(tmp) / "noprof"
    no_profile_repo.mkdir()
    r = lint_file(str(no_profile_repo / "x.ts"), "any-cluster", "const x = 1;")
    t("lint_file on no-profile repo doesn't crash", isinstance(r, dict))


# ---------------------------------------------------------------------------
# 40. EF api full bootstrap timing
# ---------------------------------------------------------------------------
section("EF api full bootstrap timing")

shutil.rmtree(EF_API / ".chameleon", ignore_errors=True)
start = time.time()
r_api = bootstrap_repo(str(EF_API))["data"]
api_duration = time.time() - start
t(
    f"EF api bootstrap completes in <120s ({api_duration:.1f}s)",
    api_duration < 120.0,
)
t(
    f"EF api detected ≥100 archetypes (got {r_api['archetypes_detected']})",
    r_api["archetypes_detected"] >= 100,
)
trust_profile(str(EF_API), "api")


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
