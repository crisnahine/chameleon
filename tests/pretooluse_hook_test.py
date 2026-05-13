"""Verify PreToolUse preflight-and-advise hook fires in real Claude Code.

Round 1: synthetic Edit attempt against an the test repo file in real Claude
         Code; capture the PreToolUse event and verify it has the
         chameleon archetype context in additionalContext.
Round 2: same on the Ruby on Rails repo with a Ruby file.

History note: an earlier comment claimed `--permission-mode
bypassPermissions` suppressed PreToolUse hook firing. Verified false on
Claude Code 2.1.140 during the May 13 dogfood run; PreToolUse fires
normally in bypass mode. The test still uses the standard permission
flow + --allowedTools because that matches what real users hit.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _test_config import RUBY_REPO, TS_REPO

PASS, FAIL = [], []
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


if shutil.which("claude") is None:
    print("SKIP: claude CLI not on PATH")
    sys.exit(0)


from chameleon_mcp.tools import bootstrap_repo, trust_profile

for repo in (TS_REPO, RUBY_REPO):
    if not repo.is_dir():
        continue
    if not (repo / ".chameleon" / "profile.json").is_file():
        bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)


def run_edit_session(repo_root: Path, sample_rel: str, language: str):
    """Run a Claude Code session that triggers a REAL Edit on a file in a
    known archetype bucket.

    Edit refuses no-op replacements before hooks fire, so we make a real
    edit. The target lives inside a directory with an existing archetype
    (src/utils/ for TS, app/models/ for Ruby) so get_pattern_context
    returns a non-null archetype and the hook emits archetype context
    instead of {}. The file is deleted afterwards.
    """
    if language == "typescript":
        target = repo_root / "src" / "utils" / "_chameleon_pretool_test_target.ts"
        target.write_text("export const placeholder = 'before';\n")
        old, new = "before", "after"
    else:
        target = repo_root / "app" / "models" / "_chameleon_pretool_test_target.rb"
        target.write_text("class PlaceholderModel < ApplicationRecord\n  WORD = 'before'\nend\n")
        old, new = "before", "after"
    try:
        proc = subprocess.run(
            [
                "claude", "-p",
                f"Use Edit on {target} to replace {old!r} with {new!r} once. Only one Edit call.",
                "--plugin-dir", str(PLUGIN_ROOT),
                "--output-format", "stream-json",
                "--include-hook-events",
                "--max-turns", "4",
                "--verbose",
                "--allowedTools", "Edit Read",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=180,
        )
        events = []
        for line in proc.stdout.splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
    finally:
        target.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Round 1 — the TypeScript repo TypeScript: PreToolUse fires with archetype context
# ---------------------------------------------------------------------------
section("Round 1 — the TypeScript repo: PreToolUse hook fires on Edit")

events = run_edit_session(TS_REPO, "_test_target.ts", "typescript")

# Filter for the Edit-specific hook fire. The session may also contain
# PreToolUse fires for chameleon's own MCP calls (get_pattern_context,
# etc.); we only care about the user's Edit tool here.
pretool_responses = [
    e for e in events
    if e.get("subtype") == "hook_response"
    and e.get("hook_event") == "PreToolUse"
    and e.get("hook_name") == "PreToolUse:Edit"
]
t(
    f"the TypeScript repo: PreToolUse:Edit hook_response present ({len(pretool_responses)})",
    len(pretool_responses) >= 1,
)

if pretool_responses:
    # Claude Code may fire the PreToolUse hook multiple times per Edit; the
    # chameleon hook dedups within one turn so only one emits content and the
    # rest emit {}. Pick the first response with a non-empty output.
    ev = next((e for e in pretool_responses if e.get("output")), pretool_responses[0])
    t(
        f"the TypeScript repo: hook_name is PreToolUse:Edit (got {ev.get('hook_name')})",
        ev.get("hook_name") == "PreToolUse:Edit",
    )
    output_str = ev.get("output", "")
    try:
        parsed = json.loads(output_str)
    except json.JSONDecodeError:
        parsed = {}
    spec = parsed.get("hookSpecificOutput") or {}
    additional = spec.get("additionalContext") or ""
    t(
        "the TypeScript repo: additionalContext non-empty",
        len(additional) > 50,
    )
    t(
        "the TypeScript repo: additionalContext contains archetype prefix",
        "[chameleon: archetype=" in additional,
    )
    t(
        "the TypeScript repo: additionalContext contains canonical witness section",
        "Canonical witness:" in additional,
    )


# ---------------------------------------------------------------------------
# Round 2 — the Ruby on Rails repo Ruby: PreToolUse fires with archetype context
# ---------------------------------------------------------------------------
section("Round 2 — the Ruby on Rails repo: PreToolUse hook fires on Ruby Edit")

events = run_edit_session(RUBY_REPO, "_test_target.rb", "ruby")

pretool_responses = [
    e for e in events
    if e.get("subtype") == "hook_response"
    and e.get("hook_event") == "PreToolUse"
    and e.get("hook_name") == "PreToolUse:Edit"
]
t(
    f"the Ruby on Rails repo: PreToolUse:Edit hook_response present ({len(pretool_responses)})",
    len(pretool_responses) >= 1,
)

if pretool_responses:
    ev = next((e for e in pretool_responses if e.get("output")), pretool_responses[0])
    output_str = ev.get("output", "")
    try:
        parsed = json.loads(output_str)
    except json.JSONDecodeError:
        parsed = {}
    spec = parsed.get("hookSpecificOutput") or {}
    additional = spec.get("additionalContext") or ""
    t(
        "the Ruby on Rails repo: additionalContext mentions Ruby archetype",
        "[chameleon: archetype=" in additional,
    )


# ---------------------------------------------------------------------------
# Round 2 — opt-out suppresses PreToolUse injection in real Claude Code
# ---------------------------------------------------------------------------
section("Round 2 — opt-out suppresses PreToolUse injection in real session")

# Set CHAMELEON_DISABLE for the subprocess
env = os.environ.copy()
env["CHAMELEON_DISABLE"] = "1"

target = TS_REPO / "src" / "utils" / "_chameleon_optout_test.ts"
target.write_text("export const placeholder = 'before';\n")
try:
    proc = subprocess.run(
        [
            "claude", "-p",
            f"Use Edit on {target} to replace 'before' with 'after' once.",
            "--plugin-dir", str(PLUGIN_ROOT),
            "--output-format", "stream-json",
            "--include-hook-events",
            "--max-turns", "4",
            "--verbose",
            "--allowedTools", "Edit Read",
        ],
        cwd=str(TS_REPO),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
finally:
    target.unlink(missing_ok=True)
events = []
for line in proc.stdout.splitlines():
    if line.strip():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

pretool_responses = [
    e for e in events
    if e.get("subtype") == "hook_response" and e.get("hook_event") == "PreToolUse"
]
if pretool_responses:
    ev = pretool_responses[0]
    output_str = ev.get("output", "")
    try:
        parsed = json.loads(output_str)
    except json.JSONDecodeError:
        parsed = {}
    additional = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    t(
        "CHAMELEON_DISABLE: hook still fires (preserves safety hard-deny chain)",
        len(pretool_responses) >= 1,
    )
    t(
        "CHAMELEON_DISABLE: additionalContext empty (no archetype injection)",
        "[chameleon: archetype=" not in additional,
    )


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
