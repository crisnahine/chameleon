"""Hook eval scenario runner.

Default mode: calls chameleon_mcp.tools.get_pattern_context in-process.
--full mode: pipes a synthetic PreToolUse event through hooks/preflight-and-advise.

When fixtures fall out of sync with the chameleon profile schema, the
runner reports SCHEMA_ROT and points at scripts/refresh_eval_fixtures.sh,
which regenerates the .chameleon/ directories with pinned now=1700000000.0.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "eval_repos"
HOOK_SCRIPT = REPO_ROOT / "hooks" / "preflight-and-advise"
SCENARIOS_DIR = REPO_ROOT / "tests" / "hook_evals" / "scenarios"


@dataclass
class ScenarioResult:
    name: str
    status: str  # PASS | FAIL | SCHEMA_ROT | HOOK_FAILED | ERROR
    mismatches: list[str] = field(default_factory=list)


def assert_scenario(scenario: dict, response: dict) -> ScenarioResult:
    """Assert a get_pattern_context response matches a scenario's `expected`.

    Returns a ScenarioResult. Does not raise.
    """
    name = scenario["name"]
    data = response.get("data", {}) or {}
    repo = data.get("repo", {}) or {}
    expected = scenario.get("expected", {}) or {}
    mismatches: list[str] = []

    profile_status = repo.get("profile_status")
    if profile_status == "profile_corrupted":
        return ScenarioResult(
            name=name,
            status="SCHEMA_ROT",
            mismatches=[
                "Fixture profile is unloadable. Run scripts/refresh_eval_fixtures.sh to regenerate."
            ],
        )

    archetype_node = data.get("archetype") or {}
    actual_archetype = archetype_node.get("archetype") if isinstance(archetype_node, dict) else None

    expected_archetype = expected.get("archetype_name", "<unset>")
    if expected_archetype != "<unset>":
        if expected_archetype != actual_archetype:
            mismatches.append(
                f"archetype: expected {expected_archetype!r}, got {actual_archetype!r}"
            )

    expected_status = expected.get("profile_status")
    if expected_status is not None and expected_status != profile_status:
        mismatches.append(
            f"profile_status: expected {expected_status!r}, got {profile_status!r}"
        )

    expected_trust = expected.get("trust_state")
    if expected_trust is not None and expected_trust != repo.get("trust_state"):
        mismatches.append(
            f"trust_state: expected {expected_trust!r}, got {repo.get('trust_state')!r}"
        )

    canonical = data.get("canonical_excerpt") or {}
    canonical_text = canonical.get("content", "") if isinstance(canonical, dict) else ""
    for needle in expected.get("canonical_excerpt_includes", []) or []:
        if needle not in canonical_text:
            mismatches.append(
                f"canonical_excerpt missing substring {needle!r}"
            )

    rules_pairs = data.get("rules") or []
    rules_text = "\n".join(f"{k}: {v}" for k, v in rules_pairs)
    for needle in expected.get("rules_must_include_substring", []) or []:
        if needle not in rules_text:
            mismatches.append(f"rules missing substring {needle!r}")
    for forbidden in expected.get("rules_must_not_include_substring", []) or []:
        if forbidden in rules_text:
            mismatches.append(f"rules unexpectedly contains substring {forbidden!r}")

    idioms_text = data.get("idioms", "") or ""
    for needle in expected.get("idioms_must_include_substring", []) or []:
        if needle not in idioms_text:
            mismatches.append(f"idioms missing substring {needle!r}")

    return ScenarioResult(
        name=name,
        status="PASS" if not mismatches else "FAIL",
        mismatches=mismatches,
    )


def discover_scenarios(root: Path) -> list[dict]:
    """Glob scenarios/**/*.json, sorted lexicographically."""
    paths = sorted(glob.glob(str(root / "**" / "*.json"), recursive=True))
    scenarios = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            obj = json.load(f)
        obj["_source_path"] = p
        scenarios.append(obj)
    return scenarios


def _synthesize_no_profile_marker(repo_tmp: Path, file_path: str) -> None:
    """For fixture_repo: null, drop a language marker so find_repo_root resolves."""
    ext = Path(file_path).suffix.lower()
    if ext in (".ts", ".tsx", ".js", ".jsx"):
        (repo_tmp / "package.json").write_text("{}")
    elif ext == ".rb":
        (repo_tmp / "Gemfile").write_text("source 'https://rubygems.org'\n")


def _apply_trust_state(repo_tmp: Path, trust_state: str) -> None:
    """Per-scenario trust setup. Assumes CHAMELEON_PLUGIN_DATA is already set."""
    if trust_state in ("untrusted", "n/a"):
        return
    if not (repo_tmp / ".chameleon").is_dir():
        raise ValueError(
            f"trust_state {trust_state!r} requires a .chameleon/ directory; "
            f"use fixture_repo or trust_state 'untrusted'/'n/a' instead"
        )
    from chameleon_mcp.tools import trust_profile
    result = trust_profile(str(repo_tmp), repo_tmp.name)
    if result.get("data", {}).get("status") != "success":
        raise ValueError(f"trust_profile failed: {result}")
    if trust_state == "stale":
        profile_path = repo_tmp / ".chameleon" / "profile.json"
        with open(profile_path, "ab") as f:
            f.write(b" ")


def run_scenario_mcp(scenario: dict) -> ScenarioResult:
    """Run one scenario through get_pattern_context (MCP layer)."""
    from chameleon_mcp.tools import get_pattern_context

    fixture_repo = scenario.get("fixture_repo")
    file_path = scenario["file_path"]
    file_content = scenario.get("file_content", "")
    trust_state = scenario.get("trust_state", "trusted")
    name = scenario["name"]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as repo_tmp_str, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_tmp_str:
        repo_tmp = Path(repo_tmp_str)
        _prev_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        _prev_allow_tmp = os.environ.get("CHAMELEON_ALLOW_TMP_REPO")
        os.environ["CHAMELEON_PLUGIN_DATA"] = data_tmp_str
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        try:
            if fixture_repo is not None:
                src = FIXTURES_DIR / fixture_repo
                if not src.is_dir():
                    return ScenarioResult(
                        name=name,
                        status="ERROR",
                        mismatches=[f"fixture not found: {src}"],
                    )
                shutil.copytree(src, repo_tmp, dirs_exist_ok=True)
            else:
                _synthesize_no_profile_marker(repo_tmp, file_path)

            _apply_trust_state(repo_tmp, trust_state)

            target = repo_tmp / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")

            response = get_pattern_context(str(target))
            return assert_scenario(scenario, response)
        except Exception as exc:
            return ScenarioResult(name=name, status="ERROR", mismatches=[repr(exc)])
        finally:
            if _prev_plugin_data is None:
                os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
            else:
                os.environ["CHAMELEON_PLUGIN_DATA"] = _prev_plugin_data
            if _prev_allow_tmp is None:
                os.environ.pop("CHAMELEON_ALLOW_TMP_REPO", None)
            else:
                os.environ["CHAMELEON_ALLOW_TMP_REPO"] = _prev_allow_tmp


def full_mode_capability_check() -> tuple[bool, str]:
    """Return (ok, reason). All three must be present for --full mode."""
    if shutil.which("bash") is None:
        return False, "bash not on PATH"
    if not HOOK_SCRIPT.is_file() or not os.access(HOOK_SCRIPT, os.X_OK):
        return False, f"hook script missing or not executable: {HOOK_SCRIPT}"
    venv_python = REPO_ROOT / "mcp" / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return False, f"mcp venv python missing: {venv_python}"
    return True, "ok"


def _read_mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None


def _read_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


def run_scenario_full(scenario: dict) -> ScenarioResult:
    """Pipe a synthetic PreToolUse event through hooks/preflight-and-advise."""
    fixture_repo = scenario.get("fixture_repo")
    file_path = scenario["file_path"]
    file_content = scenario.get("file_content", "")
    trust_state = scenario.get("trust_state", "trusted")
    name = scenario["name"]

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as repo_tmp_str, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_tmp_str:
        repo_tmp = Path(repo_tmp_str)
        _prev_plugin_data = os.environ.get("CHAMELEON_PLUGIN_DATA")
        _prev_allow_tmp = os.environ.get("CHAMELEON_ALLOW_TMP_REPO")
        _prev_log = os.environ.get("CHAMELEON_HOOK_ERROR_LOG")
        per_session_log = Path(data_tmp_str) / ".hook_errors.log"
        os.environ["CHAMELEON_PLUGIN_DATA"] = data_tmp_str
        os.environ["CHAMELEON_ALLOW_TMP_REPO"] = "1"
        os.environ["CHAMELEON_HOOK_ERROR_LOG"] = str(per_session_log)
        try:
            if fixture_repo is not None:
                src = FIXTURES_DIR / fixture_repo
                if not src.is_dir():
                    return ScenarioResult(
                        name=name,
                        status="ERROR",
                        mismatches=[f"fixture not found: {src}"],
                    )
                shutil.copytree(src, repo_tmp, dirs_exist_ok=True)
            else:
                _synthesize_no_profile_marker(repo_tmp, file_path)

            _apply_trust_state(repo_tmp, trust_state)

            target = repo_tmp / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_content, encoding="utf-8")

            event = {
                "tool_name": "Edit",
                "tool_input": {"file_path": str(target)},
                "session_id": "hook_evals",
            }

            # Pre-create the log file at 0 bytes. The hook's ">>" redirect
            # also creates it if absent, but writes nothing on success — so
            # size stays 0. Any actual error write grows the file; we detect
            # that via size rather than mtime to avoid sub-second false positives.
            per_session_log.touch()
            log_size_before = _read_size(per_session_log)

            proc = subprocess.run(
                ["bash", str(HOOK_SCRIPT)],
                input=json.dumps(event).encode("utf-8"),
                capture_output=True,
                env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)},
                timeout=10,
            )

            log_size_after = _read_size(per_session_log)
            if log_size_after > log_size_before:
                stderr_excerpt = proc.stderr.decode('utf-8', 'replace')[-400:]
                log_excerpt = ""
                try:
                    log_excerpt = per_session_log.read_text(encoding="utf-8", errors="replace")[-400:]
                except FileNotFoundError:
                    pass
                return ScenarioResult(
                    name=name,
                    status="HOOK_FAILED",
                    mismatches=[
                        f"hook fail-opened; per-session log grew. log: {log_excerpt!r}; stderr: {stderr_excerpt!r}"
                    ],
                )

            try:
                hook_out = json.loads(proc.stdout.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                return ScenarioResult(
                    name=name,
                    status="ERROR",
                    mismatches=[f"hook stdout was not JSON: {exc!r}"],
                )

            advisory_text = ""
            hook_specific = hook_out.get("hookSpecificOutput")
            if isinstance(hook_specific, dict):
                advisory_text = hook_specific.get("additionalContext", "")
            if not advisory_text:
                advisory_text = hook_out.get("additionalContext", "")

            expected = scenario.get("expected", {})
            mismatches = []

            # Empty advisory means the hook intentionally produced no
            # injection — correct behavior for no_repo / no_profile and
            # for negative `archetype_name: null` cases. Treat it as PASS
            # unless the scenario explicitly expects substring content.
            no_advisory_states = {"no_repo", "no_profile", "profile_corrupted"}
            expected_profile_status = expected.get("profile_status")
            content_assertions = bool(
                expected.get("canonical_excerpt_includes")
                or expected.get("rules_must_include_substring")
                or expected.get("idioms_must_include_substring")
            )
            if not advisory_text and (
                expected_profile_status in no_advisory_states
                or expected.get("archetype_name") is None
            ) and not content_assertions:
                return ScenarioResult(name=name, status="PASS")

            expected_arch = expected.get("archetype_name", "<unset>")
            if expected_arch != "<unset>":
                if expected_arch is None:
                    if advisory_text:
                        mismatches.append("expected no advisory but blob is non-empty")
                else:
                    if expected_arch not in advisory_text:
                        mismatches.append(
                            f"advisory blob missing archetype hint {expected_arch!r}"
                        )

            expected_trust = expected.get("trust_state")
            if expected_trust is not None:
                if expected_trust not in advisory_text:
                    mismatches.append(
                        f"advisory blob missing trust_state hint {expected_trust!r}"
                    )

            if expected_profile_status is not None and expected_profile_status not in no_advisory_states:
                if expected_profile_status not in advisory_text:
                    mismatches.append(
                        f"advisory blob missing profile_status hint {expected_profile_status!r}"
                    )

            for needle in expected.get("canonical_excerpt_includes", []) or []:
                if needle not in advisory_text:
                    mismatches.append(f"advisory blob missing substring {needle!r}")

            if mismatches:
                return ScenarioResult(name=name, status="FAIL", mismatches=mismatches)
            return ScenarioResult(name=name, status="PASS")
        except subprocess.TimeoutExpired:
            return ScenarioResult(name=name, status="HOOK_FAILED", mismatches=["hook timed out"])
        except Exception as exc:
            return ScenarioResult(name=name, status="ERROR", mismatches=[repr(exc)])
        finally:
            if _prev_plugin_data is None:
                os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
            else:
                os.environ["CHAMELEON_PLUGIN_DATA"] = _prev_plugin_data
            if _prev_allow_tmp is None:
                os.environ.pop("CHAMELEON_ALLOW_TMP_REPO", None)
            else:
                os.environ["CHAMELEON_ALLOW_TMP_REPO"] = _prev_allow_tmp
            if _prev_log is None:
                os.environ.pop("CHAMELEON_HOOK_ERROR_LOG", None)
            else:
                os.environ["CHAMELEON_HOOK_ERROR_LOG"] = _prev_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run scenarios through hooks/preflight-and-advise")
    args = parser.parse_args(argv)

    scenarios = discover_scenarios(SCENARIOS_DIR)
    results: list[ScenarioResult] = []

    if args.full:
        ok, reason = full_mode_capability_check()
        if not ok:
            print(json.dumps({"status": "skipped", "reason": reason}, indent=2))
            print(f"Summary: 0 run, 0 passed, 0 failed (skipped: {reason})", file=sys.stderr)
            return 0

    for scenario in scenarios:
        if args.full:
            result = run_scenario_full(scenario)
        else:
            result = run_scenario_mcp(scenario)
        results.append(result)

        sys.stderr.write(f"[{result.status}] {scenario['name']}\n")
        for m in result.mismatches:
            sys.stderr.write(f"    {m}\n")

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status != "PASS")
    summary = {
        "mode": "full" if args.full else "mcp",
        "scenarios_run": len(results),
        "passed": passed,
        "failed": failed,
        "results": [
            {"name": r.name, "status": r.status, "mismatches": r.mismatches}
            for r in results
        ],
    }
    print(json.dumps(summary, indent=2))
    print(f"Summary: {len(results)} run, {passed} passed, {failed} failed", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
