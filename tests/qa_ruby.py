"""QA test battery for chameleon against a real Ruby on Rails repo (ef-api).

Imports chameleon tools directly and calls them, printing PASS/FAIL verdicts.
Does NOT modify the repo.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_PATH = "/Users/crisn/Documents/Projects/Testing Apps/ef-api"

# Real files to test against (found via find)
CONTROLLER_FILE = f"{REPO_PATH}/app/controllers/api/v1/referrals_controller.rb"
MODEL_FILE = f"{REPO_PATH}/app/models/listing_asset.rb"
SERVICE_FILE = f"{REPO_PATH}/app/services/slack/channel_message.rb"
SPEC_FILE = f"{REPO_PATH}/spec/models/ticket_spec.rb"

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = ""):
    tag = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{tag}] {name}")
    if detail:
        # Indent detail lines for readability
        for line in detail.split("\n"):
            print(f"         {line}")


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Import tools
# ---------------------------------------------------------------------------

from chameleon_mcp.tools import (
    detect_repo,
    doctor,
    get_archetype,
    get_canonical_excerpt,
    get_drift_status,
    get_pattern_context,
    get_rules,
    lint_file,
)


# ---------------------------------------------------------------------------
# 1. detect_repo
# ---------------------------------------------------------------------------

section("1. detect_repo")

try:
    result = detect_repo(CONTROLLER_FILE)
    data = result.get("data", {})
    repo_id = data.get("repo_id")
    profile_status = data.get("profile_status")

    # repo_id should be 64-char hex
    is_hex_64 = bool(repo_id and re.match(r"^[0-9a-f]{64}$", repo_id))
    record(
        "detect_repo: repo_id is 64-char hex",
        is_hex_64,
        f"repo_id={repo_id[:16]}..." if repo_id else "repo_id=None",
    )
    record(
        "detect_repo: profile_status == 'profile_present'",
        profile_status == "profile_present",
        f"profile_status={profile_status!r}",
    )
    record(
        "detect_repo: repo_root points to ef-api",
        "ef-api" in (data.get("repo_root") or ""),
        f"repo_root={data.get('repo_root')!r}",
    )
    record(
        "detect_repo: trust_state is a known value",
        data.get("trust_state") in ("trusted", "untrusted", "stale", "n/a"),
        f"trust_state={data.get('trust_state')!r}",
    )
except Exception as exc:
    record("detect_repo: call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 2. get_archetype for 4 file types
# ---------------------------------------------------------------------------

section("2. get_archetype (4 file types)")

ARCHETYPE_FILES = {
    "controller": (CONTROLLER_FILE, ["controller"]),
    "model": (MODEL_FILE, ["model"]),
    "service": (SERVICE_FILE, ["service"]),
    "spec": (SPEC_FILE, ["test", "test-2", "test-api", "test-asset-purchase-agreements", "test-factories"]),
}

for label, (fpath, expected_archetypes) in ARCHETYPE_FILES.items():
    try:
        result = get_archetype(REPO_PATH, fpath)
        arch_data = result.get("data", {})
        archetype_name = arch_data.get("archetype")

        record(
            f"get_archetype({label}): returns an archetype name",
            archetype_name is not None and isinstance(archetype_name, str) and len(archetype_name) > 0,
            f"archetype={archetype_name!r}",
        )

        # Check archetype matches expected category
        matches_expected = any(exp in (archetype_name or "") for exp in expected_archetypes)
        record(
            f"get_archetype({label}): archetype matches expected category",
            matches_expected,
            f"expected one of {expected_archetypes}, got {archetype_name!r}",
        )

        # confidence_band should be present
        band = arch_data.get("confidence_band")
        record(
            f"get_archetype({label}): confidence_band present",
            band in ("high", "medium", "low"),
            f"confidence_band={band!r}",
        )
    except Exception as exc:
        record(f"get_archetype({label}): call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 3. get_pattern_context for controller and model
# ---------------------------------------------------------------------------

section("3. get_pattern_context (controller + model)")

for label, fpath in [("controller", CONTROLLER_FILE), ("model", MODEL_FILE)]:
    try:
        result = get_pattern_context(fpath)
        data = result.get("data", {})

        # archetype assigned
        arch = data.get("archetype", {})
        arch_name = arch.get("archetype")
        record(
            f"get_pattern_context({label}): archetype assigned",
            arch_name is not None and len(arch_name) > 0,
            f"archetype={arch_name!r}",
        )

        # canonical_excerpt non-empty
        excerpt = data.get("canonical_excerpt", {})
        content = excerpt.get("content", "")
        record(
            f"get_pattern_context({label}): canonical_excerpt non-empty",
            len(content) > 0,
            f"excerpt length={len(content)} chars",
        )

        # witness_path present
        witness = excerpt.get("witness_path")
        record(
            f"get_pattern_context({label}): witness_path present",
            witness is not None and len(witness) > 0,
            f"witness_path={witness!r}",
        )

        # rules present
        rules = data.get("rules", [])
        record(
            f"get_pattern_context({label}): rules present",
            isinstance(rules, list) and len(rules) > 0,
            f"rules count={len(rules)}",
        )

        # repo info
        repo_info = data.get("repo", {})
        record(
            f"get_pattern_context({label}): profile_status is profile_present",
            repo_info.get("profile_status") == "profile_present",
            f"profile_status={repo_info.get('profile_status')!r}",
        )

        # idioms field present
        record(
            f"get_pattern_context({label}): idioms field present",
            "idioms" in data,
            f"idioms type={type(data.get('idioms')).__name__}",
        )

        # meta present
        meta = data.get("meta", {})
        record(
            f"get_pattern_context({label}): meta.computed_at present",
            meta.get("computed_at") is not None,
            f"computed_at={meta.get('computed_at')!r}",
        )
    except Exception as exc:
        record(f"get_pattern_context({label}): call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 4. get_canonical_excerpt for a Rails archetype
# ---------------------------------------------------------------------------

section("4. get_canonical_excerpt (Rails archetype)")

for archetype_name in ["controller", "model", "service"]:
    try:
        result = get_canonical_excerpt(REPO_PATH, archetype_name)
        data = result.get("data", {})
        content = data.get("content") or ""

        record(
            f"get_canonical_excerpt({archetype_name}): content non-empty",
            len(content) > 0,
            f"content length={len(content)} chars",
        )

        # Content should look like Ruby code
        looks_ruby = any(kw in content for kw in ["class ", "module ", "def ", "end"])
        record(
            f"get_canonical_excerpt({archetype_name}): content looks like Ruby",
            looks_ruby,
            f"first 100 chars: {content[:100]!r}" if content else "empty",
        )

        # witness_path present
        witness = data.get("witness_path")
        record(
            f"get_canonical_excerpt({archetype_name}): witness_path present",
            witness is not None and len(witness) > 0,
            f"witness_path={witness!r}",
        )
    except Exception as exc:
        record(f"get_canonical_excerpt({archetype_name}): call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 5. get_rules
# ---------------------------------------------------------------------------

section("5. get_rules")

try:
    result = get_rules(REPO_PATH)
    data = result.get("data", {})
    rules = data.get("rules", [])

    record(
        "get_rules: returns rules list",
        isinstance(rules, list) and len(rules) > 0,
        f"rules count={len(rules)}",
    )

    # Each rule entry is a (source_key, value) tuple
    if rules:
        first_key = rules[0][0] if isinstance(rules[0], (list, tuple)) else "?"
        record(
            "get_rules: rules are (key, value) tuples",
            isinstance(rules[0], (list, tuple)) and len(rules[0]) == 2,
            f"first key={first_key!r}",
        )

        # Print available rule sources
        sources = [r[0] for r in rules if isinstance(r, (list, tuple))]
        record(
            "get_rules: rule sources found",
            len(sources) > 0,
            f"sources={sources}",
        )
except Exception as exc:
    record("get_rules: call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 6. lint_file
# ---------------------------------------------------------------------------

section("6. lint_file")

# Read a real .rb file
model_content = Path(MODEL_FILE).read_text(encoding="utf-8", errors="replace")

# Get its archetype first
arch_result = get_archetype(REPO_PATH, MODEL_FILE)
model_archetype = arch_result.get("data", {}).get("archetype", "model")

# 6a. lint_file WITHOUT file_path param
try:
    result = lint_file(REPO_PATH, model_archetype, model_content)
    data = result.get("data", {})

    record(
        "lint_file(no file_path): stub is False (real engine ran)",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}, stub_reason={data.get('stub_reason')!r}",
    )

    confidence = data.get("canonical_confidence")
    record(
        "lint_file(no file_path): canonical_confidence is float 0..1",
        isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0,
        f"canonical_confidence={confidence!r}",
    )

    record(
        "lint_file(no file_path): violations is a list",
        isinstance(data.get("violations"), list),
        f"violations count={len(data.get('violations', []))}",
    )
except Exception as exc:
    record("lint_file(no file_path): call succeeded", False, f"{type(exc).__name__}: {exc}")

# 6b. lint_file WITH file_path param
try:
    result = lint_file(REPO_PATH, model_archetype, model_content, file_path=MODEL_FILE)
    data = result.get("data", {})

    record(
        "lint_file(with file_path): stub is False",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}",
    )

    confidence = data.get("canonical_confidence")
    record(
        "lint_file(with file_path): canonical_confidence is float 0..1",
        isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0,
        f"canonical_confidence={confidence!r}",
    )

    # language should be detected as ruby
    lang = data.get("language")
    record(
        "lint_file(with file_path): language detected as ruby",
        lang == "ruby",
        f"language={lang!r}",
    )
except Exception as exc:
    record("lint_file(with file_path): call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 7. get_drift_status
# ---------------------------------------------------------------------------

section("7. get_drift_status")

try:
    result = get_drift_status(REPO_PATH)
    data = result.get("data", {})

    record(
        "get_drift_status: repo_id present",
        data.get("repo_id") is not None,
        f"repo_id={str(data.get('repo_id', ''))[:16]}...",
    )

    record(
        "get_drift_status: recommended_action present",
        data.get("recommended_action") is not None and isinstance(data.get("recommended_action"), str),
        f"recommended_action={data.get('recommended_action')!r}",
    )

    # days_since_refresh can be None (no trust grant) or int
    dsr = data.get("days_since_refresh")
    record(
        "get_drift_status: days_since_refresh is int or None",
        dsr is None or isinstance(dsr, int),
        f"days_since_refresh={dsr!r}",
    )

    # observed_drift_score can be None or float
    ods = data.get("observed_drift_score")
    record(
        "get_drift_status: observed_drift_score is float or None",
        ods is None or isinstance(ods, (int, float)),
        f"observed_drift_score={ods!r}",
    )
except Exception as exc:
    record("get_drift_status: call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 8. Ruby-specific checks
# ---------------------------------------------------------------------------

section("8. Ruby-specific checks")

# 8a. DSL detection: lint a model with has_many, validates, before_action
try:
    # The model file has belongs_to, validates, before_validation
    result = lint_file(REPO_PATH, "model", model_content, file_path=MODEL_FILE)
    data = result.get("data", {})

    # The real engine should have run (not stub)
    record(
        "Ruby DSL: model lint runs real engine on DSL-heavy file",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}",
    )

    # canonical_confidence should be meaningful (> 0) for a model file
    # linted against the model archetype
    confidence = data.get("canonical_confidence", 0.0)
    record(
        "Ruby DSL: model canonical_confidence > 0 (DSL recognized)",
        isinstance(confidence, (int, float)) and confidence > 0,
        f"canonical_confidence={confidence!r}",
    )
except Exception as exc:
    record("Ruby DSL: model lint", False, f"{type(exc).__name__}: {exc}")

# 8b. DSL detection: lint a controller with before_action
try:
    ctrl_content = Path(CONTROLLER_FILE).read_text(encoding="utf-8", errors="replace")
    result = lint_file(REPO_PATH, "controller", ctrl_content, file_path=CONTROLLER_FILE)
    data = result.get("data", {})

    record(
        "Ruby DSL: controller lint runs real engine",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}",
    )

    confidence = data.get("canonical_confidence", 0.0)
    record(
        "Ruby DSL: controller canonical_confidence > 0",
        isinstance(confidence, (int, float)) and confidence > 0,
        f"canonical_confidence={confidence!r}",
    )
except Exception as exc:
    record("Ruby DSL: controller lint", False, f"{type(exc).__name__}: {exc}")

# 8c. Superclass detection: controller archetype's canonical should show
# ApplicationController lineage
try:
    ctrl_arch_result = get_archetype(REPO_PATH, CONTROLLER_FILE)
    ctrl_archetype = ctrl_arch_result.get("data", {}).get("archetype")

    excerpt_result = get_canonical_excerpt(REPO_PATH, ctrl_archetype or "controller")
    excerpt_content = excerpt_result.get("data", {}).get("content", "")

    # The canonical for the controller archetype should reference a base controller
    has_controller_lineage = (
        "Controller" in excerpt_content
        or "< Api" in excerpt_content
        or "< Application" in excerpt_content
        or "< ActionController" in excerpt_content
    )
    record(
        "Ruby superclass: controller canonical shows Controller lineage",
        has_controller_lineage,
        f"contains 'Controller' or '< Api' or '< Application': {has_controller_lineage}",
    )
except Exception as exc:
    record("Ruby superclass: controller canonical", False, f"{type(exc).__name__}: {exc}")

# 8d. Superclass detection: model archetype should show ApplicationRecord
try:
    excerpt_result = get_canonical_excerpt(REPO_PATH, "model")
    excerpt_content = excerpt_result.get("data", {}).get("content", "")

    has_model_lineage = (
        "ApplicationRecord" in excerpt_content
        or "ActiveRecord" in excerpt_content
    )
    record(
        "Ruby superclass: model canonical shows ApplicationRecord lineage",
        has_model_lineage,
        f"contains 'ApplicationRecord' or 'ActiveRecord': {has_model_lineage}",
    )
except Exception as exc:
    record("Ruby superclass: model canonical", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 9. Caching test: get_pattern_context cold vs warm
# ---------------------------------------------------------------------------

section("9. Caching test (get_pattern_context cold vs warm)")

try:
    # Clear the repo_id cache to force cold path
    from chameleon_mcp.tools import _REPO_ID_CACHE
    _REPO_ID_CACHE.clear()

    # Cold call
    t0 = time.perf_counter()
    _ = get_pattern_context(CONTROLLER_FILE)
    cold_ms = (time.perf_counter() - t0) * 1000

    # Warm call (repo_id cached, profile likely cached)
    t0 = time.perf_counter()
    _ = get_pattern_context(CONTROLLER_FILE)
    warm_ms = (time.perf_counter() - t0) * 1000

    record(
        "caching: cold call completes in < 5000ms",
        cold_ms < 5000,
        f"cold={cold_ms:.1f}ms",
    )
    record(
        "caching: warm call completes in < 500ms",
        warm_ms < 500,
        f"warm={warm_ms:.1f}ms",
    )
    record(
        "caching: warm is faster than cold",
        warm_ms < cold_ms,
        f"cold={cold_ms:.1f}ms, warm={warm_ms:.1f}ms, speedup={cold_ms/max(warm_ms, 0.001):.1f}x",
    )
except Exception as exc:
    record("caching: timing test", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 10. doctor
# ---------------------------------------------------------------------------

section("10. doctor")

try:
    result = doctor()
    data = result.get("data", {})

    overall = data.get("overall")
    record(
        "doctor: overall status is ok or warn",
        overall in ("ok", "warn"),
        f"overall={overall!r}",
    )

    checks = data.get("checks", [])
    record(
        "doctor: checks list non-empty",
        isinstance(checks, list) and len(checks) > 0,
        f"checks count={len(checks)}",
    )

    # Python version check should pass
    py_check = next((c for c in checks if c.get("name") == "python_version"), None)
    record(
        "doctor: python_version check is ok",
        py_check is not None and py_check.get("status") == "ok",
        f"detail={py_check.get('detail')!r}" if py_check else "check not found",
    )

    # plugin_data_writable should pass
    pd_check = next((c for c in checks if c.get("name") == "plugin_data_writable"), None)
    record(
        "doctor: plugin_data_writable is ok",
        pd_check is not None and pd_check.get("status") == "ok",
        f"detail={pd_check.get('detail')!r}" if pd_check else "check not found",
    )

    # hmac_key should pass
    hmac_check = next((c for c in checks if c.get("name") == "hmac_key"), None)
    record(
        "doctor: hmac_key is ok",
        hmac_check is not None and hmac_check.get("status") == "ok",
        f"detail={hmac_check.get('detail')!r}" if hmac_check else "check not found",
    )

    # chameleon_version present
    version = data.get("chameleon_version")
    record(
        "doctor: chameleon_version present",
        version is not None and isinstance(version, str) and len(version) > 0,
        f"version={version!r}",
    )

    # summary
    summary = data.get("summary", {})
    record(
        "doctor: no error-level checks",
        summary.get("error", 0) == 0,
        f"summary={json.dumps(summary)}",
    )
except Exception as exc:
    record("doctor: call succeeded", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print("  SUMMARY")
print(f"{'='*60}")

total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

print(f"\n  Total:  {total}")
print(f"  Passed: {passed}")
print(f"  Failed: {failed}")

if failed > 0:
    print(f"\n  FAILED TESTS:")
    for name, ok, detail in results:
        if not ok:
            print(f"    - {name}")
            if detail:
                for line in detail.split("\n"):
                    print(f"      {line}")

print()
sys.exit(0 if failed == 0 else 1)
