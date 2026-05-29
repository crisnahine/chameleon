"""
QA test battery for chameleon MCP tools against a real TypeScript repo.

Set CHAMELEON_TEST_TS_REPO to the absolute path of a TS repo with a
chameleon profile before running.

Invocation:
    CHAMELEON_TEST_TS_REPO=/path/to/ts-repo \
    PYTHONPATH=. mcp/.venv/bin/python tests/qa_typescript.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path


REPO_PATH = os.environ.get("CHAMELEON_TEST_TS_REPO", "")

if not REPO_PATH:
    print("SKIP: CHAMELEON_TEST_TS_REPO not set")
    sys.exit(0)

_tsx_files: list[str] = []
_ts_files: list[str] = []
for root, _dirs, files in os.walk(REPO_PATH):
    if "node_modules" in root or ".next" in root:
        continue
    for f in files:
        full = os.path.join(root, f)
        if f.endswith(".tsx") and len(_tsx_files) < 4:
            _tsx_files.append(full)
        elif (
            f.endswith(".ts")
            and not f.endswith(".d.ts")
            and "/src/" in full
            and len(_ts_files) < 4
        ):
            _ts_files.append(full)
    if len(_tsx_files) >= 4 and len(_ts_files) >= 4:
        break

TEST_TSX_FILES = _tsx_files[:4]
TEST_TS_FILES = _ts_files[:2]
TEST_FILES = TEST_TSX_FILES + TEST_TS_FILES


from chameleon_mcp.tools import (  # noqa: E402
    detect_repo,
    doctor,
    get_archetype,
    get_canonical_excerpt,
    get_drift_status,
    get_pattern_context,
    get_rules,
    lint_file,
    list_profiles,
)


results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = ""):
    tag = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{tag}] {name}")
    if detail:
        d = detail if len(detail) < 300 else detail[:300] + "..."
        print(f"         {d}")


print("\n=== Test 1: detect_repo ===")
try:
    dr = detect_repo(os.path.join(REPO_PATH, "src/index.tsx"))
    data = dr.get("data", {})
    repo_id = data.get("repo_id")
    profile_status = data.get("profile_status")
    trust_state = data.get("trust_state")

    record(
        "detect_repo.api_version",
        dr.get("api_version") == "1",
        f"got {dr.get('api_version')!r}",
    )
    record(
        "detect_repo.repo_id_format",
        isinstance(repo_id, str) and len(repo_id) == 64 and re.match(r"^[0-9a-f]{64}$", repo_id) is not None,
        f"repo_id={repo_id!r}",
    )
    record(
        "detect_repo.profile_status",
        profile_status == "profile_present",
        f"profile_status={profile_status!r}",
    )
    record(
        "detect_repo.trust_state",
        trust_state in ("trusted", "stale", "untrusted"),
        f"trust_state={trust_state!r}",
    )

    REPO_ID = repo_id
except Exception as exc:
    record("detect_repo", False, f"EXCEPTION: {exc}")
    REPO_ID = None


print("\n=== Test 2: get_archetype ===")
archetype_names: list[str] = []
for fpath in TEST_FILES:
    rel = os.path.relpath(fpath, REPO_PATH)
    try:
        ar = get_archetype(REPO_PATH, fpath)
        ad = ar.get("data", {})
        arch_name = ad.get("archetype")
        conf_band = ad.get("confidence_band")
        match_q = ad.get("match_quality")

        ok = arch_name is not None and isinstance(arch_name, str) and len(arch_name) > 0
        record(
            f"get_archetype({rel}).archetype",
            ok,
            f"archetype={arch_name!r}",
        )
        record(
            f"get_archetype({rel}).confidence_band",
            conf_band in ("high", "medium", "low"),
            f"confidence_band={conf_band!r}",
        )
        record(
            f"get_archetype({rel}).match_quality",
            match_q in ("ast", "exact", "fallback", "none"),
            f"match_quality={match_q!r}",
        )
        if arch_name:
            archetype_names.append(arch_name)
    except Exception as exc:
        record(f"get_archetype({rel})", False, f"EXCEPTION: {exc}")


print("\n=== Test 3: get_pattern_context ===")
for fpath in TEST_FILES[:3]:
    rel = os.path.relpath(fpath, REPO_PATH)
    try:
        pc = get_pattern_context(fpath)
        pd = pc.get("data", {})

        rid = (pd.get("repo") or {}).get("id")
        record(
            f"get_pattern_context({rel}).repo.id",
            isinstance(rid, str) and len(rid) == 64,
            f"repo.id={rid!r}" if rid else "repo.id=None",
        )

        arch_block = pd.get("archetype") or {}
        aname = arch_block.get("archetype")
        record(
            f"get_pattern_context({rel}).archetype.name",
            isinstance(aname, str) and len(aname) > 0,
            f"archetype={aname!r}",
        )

        ce = pd.get("canonical_excerpt") or {}
        content = ce.get("content")
        record(
            f"get_pattern_context({rel}).canonical_excerpt.content",
            isinstance(content, str) and len(content) > 0,
            f"len={len(content) if content else 0}",
        )

        rules = pd.get("rules")
        record(
            f"get_pattern_context({rel}).rules",
            isinstance(rules, list),
            f"count={len(rules) if rules else 0}",
        )

        meta = pd.get("meta") or {}
        mt = meta.get("mtime_token")
        record(
            f"get_pattern_context({rel}).meta.mtime_token",
            mt is not None,
            f"mtime_token={mt!r}" if mt else "mtime_token=None",
        )

    except Exception as exc:
        record(f"get_pattern_context({rel})", False, f"EXCEPTION: {exc}")


print("\n=== Test 4: get_canonical_excerpt ===")
if archetype_names:
    arch_to_test = archetype_names[0]
    try:
        ce = get_canonical_excerpt(REPO_PATH, arch_to_test)
        cd = ce.get("data", {})
        content = cd.get("content")
        witness = cd.get("witness_path")

        record(
            "get_canonical_excerpt.content",
            isinstance(content, str) and len(content) > 0,
            f"len={len(content) if content else 0}, archetype={arch_to_test!r}",
        )
        record(
            "get_canonical_excerpt.witness_path",
            isinstance(witness, str) and len(witness) > 0,
            f"witness_path={witness!r}",
        )
    except Exception as exc:
        record("get_canonical_excerpt", False, f"EXCEPTION: {exc}")
else:
    record("get_canonical_excerpt", False, "SKIP: no archetype names found in test 2")


print("\n=== Test 5: get_rules ===")
try:
    rr = get_rules(REPO_PATH)
    rd = rr.get("data", {})
    rules = rd.get("rules")

    # rules is repo-global (from root tool configs). A monorepo coordinator
    # whose tsconfig/eslintrc live in packages/* legitimately has no root
    # rules, so assert a well-formed list, not non-empty.
    record(
        "get_rules.well_formed",
        isinstance(rules, list),
        f"count={len(rules) if isinstance(rules, list) else 'n/a'}"
        + ("" if (isinstance(rules, list) and rules) else " (no root-level tool configs)"),
    )
    if rules:
        first = rules[0]
        record(
            "get_rules.shape",
            isinstance(first, (list, tuple)) and len(first) == 2,
            f"first_rule_key={first[0]!r}" if isinstance(first, (list, tuple)) and first else str(first)[:80],
        )
except Exception as exc:
    record("get_rules", False, f"EXCEPTION: {exc}")


print("\n=== Test 6: lint_file ===")
if archetype_names and TEST_FILES:
    test_file = TEST_FILES[0]
    test_arch = archetype_names[0]
    try:
        file_content = Path(test_file).read_text(encoding="utf-8", errors="replace")[:50_000]
        rel = os.path.relpath(test_file, REPO_PATH)

        lr = lint_file(REPO_PATH, test_arch, file_content)
        ld = lr.get("data", {})

        record(
            "lint_file.no_crash",
            True,
            f"stub={ld.get('stub')}, violations={len(ld.get('violations', []))}",
        )
        record(
            "lint_file.has_confidence",
            "canonical_confidence" in ld,
            f"canonical_confidence={ld.get('canonical_confidence')}",
        )
        record(
            "lint_file.content_size",
            ld.get("content_size", 0) > 0,
            f"content_size={ld.get('content_size')}",
        )

        lr2 = lint_file(REPO_PATH, test_arch, file_content, file_path=test_file)
        ld2 = lr2.get("data", {})
        record(
            "lint_file.with_file_path",
            True,
            f"stub={ld2.get('stub')}, violations={len(ld2.get('violations', []))}",
        )

    except Exception as exc:
        record("lint_file", False, f"EXCEPTION: {exc}")
else:
    record("lint_file", False, "SKIP: no archetypes or test files")


print("\n=== Test 7: get_drift_status ===")
try:
    ds = get_drift_status(REPO_PATH)
    dd = ds.get("data", {})

    record(
        "get_drift_status.repo_id",
        isinstance(dd.get("repo_id"), str) and len(dd["repo_id"]) == 64,
        f"repo_id={dd.get('repo_id', '')[:16]}...",
    )
    record(
        "get_drift_status.has_drift_fields",
        "days_since_refresh" in dd and "observed_drift_score" in dd and "recommended_action" in dd,
        f"days={dd.get('days_since_refresh')}, drift={dd.get('observed_drift_score')}, action={dd.get('recommended_action')!r}",
    )
except Exception as exc:
    record("get_drift_status", False, f"EXCEPTION: {exc}")


print("\n=== Test 8: list_profiles ===")
try:
    lp = list_profiles()
    lpd = lp.get("data", {})
    profiles = lpd.get("profiles", [])
    total = lpd.get("total_known", 0)

    record(
        "list_profiles.has_profiles",
        isinstance(profiles, list) and len(profiles) > 0,
        f"total_known={total}, page_size={len(profiles)}",
    )

    repo_name = os.path.basename(os.path.normpath(REPO_PATH))
    self_found = any(
        repo_name in (p.get("repo_root") or "")
        for p in profiles
    )
    record(
        "list_profiles.repo_under_test_present",
        self_found,
        f"searched {len(profiles)} profiles for {repo_name!r} in repo_root",
    )
except Exception as exc:
    record("list_profiles", False, f"EXCEPTION: {exc}")


print("\n=== Test 9: caching ===")
if TEST_FILES:
    cache_file = TEST_FILES[0]
    rel = os.path.relpath(cache_file, REPO_PATH)
    try:
        t0 = time.perf_counter()
        _ = get_pattern_context(cache_file)
        t1 = time.perf_counter()
        cold_ms = (t1 - t0) * 1000

        t2 = time.perf_counter()
        _ = get_pattern_context(cache_file)
        t3 = time.perf_counter()
        warm_ms = (t3 - t2) * 1000

        record(
            "caching.cold_call_timing",
            cold_ms > 0,
            f"cold={cold_ms:.2f}ms",
        )
        record(
            "caching.warm_call_timing",
            warm_ms > 0,
            f"warm={warm_ms:.2f}ms",
        )
        record(
            "caching.warm_faster",
            warm_ms < cold_ms,
            f"cold={cold_ms:.2f}ms, warm={warm_ms:.2f}ms, speedup={cold_ms / warm_ms:.1f}x" if warm_ms > 0 else "warm=0ms",
        )
    except Exception as exc:
        record("caching", False, f"EXCEPTION: {exc}")
else:
    record("caching", False, "SKIP: no test files")


print("\n=== Test 10: doctor ===")
try:
    doc = doctor()
    dd = doc.get("data", {})

    overall = dd.get("overall")
    record(
        "doctor.overall",
        overall in ("ok", "warn", "error"),
        f"overall={overall!r}",
    )

    checks = dd.get("checks", [])
    record(
        "doctor.has_checks",
        isinstance(checks, list) and len(checks) > 0,
        f"check_count={len(checks)}",
    )

    summary = dd.get("summary", {})
    record(
        "doctor.summary",
        isinstance(summary, dict) and "total" in summary,
        f"ok={summary.get('ok')}, warn={summary.get('warn')}, error={summary.get('error')}",
    )

    version = dd.get("chameleon_version")
    record(
        "doctor.version",
        isinstance(version, str) and len(version) > 0,
        f"version={version!r}",
    )
except Exception as exc:
    record("doctor", False, f"EXCEPTION: {exc}")


print("\n" + "=" * 60)
passed = sum(1 for _, p, _ in results if p)
failed = sum(1 for _, p, _ in results if not p)
total = len(results)
print(f"TOTAL: {total}  |  PASSED: {passed}  |  FAILED: {failed}")

if failed:
    print("\nFailed tests:")
    for name, p, detail in results:
        if not p:
            print(f"  - {name}: {detail}")

print("=" * 60)
sys.exit(1 if failed else 0)
