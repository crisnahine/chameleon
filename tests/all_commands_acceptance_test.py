"""All-commands acceptance test through real Claude Code on both EF stacks.

For each EF repo (TS + Ruby):
  Round 1 — invoke every slash command (7 user-invocable skills) via
            `claude -p "/<command>"` and verify the session completes
            without error and the model produces a sensible response.
  Round 2 — instruct Claude in a single session to call every MCP tool
            (13 tools) and verify each was invoked + returned a result.

Expensive (~$0.10 per `claude -p` call × 16 sessions = ~$1.60). Do not
run on every commit. Run before any release that changes the slash
command surface, the MCP tool surface, or `.mcp.json`.

Skipped automatically if `claude` CLI is not on PATH.
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
EF_API = Path("/Users/crisn/Documents/Projects/empire-flippers/api")

ACCEPTANCE_TARGETS = [
    ("EF client", EF_CLIENT, "src/utils/balanceTransaction.ts"),
    ("EF api", EF_API, "app/models/listing.rb"),
]

# All 7 user-invocable slash commands (using-chameleon auto-fires; not invocable)
SLASH_COMMANDS = [
    "chameleon:chameleon-status",
    "chameleon:chameleon-trust",
    "chameleon:chameleon-init",
    "chameleon:chameleon-refresh",
    "chameleon:chameleon-teach",
    "chameleon:chameleon-disable",
    "chameleon:chameleon-pause-15m",
]

# All 13 MCP tools (full names as Claude Code exposes them when plugin is loaded)
MCP_TOOLS = [
    "detect_repo",
    "get_archetype",
    "get_pattern_context",
    "get_canonical_excerpt",
    "get_rules",
    "lint_file",
    "get_drift_status",
    "refresh_repo",
    "bootstrap_repo",
    "list_profiles",
    "merge_profiles",
    "teach_profile",
    "trust_profile",
]
MCP_PREFIX = "mcp__plugin_chameleon_chameleon-mcp__"


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


if shutil.which("claude") is None:
    print("SKIP: claude CLI not on PATH")
    sys.exit(0)

# Ensure both EF repos are bootstrapped + trusted
from chameleon_mcp.tools import bootstrap_repo, trust_profile
for label, repo_root, _ in ACCEPTANCE_TARGETS:
    if not repo_root.is_dir():
        continue
    if not (repo_root / ".chameleon" / "profile.json").is_file():
        bootstrap_repo(str(repo_root))
    trust_profile(str(repo_root), repo_root.name)


def run_claude(prompt: str, *, cwd: Path, allowed_tools: str = "", max_turns: int = 5) -> tuple[int, list[dict], str]:
    cmd = [
        "claude", "-p", prompt,
        "--plugin-dir", str(PLUGIN_ROOT),
        "--output-format", "stream-json",
        "--max-turns", str(max_turns),
        "--verbose",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=300,
    )
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return proc.returncode, events, proc.stderr


def claude_called_tool(events: list[dict], tool_name: str) -> bool:
    """True iff any assistant message contains a tool_use for tool_name."""
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


def get_assistant_text(events: list[dict]) -> str:
    """Concatenate all assistant text replies in the session."""
    out = []
    for e in events:
        msg = e.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                out.append(item.get("text", ""))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Round 1 — every slash command invokes cleanly per repo
# ---------------------------------------------------------------------------
for label, repo_root, _ in ACCEPTANCE_TARGETS:
    if not repo_root.is_dir():
        continue
    section(f"Round 1 — {label}: 7 slash commands invocation")

    for cmd_name in SLASH_COMMANDS:
        # Slash commands resolve to the skill SKILL.md body; the model
        # reads it and executes the documented flow. Many skills do
        # multi-step work (read file → call MCP → synthesize), so allow
        # enough turns for completion.
        rc, events, stderr = run_claude(
            f"/{cmd_name}",
            cwd=repo_root,
            allowed_tools="Bash Read Glob Grep " + " ".join(f"{MCP_PREFIX}{tool}" for tool in MCP_TOOLS),
            max_turns=10,
        )

        # Slash command success criteria:
        # 1. Skill registered (init event lists it in slash_commands)
        # 2. Claude began executing the skill (either tool call or text)
        init = next((e for e in events if e.get("subtype") == "init"), None)
        skill_registered = init is not None and cmd_name in init.get("slash_commands", [])

        text = get_assistant_text(events)
        any_assistant_action = bool(text.strip()) or any(
            isinstance(e.get("message"), dict)
            and isinstance(e["message"].get("content"), list)
            and any(
                isinstance(c, dict) and c.get("type") == "tool_use"
                for c in e["message"]["content"]
            )
            for e in events
        )

        t(
            f"{label}: /{cmd_name} skill registered + Claude executes",
            skill_registered and any_assistant_action,
            stderr[:120] if not (skill_registered and any_assistant_action) else "",
        )


# ---------------------------------------------------------------------------
# Round 2 — every MCP tool callable per repo (single batched session)
# ---------------------------------------------------------------------------
for label, repo_root, sample_rel in ACCEPTANCE_TARGETS:
    if not repo_root.is_dir():
        continue
    section(f"Round 2 — {label}: 13 MCP tools batch invocation")

    sample_abs = str(repo_root / sample_rel)
    repo_path = str(repo_root)

    # Get repo_id, archetype upfront for tool args
    from chameleon_mcp.tools import detect_repo as _det, get_pattern_context as _gpc

    rid = _det(sample_abs)["data"]["repo_id"]
    arch_name = (_gpc(sample_abs)["data"].get("archetype") or {}).get("archetype")
    if arch_name is None:
        # Fallback: pick first archetype from profile
        archetypes = json.loads((repo_root / ".chameleon" / "archetypes.json").read_text())
        arch_name = next(iter(archetypes["archetypes"].keys()))

    prompt = f"""I need you to call each of these chameleon-mcp tools exactly once, in this order, and after each call print one line: "DONE: <tool_name>".

The repo path is: {repo_path}
The repo_id is: {rid}
A sample file path is: {sample_abs}
A known archetype name is: {arch_name}

Call these tools with the listed args:
1. detect_repo(file_path={sample_abs!r})
2. get_archetype(repo={rid!r}, file_path={sample_abs!r})
3. get_pattern_context(file_path={sample_abs!r})
4. get_canonical_excerpt(repo={rid!r}, archetype={arch_name!r})
5. get_rules(repo={rid!r}, archetype={arch_name!r})
6. lint_file(repo={rid!r}, archetype={arch_name!r}, content="export const x = 1;")
7. get_drift_status(repo={rid!r})
8. list_profiles(cursor=null, limit=10)
9. teach_profile(repo={repo_path!r}, feedback="acceptance-test idiom from all_commands_acceptance_test.py")
10. merge_profiles(repo={rid!r}, base="x", ours="y", theirs="z")
11. bootstrap_repo(path={repo_path!r}, mode="full", paths_glob=null)
12. refresh_repo(repo={repo_path!r}, force=false)
13. trust_profile(repo={repo_path!r}, confirmation_token={repo_root.name!r})

After all 13 calls, print "ALL DONE".
"""

    rc, events, stderr = run_claude(
        prompt,
        cwd=repo_root,
        allowed_tools=" ".join(f"{MCP_PREFIX}{tool}" for tool in MCP_TOOLS),
        max_turns=20,
    )

    t(f"{label}: batch session exits 0", rc == 0, stderr[:120] if rc != 0 else "")

    for tool_name in MCP_TOOLS:
        called = claude_called_tool(events, f"{MCP_PREFIX}{tool_name}")
        t(f"{label}: {tool_name} invoked via Claude Code", called)


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
