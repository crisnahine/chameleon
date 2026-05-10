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
import os
import shutil
import subprocess
import sys
from pathlib import Path

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")
EF_CLIENT = Path("/Users/crisn/Documents/Projects/empire-flippers/client")


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

# Ensure EF client is bootstrapped + trusted before we run
from chameleon_mcp.tools import bootstrap_repo, trust_profile
if not (EF_CLIENT / ".chameleon" / "profile.json").is_file():
    bootstrap_repo(str(EF_CLIENT))
trust_profile(str(EF_CLIENT), "client")


def run_claude(prompt: str, allowed_tools: str = "") -> list[dict]:
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
        cwd=str(EF_CLIENT),
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


# ---------------------------------------------------------------------------
# Round 1: plugin + hooks + MCP registration
# ---------------------------------------------------------------------------
section("Round 1 — plugin loads + hooks + MCP server connects")

events = run_claude("Reply with the single word: OK")

init = next((e for e in events if e.get("subtype") == "init"), None)
t("init event present", init is not None)

if init:
    plugins = init.get("plugins", [])
    t(
        "Chameleon plugin loaded",
        any(p.get("name") == "chameleon" for p in plugins),
    )

    mcp_servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
    chameleon_mcp_status = mcp_servers.get("plugin:chameleon:chameleon-mcp")
    t(
        f"chameleon-mcp server status (got: {chameleon_mcp_status})",
        chameleon_mcp_status == "connected",
    )

    tools = init.get("tools", [])
    chameleon_tools = [
        x for x in tools if x.startswith("mcp__plugin_chameleon_chameleon-mcp__")
    ]
    t(
        f"All 13 chameleon tools registered (got {len(chameleon_tools)})",
        len(chameleon_tools) == 13,
    )

    skills = init.get("skills", [])
    chameleon_skills = [s for s in skills if s.startswith("chameleon:")]
    t(
        f"All 8 chameleon skills registered (got {len(chameleon_skills)})",
        len(chameleon_skills) == 8,
    )


# ---------------------------------------------------------------------------
# Round 1 — SessionStart hook fires with using-chameleon SKILL
# ---------------------------------------------------------------------------
section("Round 1 — SessionStart hook injection")

session_start = next(
    (e for e in events
     if e.get("hook_event") == "SessionStart" and e.get("subtype") == "hook_response"),
    None,
)
t("SessionStart hook_response event present", session_start is not None)

if session_start:
    output = session_start.get("output", "")
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = {}
    additional = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
    t("additionalContext non-empty", len(additional) > 100)
    t("additionalContext mentions using-chameleon", "using-chameleon" in additional)
    t(
        "additionalContext contains the 'Before any Edit' rule",
        "Before any Edit" in additional,
    )


# ---------------------------------------------------------------------------
# Round 2 — Claude can actually call chameleon MCP tools (trust path bug)
# ---------------------------------------------------------------------------
section("Round 2 — chameleon-mcp tool invocation + trust resolution")

events2 = run_claude(
    "Call chameleon-mcp's detect_repo tool on src/index.tsx and report the trust_state value, nothing else.",
    allowed_tools="mcp__plugin_chameleon_chameleon-mcp__detect_repo",
)

# Find the tool result for detect_repo
tool_result = None
for e in events2:
    msg = e.get("message", {})
    content = msg.get("content")
    if not isinstance(content, list):
        continue
    for item in content:
        if (
            isinstance(item, dict)
            and item.get("type") == "tool_result"
            and isinstance(item.get("content"), list)
        ):
            for inner in item["content"]:
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text", "")
                    if "trust_state" in text and "repo_id" in text:
                        try:
                            tool_result = json.loads(text)
                        except json.JSONDecodeError:
                            pass

t("detect_repo tool was invoked + returned a result", tool_result is not None)
if tool_result:
    trust = tool_result.get("data", {}).get("trust_state")
    t(
        f"detect_repo returns trust_state=trusted from Claude Code (got {trust})",
        trust == "trusted",
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
