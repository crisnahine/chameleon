"""QA test battery for chameleon against a real Python repo.

Imports chameleon tools directly and calls them, printing PASS/FAIL verdicts.
Does NOT modify the repo.

Set CHAMELEON_TEST_PYTHON_REPO to the absolute path of a Python repo (Django,
Flask, or FastAPI) with a chameleon profile before running. Representative
files and their archetype names are discovered from the repo, so the battery
is portable across repos and frameworks.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def json_load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


REPO_PATH = os.environ.get("CHAMELEON_TEST_PYTHON_REPO", "")

if not REPO_PATH:
    print("SKIP: CHAMELEON_TEST_PYTHON_REPO not set")
    sys.exit(0)


def _first_file(*globs: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Return the first existing repo file matching any glob, skipping excludes."""
    root = Path(REPO_PATH)
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            if p.is_file() and not any(x in p.name for x in exclude):
                return str(p)
    return None


# Django role files (basename) + the FastAPI/Flask web layer (routes/ dir).
MODEL_FILE = _first_file("**/models.py", "**/models/*.py", exclude=("__init__",))
VIEW_FILE = _first_file("**/views.py", "**/views/*.py", exclude=("__init__",))
SERIALIZER_FILE = _first_file("**/serializers.py", "**/serializers/*.py", exclude=("__init__",))
ROUTE_FILE = _first_file(
    "**/routes/*.py", "**/routers/*.py", "**/endpoints/*.py", exclude=("__init__",)
)
TEST_FILE = _first_file("**/test_*.py", "**/tests.py", "**/tests/*.py", exclude=("__init__",))

# Need at least one recognizable Python source file to run the battery.
_ANY = MODEL_FILE or VIEW_FILE or SERIALIZER_FILE or ROUTE_FILE
if not _ANY:
    print(
        f"SKIP: {REPO_PATH} has no recognizable Python role file "
        "(no models.py / views.py / serializers.py / routes module found)"
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
    get_drift_status,
    get_pattern_context,
    get_rules,
    lint_file,
)

# Pointing the env var at a repo implies trusting it for the run (a fresh
# CHAMELEON_PLUGIN_DATA holds no grant, which would redact canonical/rules
# payloads and fail assertions that say nothing about the code under test).
try:
    from chameleon_mcp.profile.trust import grant_trust as _grant_trust
    from chameleon_mcp.tools import _compute_repo_id as _qa_repo_id

    _grant_trust(_qa_repo_id(Path(REPO_PATH)), Path(REPO_PATH) / ".chameleon")
except Exception as _exc:
    print(f"WARN: could not pre-grant trust for the battery run: {_exc}")


section("1. detect_repo + Python language")

try:
    result = detect_repo(_ANY)
    data = result.get("data", {})
    repo_id = data.get("repo_id")
    record(
        "detect_repo: repo_id is 64-char hex",
        bool(repo_id and re.match(r"^[0-9a-f]{64}$", repo_id)),
        f"repo_id={repo_id[:16] if repo_id else None}...",
    )
    record(
        "detect_repo: profile_status == 'profile_present'",
        data.get("profile_status") == "profile_present",
        f"profile_status={data.get('profile_status')!r}",
    )
    prof = json_load(Path(REPO_PATH) / ".chameleon" / "profile.json")
    record(
        "profile language == 'python'",
        prof.get("language") == "python",
        f"language={prof.get('language')!r}",
    )
except Exception as exc:
    record("detect_repo: call succeeded", False, f"{type(exc).__name__}: {exc}")


section("2. get_archetype (discovered Python file types)")

ARCHETYPE_FILES = {
    "model": MODEL_FILE,
    "view": VIEW_FILE,
    "serializer": SERIALIZER_FILE,
    "route": ROUTE_FILE,
    "test": TEST_FILE,
}

for label, fpath in ARCHETYPE_FILES.items():
    if not fpath:
        continue
    try:
        arch_data = get_archetype(REPO_PATH, fpath).get("data", {})
        name = arch_data.get("archetype")
        record(
            f"get_archetype({label}): returns an archetype name",
            isinstance(name, str) and len(name) > 0,
            f"archetype={name!r}",
        )
        band = arch_data.get("confidence_band")
        record(
            f"get_archetype({label}): confidence_band present",
            band in ("high", "medium", "low"),
            f"confidence_band={band!r}",
        )
    except Exception as exc:
        record(f"get_archetype({label}): call succeeded", False, f"{type(exc).__name__}: {exc}")


section("3. get_pattern_context (role-appropriate canonical)")

for label, fpath in [("model", MODEL_FILE), ("view", VIEW_FILE), ("route", ROUTE_FILE)]:
    if not fpath:
        continue
    try:
        d = get_pattern_context(file_path=fpath).get("data", {})
        arc = d.get("archetype", {})
        can = d.get("canonical_excerpt", {})
        record(
            f"get_pattern_context({label}): archetype + match_quality",
            bool(arc.get("archetype")) and arc.get("match_quality") in ("exact", "ast", "fallback"),
            f"archetype={arc.get('archetype')!r} mq={arc.get('match_quality')!r}",
        )
        wit = can.get("witness_path") or ""
        record(
            f"get_pattern_context({label}): canonical witness is a .py file",
            wit.endswith(".py") or not wit,  # empty witness allowed for sparse archetypes
            f"witness={wit!r} len={len(can.get('content', ''))}",
        )
    except Exception as exc:
        record(f"get_pattern_context({label})", False, f"{type(exc).__name__}: {exc}")


section("4. get_rules + lint_file + drift + doctor")

try:
    rules = get_rules(_ANY).get("data", {})
    record("get_rules: returns a dict", isinstance(rules, dict), f"keys={sorted(rules)[:6]}")
except Exception as exc:
    record("get_rules: call succeeded", False, f"{type(exc).__name__}: {exc}")

try:
    _arch = get_archetype(REPO_PATH, _ANY).get("data", {}).get("archetype") or "model"
    lf = lint_file(REPO_PATH, _arch, Path(_ANY).read_text(encoding="utf-8"), file_path=_ANY).get(
        "data", {}
    )
    record("lint_file: returns a dict with violations list", isinstance(lf.get("violations"), list))
except Exception as exc:
    record("lint_file: call succeeded", False, f"{type(exc).__name__}: {exc}")

# eval/exec sink fires on a synthetic Python snippet through lint_file's scanner.
try:
    from chameleon_mcp.lint_engine import scan_dangerous_sinks

    sinks = scan_dangerous_sinks("x = eval(payload)\n", language="python")
    record(
        "scan_dangerous_sinks: eval-call fires for python",
        any(v.rule == "eval-call" for v in sinks),
        f"rules={[v.rule for v in sinks]}",
    )
except Exception as exc:
    record("scan_dangerous_sinks", False, f"{type(exc).__name__}: {exc}")

try:
    drift = get_drift_status(_ANY).get("data", {})
    record("get_drift_status: returns a dict", isinstance(drift, dict))
except Exception as exc:
    record("get_drift_status: call succeeded", False, f"{type(exc).__name__}: {exc}")

try:
    doc = doctor().get("data", {})
    record("doctor: returns a dict", isinstance(doc, dict))
except Exception as exc:
    record("doctor: call succeeded", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

section("SUMMARY")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n  {passed}/{total} checks passed\n")
for name, ok, _ in results:
    if not ok:
        print(f"  FAILED: {name}")
sys.exit(0 if passed == total else 1)
