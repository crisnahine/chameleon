"""QA test battery for chameleon against a real Ruby on Rails repo.

Imports chameleon tools directly and calls them, printing PASS/FAIL verdicts.
Does NOT modify the repo.

Set CHAMELEON_TEST_RUBY_REPO to the absolute path of a Rails repo with a
chameleon profile before running. Representative files and their archetype
names are discovered from the repo, so the battery is portable across repos.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO_PATH = os.environ.get("CHAMELEON_TEST_RUBY_REPO", "")

if not REPO_PATH:
    print("SKIP: CHAMELEON_TEST_RUBY_REPO not set")
    sys.exit(0)


def _first_file(*globs: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Return the first existing repo file matching any glob, skipping excludes."""
    root = Path(REPO_PATH)
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            if p.is_file() and not any(x in p.name for x in exclude):
                return str(p)
    return None


CONTROLLER_FILE = _first_file(
    "app/controllers/**/*_controller.rb",
    "app/controllers/*_controller.rb",
    exclude=("application_controller",),
)
MODEL_FILE = _first_file(
    "app/models/*.rb",
    exclude=("application_record", "application_mailer"),
)
SERVICE_FILE = _first_file("app/services/**/*.rb", "app/services/*.rb")
SPEC_FILE = _first_file("spec/**/*_spec.rb", "test/**/*_test.rb")

if not CONTROLLER_FILE or not MODEL_FILE:
    print(
        f"SKIP: {REPO_PATH} does not look like a standard Rails repo "
        "(no app/controllers or app/models file found)"
    )
    sys.exit(0)

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = ""):
    tag = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{tag}] {name}")
    if detail:
        for line in detail.split("\n"):
            print(f"         {line}")


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


from chameleon_mcp.tools import (  # noqa: E402
    detect_repo,
    doctor,
    get_archetype,
    get_canonical_excerpt,
    get_drift_status,
    get_pattern_context,
    get_rules,
    lint_file,
)


def _archetype_for(fpath: str) -> str | None:
    try:
        return get_archetype(REPO_PATH, fpath).get("data", {}).get("archetype")
    except Exception:
        return None


section("1. detect_repo")

try:
    result = detect_repo(CONTROLLER_FILE)
    data = result.get("data", {})
    repo_id = data.get("repo_id")
    profile_status = data.get("profile_status")

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
        "detect_repo: repo_root resolves to the target repo",
        Path(data.get("repo_root") or "").resolve() == Path(REPO_PATH).resolve(),
        f"repo_root={data.get('repo_root')!r}",
    )
    record(
        "detect_repo: trust_state is a known value",
        data.get("trust_state") in ("trusted", "untrusted", "stale", "n/a"),
        f"trust_state={data.get('trust_state')!r}",
    )
except Exception as exc:
    record("detect_repo: call succeeded", False, f"{type(exc).__name__}: {exc}")


section("2. get_archetype (discovered file types)")

ARCHETYPE_FILES = {
    "controller": CONTROLLER_FILE,
    "model": MODEL_FILE,
    "service": SERVICE_FILE,
    "spec": SPEC_FILE,
}

discovered_archetypes: dict[str, str] = {}

for label, fpath in ARCHETYPE_FILES.items():
    if not fpath:
        continue
    try:
        result = get_archetype(REPO_PATH, fpath)
        arch_data = result.get("data", {})
        archetype_name = arch_data.get("archetype")

        ok_name = isinstance(archetype_name, str) and len(archetype_name) > 0
        if ok_name:
            discovered_archetypes[label] = archetype_name
        record(
            f"get_archetype({label}): returns an archetype name",
            ok_name,
            f"archetype={archetype_name!r}",
        )

        band = arch_data.get("confidence_band")
        record(
            f"get_archetype({label}): confidence_band present",
            band in ("high", "medium", "low"),
            f"confidence_band={band!r}",
        )
    except Exception as exc:
        record(f"get_archetype({label}): call succeeded", False, f"{type(exc).__name__}: {exc}")


section("3. get_pattern_context (controller + model)")

for label, fpath in [("controller", CONTROLLER_FILE), ("model", MODEL_FILE)]:
    try:
        result = get_pattern_context(fpath)
        data = result.get("data", {})

        arch = data.get("archetype", {})
        arch_name = arch.get("archetype")
        record(
            f"get_pattern_context({label}): archetype assigned",
            isinstance(arch_name, str) and len(arch_name) > 0,
            f"archetype={arch_name!r}",
        )

        excerpt = data.get("canonical_excerpt", {})
        content = excerpt.get("content", "")
        record(
            f"get_pattern_context({label}): canonical_excerpt non-empty",
            len(content) > 0,
            f"excerpt length={len(content)} chars",
        )

        witness = excerpt.get("witness_path")
        record(
            f"get_pattern_context({label}): witness_path present",
            witness is not None and len(witness) > 0,
            f"witness_path={witness!r}",
        )

        rules = data.get("rules", [])
        record(
            f"get_pattern_context({label}): rules present",
            isinstance(rules, list) and len(rules) > 0,
            f"rules count={len(rules)}",
        )

        repo_info = data.get("repo", {})
        record(
            f"get_pattern_context({label}): profile_status is profile_present",
            repo_info.get("profile_status") == "profile_present",
            f"profile_status={repo_info.get('profile_status')!r}",
        )

        record(
            f"get_pattern_context({label}): idioms field present",
            "idioms" in data,
            f"idioms type={type(data.get('idioms')).__name__}",
        )

        meta = data.get("meta", {})
        record(
            f"get_pattern_context({label}): meta.computed_at present",
            meta.get("computed_at") is not None,
            f"computed_at={meta.get('computed_at')!r}",
        )
    except Exception as exc:
        record(
            f"get_pattern_context({label}): call succeeded", False, f"{type(exc).__name__}: {exc}"
        )


section("4. get_canonical_excerpt (discovered archetypes)")

for label, archetype_name in discovered_archetypes.items():
    try:
        result = get_canonical_excerpt(REPO_PATH, archetype_name)
        data = result.get("data", {})
        content = data.get("content") or ""

        # Canonical-less archetypes (all-spec/test clusters, whose members are
        # canonical-pool-excluded) legitimately have no witness: get_canonical_excerpt
        # returns status="no_witness". That is the correct outcome, not a failure.
        if data.get("status") == "no_witness":
            record(
                f"get_canonical_excerpt({label}={archetype_name}): canonical-less ok",
                True,
                "no witness (canonical-pool-excluded archetype, e.g. spec/test)",
            )
            continue

        record(
            f"get_canonical_excerpt({label}={archetype_name}): content non-empty",
            len(content) > 0,
            f"content length={len(content)} chars",
        )

        looks_ruby = any(kw in content for kw in ["class ", "module ", "def ", "end"])
        record(
            f"get_canonical_excerpt({label}={archetype_name}): content looks like Ruby",
            looks_ruby,
            f"first 100 chars: {content[:100]!r}" if content else "empty",
        )

        witness = data.get("witness_path")
        record(
            f"get_canonical_excerpt({label}={archetype_name}): witness_path present",
            witness is not None and len(witness) > 0,
            f"witness_path={witness!r}",
        )
    except Exception as exc:
        record(
            f"get_canonical_excerpt({label}): call succeeded",
            False,
            f"{type(exc).__name__}: {exc}",
        )


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

    if rules:
        first_key = rules[0][0] if isinstance(rules[0], (list, tuple)) else "?"
        record(
            "get_rules: rules are (key, value) tuples",
            isinstance(rules[0], (list, tuple)) and len(rules[0]) == 2,
            f"first key={first_key!r}",
        )

        sources = [r[0] for r in rules if isinstance(r, (list, tuple))]
        record(
            "get_rules: rule sources found",
            len(sources) > 0,
            f"sources={sources}",
        )
except Exception as exc:
    record("get_rules: call succeeded", False, f"{type(exc).__name__}: {exc}")


section("6. lint_file")

model_content = Path(MODEL_FILE).read_text(encoding="utf-8", errors="replace")
model_archetype = _archetype_for(MODEL_FILE) or discovered_archetypes.get("model", "model")

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

    lang = data.get("language")
    record(
        "lint_file(with file_path): language detected as ruby",
        lang == "ruby",
        f"language={lang!r}",
    )
except Exception as exc:
    record("lint_file(with file_path): call succeeded", False, f"{type(exc).__name__}: {exc}")


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
        data.get("recommended_action") is not None
        and isinstance(data.get("recommended_action"), str),
        f"recommended_action={data.get('recommended_action')!r}",
    )

    dsr = data.get("days_since_refresh")
    record(
        "get_drift_status: days_since_refresh is int or None",
        dsr is None or isinstance(dsr, int),
        f"days_since_refresh={dsr!r}",
    )

    ods = data.get("observed_drift_score")
    record(
        "get_drift_status: observed_drift_score is float or None",
        ods is None or isinstance(ods, (int, float)),
        f"observed_drift_score={ods!r}",
    )
except Exception as exc:
    record("get_drift_status: call succeeded", False, f"{type(exc).__name__}: {exc}")


section("8. Ruby-specific checks")

try:
    result = lint_file(REPO_PATH, model_archetype, model_content, file_path=MODEL_FILE)
    data = result.get("data", {})

    record(
        "Ruby DSL: model lint runs real engine",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}",
    )

    confidence = data.get("canonical_confidence", None)
    record(
        "Ruby DSL: model canonical_confidence is a valid score",
        isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0,
        f"canonical_confidence={confidence!r}",
    )
except Exception as exc:
    record("Ruby DSL: model lint", False, f"{type(exc).__name__}: {exc}")

try:
    ctrl_content = Path(CONTROLLER_FILE).read_text(encoding="utf-8", errors="replace")
    ctrl_archetype = _archetype_for(CONTROLLER_FILE) or discovered_archetypes.get(
        "controller", "controller"
    )
    result = lint_file(REPO_PATH, ctrl_archetype, ctrl_content, file_path=CONTROLLER_FILE)
    data = result.get("data", {})

    record(
        "Ruby DSL: controller lint runs real engine",
        data.get("stub") is False,
        f"stub={data.get('stub')!r}",
    )

    confidence = data.get("canonical_confidence", None)
    record(
        "Ruby DSL: controller canonical_confidence is a valid score",
        isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0,
        f"canonical_confidence={confidence!r}",
    )
except Exception as exc:
    record("Ruby DSL: controller lint", False, f"{type(exc).__name__}: {exc}")


section("9. Caching test (get_pattern_context cold vs warm)")

try:
    from chameleon_mcp.tools import _REPO_ID_CACHE

    _REPO_ID_CACHE.clear()

    t0 = time.perf_counter()
    _ = get_pattern_context(CONTROLLER_FILE)
    cold_ms = (time.perf_counter() - t0) * 1000

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
        f"cold={cold_ms:.1f}ms, warm={warm_ms:.1f}ms, speedup={cold_ms / max(warm_ms, 0.001):.1f}x",
    )
except Exception as exc:
    record("caching: timing test", False, f"{type(exc).__name__}: {exc}")


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

    py_check = next((c for c in checks if c.get("name") == "python_version"), None)
    record(
        "doctor: python_version check is ok",
        py_check is not None and py_check.get("status") == "ok",
        f"detail={py_check.get('detail')!r}" if py_check else "check not found",
    )

    pd_check = next((c for c in checks if c.get("name") == "plugin_data_writable"), None)
    record(
        "doctor: plugin_data_writable is ok",
        pd_check is not None and pd_check.get("status") == "ok",
        f"detail={pd_check.get('detail')!r}" if pd_check else "check not found",
    )

    hmac_check = next((c for c in checks if c.get("name") == "hmac_key"), None)
    record(
        "doctor: hmac_key is ok",
        hmac_check is not None and hmac_check.get("status") == "ok",
        f"detail={hmac_check.get('detail')!r}" if hmac_check else "check not found",
    )

    version = data.get("chameleon_version")
    record(
        "doctor: chameleon_version present",
        version is not None and isinstance(version, str) and len(version) > 0,
        f"version={version!r}",
    )

    summary = data.get("summary", {})
    record(
        "doctor: no error-level checks",
        summary.get("error", 0) == 0,
        f"summary={json.dumps(summary)}",
    )
except Exception as exc:
    record("doctor: call succeeded", False, f"{type(exc).__name__}: {exc}")


print(f"\n{'=' * 60}")
print("  SUMMARY")
print(f"{'=' * 60}")

total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

print(f"\n  Total:  {total}")
print(f"  Passed: {passed}")
print(f"  Failed: {failed}")

if failed > 0:
    print("\n  FAILED TESTS:")
    for name, ok, detail in results:
        if not ok:
            print(f"    - {name}")
            if detail:
                for line in detail.split("\n"):
                    print(f"      {line}")

print()
sys.exit(0 if failed == 0 else 1)
