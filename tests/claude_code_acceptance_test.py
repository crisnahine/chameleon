"""End-to-end acceptance test inside real Claude Code.

This is the test that closes the "never ran in actual Claude Code" gap.
Spawns `claude --plugin-dir <chameleon> -p ...` and parses the
stream-json output, asserting:

- Plugin loads (`init.plugins` contains chameleon).
- SessionStart hook fires and injects using-chameleon SKILL.md.
- MCP server is registered and connected
  (`init.mcp_servers` contains "plugin:chameleon:chameleon-mcp" with
  status="connected").
- All 13 chameleon-mcp tools appear in `init.tools`.
- A direct call to `mcp__plugin_chameleon_chameleon-mcp__detect_repo`
  returns `trust_state: trusted` (this catches the
  CLAUDE_PLUGIN_DATA-conflict bug — different launchers reading
  different trust paths).

Skipped automatically if the `claude` CLI isn't on PATH or the user is
not authenticated. This costs ~$0.10 per run; do not run on every
commit, but run before any release that touches plugin manifest,
hooks, or trust storage paths.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from _test_config import RUBY_REPO, TS_REPO

PASS, FAIL = [], []
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Each entry: (label, repo_root, sample_file_relative_path, language).
# The sample file MUST be a member of a known archetype so
# get_pattern_context returns a non-null archetype name. Entry-point files
# (src/index.tsx, config/application.rb) are typically singletons that
# don't cluster — using a known-canonical witness avoids that gotcha.
ACCEPTANCE_TARGETS = [
    ("the TypeScript repo (TypeScript)", TS_REPO, "src/utils/balanceTransaction.ts", "typescript"),
    ("the Ruby on Rails repo (Ruby on Rails)", RUBY_REPO, "app/models/listing.rb", "ruby"),
]


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


# Skip if claude CLI not available
if shutil.which("claude") is None:
    print("SKIP: claude CLI not on PATH")
    sys.exit(0)

# Ensure both test repos are bootstrapped + trusted before we run
from chameleon_mcp.tools import bootstrap_repo, trust_profile

for label, repo_root, _, _ in ACCEPTANCE_TARGETS:
    if not repo_root.is_dir():
        continue
    if not (repo_root / ".chameleon" / "profile.json").is_file():
        bootstrap_repo(str(repo_root))
    trust_profile(str(repo_root), repo_root.name)


def run_claude(prompt: str, *, cwd: Path, allowed_tools: str = "") -> list[dict]:
    """Run `claude -p` with chameleon plugin loaded; return stream-json events."""
    cmd = [
        "claude", "-p", prompt,
        "--plugin-dir", str(PLUGIN_ROOT),
        "--output-format", "stream-json",
        "--include-hook-events",
        "--max-turns", "3",
        "--verbose",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])

    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=180,
    )
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def find_tool_result(events: list[dict], must_contain: str) -> dict | None:
    """Pull out the first tool_result whose text contains a marker substring."""
    for e in events:
        msg = e.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            inner_content = item.get("content")
            if not isinstance(inner_content, list):
                continue
            for inner in inner_content:
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text", "")
                    if must_contain in text:
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            continue
    return None


def claude_called_tool(events: list[dict], tool_name: str) -> bool:
    """True iff any assistant message in events contains a tool_use for tool_name."""
    for e in events:
        msg = e.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if (
                isinstance(item, dict)
                and item.get("type") == "tool_use"
                and item.get("name") == tool_name
            ):
                return True
    return False


# ---------------------------------------------------------------------------
# Per-repo acceptance: plugin + hooks + MCP + trust + skill-following
# ---------------------------------------------------------------------------
for label, repo_root, sample_rel, language in ACCEPTANCE_TARGETS:
    if not repo_root.is_dir():
        section(f"SKIP {label} — repo not present at {repo_root}")
        continue

    section(f"Round 1 — {label}: plugin loads + hooks + MCP server")

    events = run_claude(
        "Reply with the single word: OK",
        cwd=repo_root,
    )

    init = next((e for e in events if e.get("subtype") == "init"), None)
    t(f"{label}: init event present", init is not None)

    if init:
        plugins = init.get("plugins", [])
        t(
            f"{label}: chameleon plugin loaded",
            any(p.get("name") == "chameleon" for p in plugins),
        )

        mcp_servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
        chameleon_mcp_status = mcp_servers.get("plugin:chameleon:chameleon-mcp")
        t(
            f"{label}: chameleon-mcp connected (got {chameleon_mcp_status})",
            chameleon_mcp_status == "connected",
        )

        tools = init.get("tools", [])
        chameleon_tools = [
            x for x in tools if x.startswith("mcp__plugin_chameleon_chameleon-mcp__")
        ]
        t(
            f"{label}: chameleon MCP tools registered (got {len(chameleon_tools)})",
            len(chameleon_tools) >= 15,
        )

        skills = init.get("skills", [])
        chameleon_skills = [s for s in skills if s.startswith("chameleon:")]
        t(
            f"{label}: chameleon skills registered (got {len(chameleon_skills)})",
            len(chameleon_skills) >= 8,
        )

    section(f"Round 1 — {label}: SessionStart hook injects using-chameleon SKILL")

    # Claude Code may fire SessionStart multiple times in one boot (startup +
    # internal re-fires). The chameleon hook dedups: only the first one with
    # the matching session_id emits content, the rest emit empty `{}` so we
    # don't re-inject the skill on every fire. Scan ALL hook_responses and
    # pick the one with non-empty `output`, not the literal first one.
    session_starts = [
        e for e in events
        if e.get("hook_event") == "SessionStart" and e.get("subtype") == "hook_response"
    ]
    session_start = next((e for e in session_starts if e.get("output")), None)
    t(
        f"{label}: SessionStart hook_response with non-empty output present",
        session_start is not None,
    )

    if session_start:
        output = session_start.get("output", "")
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = {}
        additional = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        t(f"{label}: additionalContext non-empty", len(additional) > 100)
        t(
            f"{label}: additionalContext mentions using-chameleon",
            "using-chameleon" in additional,
        )
        t(
            f"{label}: additionalContext mentions both TS and Ruby",
            "TypeScript or Ruby" in additional,
        )

    # ----------------------------------------------------------------------
    # Round 2 — Claude can call chameleon MCP tools, trust resolves correctly
    # ----------------------------------------------------------------------
    section(f"Round 2 — {label}: detect_repo via real Claude Code")

    events2 = run_claude(
        f"Call chameleon-mcp's detect_repo tool on {sample_rel} and report only the trust_state value, nothing else.",
        cwd=repo_root,
        allowed_tools="mcp__plugin_chameleon_chameleon-mcp__detect_repo",
    )
    tool_result = find_tool_result(events2, "trust_state")
    t(
        f"{label}: detect_repo tool invoked + returned a result",
        tool_result is not None,
    )
    if tool_result:
        trust = tool_result.get("data", {}).get("trust_state")
        t(
            f"{label}: detect_repo trust_state=trusted (got {trust})",
            trust == "trusted",
        )

    # ----------------------------------------------------------------------
    # Round 2 — Claude follows the skill rule (calls get_pattern_context BEFORE Edit)
    # ----------------------------------------------------------------------
    section(f"Round 2 — {label}: skill-following on {language} file")

    events3 = run_claude(
        f"Use the Edit tool to change {repo_root}/{sample_rel} — replace the very first line with itself (no actual change). This is a hook test.",
        cwd=repo_root,
        allowed_tools="Edit Read mcp__plugin_chameleon_chameleon-mcp__get_pattern_context",
    )
    t(
        f"{label}: Claude called get_pattern_context (followed skill rule)",
        claude_called_tool(events3, "mcp__plugin_chameleon_chameleon-mcp__get_pattern_context"),
    )
    pc_result = find_tool_result(events3, "archetype")
    if pc_result:
        archetype_obj = pc_result.get("data", {}).get("archetype") or {}
        archetype = archetype_obj.get("archetype")
        t(
            f"{label}: get_pattern_context returned archetype for {language} file (got {archetype!r})",
            isinstance(archetype, str) and len(archetype) > 0,
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
