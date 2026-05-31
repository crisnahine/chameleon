#!/usr/bin/env python3
"""Drive real repo files through chameleon's PreToolUse and PostToolUse hooks.

Discovers representative files from CHAMELEON_TEST_TS_REPO and
CHAMELEON_TEST_RUBY_REPO, then runs each through the actual hook scripts
(preflight-and-advise, posttool-verify) to verify the end-to-end pipeline:
PreToolUse injects archetype context for trusted profiles, and PostToolUse
lints the written content without crashing.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CHAMELEON_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = CHAMELEON_ROOT / "hooks"
MCP_DIR = CHAMELEON_ROOT / "mcp"
TS_REPO = os.environ.get("CHAMELEON_TEST_TS_REPO", "")
RUBY_REPO = os.environ.get("CHAMELEON_TEST_RUBY_REPO", "")

sys.path.insert(0, str(MCP_DIR))
from chameleon_mcp.tools import detect_repo, get_archetype  # noqa: E402

_PROBE_CAP = 200


def recognized_file(repo: str, *globs: str, exclude: tuple[str, ...] = ()) -> str | None:
    """First repo file matching a glob whose archetype the profile recognizes."""
    root = Path(repo)
    probed = 0
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            if not p.is_file() or any(x in p.name for x in exclude):
                continue
            probed += 1
            if probed > _PROBE_CAP:
                return None
            try:
                data = get_archetype(repo, str(p)).get("data", {})
            except Exception:
                continue
            if data.get("archetype") and data.get("match_quality") != "none":
                return str(p)
    return None


def discover_tasks() -> list[dict]:
    tasks: list[dict] = []
    if TS_REPO:
        ts_specs = [
            ("component", ("**/*.tsx",), (".test.", ".stories.", ".spec.")),
            ("module", ("**/*.ts",), (".d.ts", ".test.", ".spec.")),
        ]
        for label, globs, exclude in ts_specs:
            f = recognized_file(TS_REPO, *globs, exclude=exclude)
            if f:
                tasks.append({"label": f"ts-{label}", "repo": TS_REPO, "file": f})
    if RUBY_REPO:
        rb_specs = [
            ("controller", ("app/controllers/**/*_controller.rb",), ("application_controller",)),
            ("model", ("app/models/**/*.rb",), ("application_record", "application_mailer")),
            ("service", ("app/services/**/*.rb",), ()),
            ("spec", ("spec/**/*_spec.rb", "test/**/*_test.rb"), ()),
        ]
        for label, globs, exclude in rb_specs:
            f = recognized_file(RUBY_REPO, *globs, exclude=exclude)
            if f:
                tasks.append({"label": f"rb-{label}", "repo": RUBY_REPO, "file": f})
    return tasks


def run_hook(hook_name: str, payload: dict, cwd: str) -> dict:
    """Run a chameleon hook script with the given payload on stdin."""
    script = HOOK_DIR / hook_name
    env = {
        **os.environ,
        "CLAUDE_CWD": cwd,
        "CLAUDE_PLUGIN_ROOT": str(CHAMELEON_ROOT),
        "PYTHONPATH": str(MCP_DIR),
    }
    try:
        result = subprocess.run(
            ["bash", str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=15,
            cwd=cwd,
            env=env,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        parsed = json.loads(stdout)
        if "hookSpecificOutput" in parsed:
            return parsed["hookSpecificOutput"]
        return parsed
    except Exception as e:
        return {"_error": str(e)}


def main():
    if not TS_REPO and not RUBY_REPO:
        print("SKIP: set CHAMELEON_TEST_TS_REPO and/or CHAMELEON_TEST_RUBY_REPO")
        return

    trust = {}
    for repo in {TS_REPO, RUBY_REPO} - {""}:
        trust[repo] = detect_repo(repo).get("data", {}).get("trust_state")

    tasks = discover_tasks()
    if not tasks:
        print("SKIP: no representative files discovered in the target repo(s)")
        return

    results = []
    print("=" * 80)
    print(f"CHAMELEON HOOK SIMULATION: {len(tasks)} REAL FILES")
    print("=" * 80)

    for i, task in enumerate(tasks, 1):
        repo = task["repo"]
        fpath = task["file"]
        content = Path(fpath).read_text(encoding="utf-8", errors="replace")
        rel = os.path.relpath(fpath, repo)
        sid = f"sim-{os.getpid()}-{i}"
        print(f"\n{'-' * 80}")
        print(f"Task {i}/{len(tasks)}: [{task['label']}] {rel}")

        pre_payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": fpath},
            "session_id": sid,
        }
        t0 = time.monotonic()
        pre_result = run_hook("preflight-and-advise", pre_payload, repo)
        pre_ms = (time.monotonic() - t0) * 1000

        pre_error = pre_result.get("_error")
        context_text = pre_result.get("additionalContext", "")
        has_pattern = "🦎 chameleon:" in context_text and "degraded" not in context_text.lower()

        if trust.get(repo) in ("trusted", "stale"):
            pre_pass = pre_error is None and has_pattern
        else:
            pre_pass = pre_error is None

        print(
            f"  PreToolUse: {pre_ms:.0f}ms  pattern_context={has_pattern} "
            f"trust={trust.get(repo)}  RESULT={'PASS' if pre_pass else 'FAIL'}"
        )
        if pre_error:
            print(f"    ERROR: {pre_error}")

        post_payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": fpath},
            "tool_response": {"content": content, "success": True},
            "session_id": sid,
        }
        t0 = time.monotonic()
        post_result = run_hook("posttool-verify", post_payload, repo)
        post_ms = (time.monotonic() - t0) * 1000

        post_error = post_result.get("_error")
        post_context = post_result.get("additionalContext", "")
        has_violations = "violation" in post_context.lower()
        post_pass = post_error is None

        print(
            f"  PostToolUse: {post_ms:.0f}ms  violations_surfaced={has_violations}  "
            f"RESULT={'PASS' if post_pass else 'FAIL'}"
        )
        if post_error:
            print(f"    ERROR: {post_error}")

        results.append(
            {
                "task": task["label"],
                "pre_pass": pre_pass,
                "post_pass": post_pass,
                "pre_ms": pre_ms,
                "post_ms": post_ms,
            }
        )

    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")
    n = len(results)
    pre_passed = sum(1 for r in results if r["pre_pass"])
    post_passed = sum(1 for r in results if r["post_pass"])
    all_passed = sum(1 for r in results if r["pre_pass"] and r["post_pass"])
    avg_pre = sum(r["pre_ms"] for r in results) / n
    avg_post = sum(r["post_ms"] for r in results) / n

    print(f"\n  PreToolUse:  {pre_passed}/{n} PASS")
    print(f"  PostToolUse: {post_passed}/{n} PASS")
    print(f"  Combined:    {all_passed}/{n} PASS")
    print(f"\n  Avg PreToolUse latency:  {avg_pre:.0f}ms")
    print(f"  Avg PostToolUse latency: {avg_post:.0f}ms")

    for r in results:
        status = "PASS" if r["pre_pass"] and r["post_pass"] else "FAIL"
        print(
            f"  {r['task']}: Pre={'OK' if r['pre_pass'] else 'FAIL'} "
            f"Post={'OK' if r['post_pass'] else 'FAIL'} "
            f"({r['pre_ms']:.0f}ms + {r['post_ms']:.0f}ms) [{status}]"
        )

    sys.exit(0 if all_passed == n else 1)


if __name__ == "__main__":
    main()
