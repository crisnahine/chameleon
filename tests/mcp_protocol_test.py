"""End-to-end MCP stdio protocol test for chameleon-mcp.

Spawns the actual `chameleon-mcp` entry point that Claude Code spawns,
runs the MCP handshake, lists tools, and invokes every tool through the
real JSON-RPC pipeline.

Two rounds:
  Round 1: protocol handshake + list_tools + 13-tool registry
  Round 2: invoke each tool via call_tool with valid + invalid args
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASS, FAIL = [], []
PLUGIN_ROOT = Path("/Users/crisn/Documents/Projects/chameleon")
EF_CLIENT = Path("/Users/crisn/Documents/Projects/empire-flippers/client")
EF_API = Path("/Users/crisn/Documents/Projects/empire-flippers/api")
SERVER_BIN = PLUGIN_ROOT / "mcp" / ".venv" / "bin" / "chameleon-mcp"


def t(name, condition, info=""):
    (PASS if condition else FAIL).append((name, info))
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + info) if info else ''}")


def section(title):
    print(f"\n=== {title} ===")


EXPECTED_TOOLS = {
    "detect_repo", "get_archetype", "get_pattern_context",
    "get_canonical_excerpt", "get_rules", "lint_file",
    "get_drift_status", "refresh_repo", "bootstrap_repo",
    "list_profiles", "merge_profiles", "teach_profile", "trust_profile",
}


async def run_protocol_test():
    server_params = StdioServerParameters(
        command=str(SERVER_BIN),
        args=[],
        env=None,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # ----------------------------------------------------------------
            # Round 1: handshake + list tools
            # ----------------------------------------------------------------
            section("Round 1 — protocol handshake")

            init_result = await session.initialize()
            t(
                "initialize() returns server info",
                init_result is not None and hasattr(init_result, "serverInfo"),
            )
            t(
                "Server name is chameleon-mcp",
                "chameleon" in (init_result.serverInfo.name or "").lower(),
            )

            section("Round 1 — list_tools registry")

            tools_response = await session.list_tools()
            tool_names = {tool.name for tool in tools_response.tools}
            t(
                f"list_tools returns {len(tool_names)} tools",
                len(tool_names) == 13,
            )
            missing = EXPECTED_TOOLS - tool_names
            extra = tool_names - EXPECTED_TOOLS
            t(
                f"All 13 expected tools registered (missing: {missing}, extra: {extra})",
                not missing and not extra,
            )

            # Each tool must have a description and an input schema
            tools_with_no_desc = [
                tool.name for tool in tools_response.tools
                if not tool.description or len(tool.description) < 5
            ]
            t(
                f"All tools have descriptions (no-desc: {tools_with_no_desc})",
                not tools_with_no_desc,
            )

            tools_with_no_schema = [
                tool.name for tool in tools_response.tools
                if not tool.inputSchema
            ]
            t(
                f"All tools have inputSchema (no-schema: {tools_with_no_schema})",
                not tools_with_no_schema,
            )

            # ----------------------------------------------------------------
            # Round 2: call_tool for each tool with valid args
            # ----------------------------------------------------------------
            section("Round 2 — call_tool with valid args")

            # detect_repo (file inside EF client)
            r = await session.call_tool(
                "detect_repo",
                arguments={"file_path": str(EF_CLIENT / "src" / "index.tsx")},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool detect_repo succeeds",
                data.get("data", {}).get("repo_id") is not None,
            )

            client_repo_id = data["data"]["repo_id"]

            # get_pattern_context
            r = await session.call_tool(
                "get_pattern_context",
                arguments={"file_path": str(EF_CLIENT / "src" / "index.tsx")},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool get_pattern_context returns archetype info",
                "archetype" in data.get("data", {}),
            )

            # get_archetype
            r = await session.call_tool(
                "get_archetype",
                arguments={
                    "repo": client_repo_id,
                    "file_path": str(EF_CLIENT / "src" / "index.tsx"),
                },
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool get_archetype returns response",
                "archetype" in data.get("data", {}),
            )

            # get_canonical_excerpt — pull a real archetype from the profile
            archetypes_json = json.loads(
                (EF_CLIENT / ".chameleon" / "archetypes.json").read_text()
            )
            first_arch = next(iter(archetypes_json["archetypes"].keys()))
            r = await session.call_tool(
                "get_canonical_excerpt",
                arguments={"repo": client_repo_id, "archetype": first_arch},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool get_canonical_excerpt returns content",
                len(data.get("data", {}).get("content") or "") > 0,
            )

            # get_rules
            r = await session.call_tool(
                "get_rules",
                arguments={"repo": client_repo_id, "archetype": None},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool get_rules returns rules array",
                "rules" in data.get("data", {}),
            )

            # lint_file
            r = await session.call_tool(
                "lint_file",
                arguments={
                    "repo": client_repo_id,
                    "archetype": first_arch,
                    "content": "export const x = 1;",
                },
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool lint_file returns response",
                isinstance(data, dict),
            )

            # get_drift_status
            r = await session.call_tool(
                "get_drift_status",
                arguments={"repo": client_repo_id},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool get_drift_status returns recommended_action",
                "recommended_action" in data.get("data", {}),
            )

            # list_profiles
            r = await session.call_tool(
                "list_profiles",
                arguments={"cursor": None, "limit": 10},
            )
            data = json.loads(r.content[0].text)
            t(
                f"call_tool list_profiles returns ≥2 profiles",
                len(data.get("data", {}).get("profiles") or []) >= 2,
            )

            # teach_profile
            r = await session.call_tool(
                "teach_profile",
                arguments={
                    "repo": str(EF_CLIENT),
                    "feedback": "mcp-protocol-test idiom: prefer named exports",
                },
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool teach_profile returns success",
                data.get("data", {}).get("status") == "success",
            )

            # merge_profiles (still a stub, but should respond)
            r = await session.call_tool(
                "merge_profiles",
                arguments={
                    "repo": client_repo_id,
                    "base": "a", "ours": "b", "theirs": "c",
                },
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool merge_profiles returns response",
                isinstance(data, dict),
            )

            # bootstrap_repo (re-bootstrap; quick because already cached/idempotent)
            r = await session.call_tool(
                "bootstrap_repo",
                arguments={
                    "path": str(EF_CLIENT),
                    "mode": "full",
                    "paths_glob": None,
                },
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool bootstrap_repo returns status",
                data.get("data", {}).get("status") in ("success", "failed", "failed_unsupported_language", "failed_lock_held"),
            )

            # refresh_repo
            r = await session.call_tool(
                "refresh_repo",
                arguments={"repo": str(EF_CLIENT), "force": False},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool refresh_repo returns status",
                "status" in data.get("data", {}),
            )

            # trust_profile
            r = await session.call_tool(
                "trust_profile",
                arguments={"repo": str(EF_CLIENT), "confirmation_token": "client"},
            )
            data = json.loads(r.content[0].text)
            t(
                "call_tool trust_profile returns response",
                isinstance(data, dict),
            )

            # ----------------------------------------------------------------
            # Round 2: invalid arg handling
            # ----------------------------------------------------------------
            section("Round 2 — invalid argument handling")

            # detect_repo with empty string
            try:
                r = await session.call_tool(
                    "detect_repo",
                    arguments={"file_path": ""},
                )
                data = json.loads(r.content[0].text)
                t(
                    "detect_repo with empty file_path returns clean response",
                    isinstance(data, dict),
                )
            except Exception as e:
                t("detect_repo with empty file_path doesn't crash server", False, str(e)[:80])

            # detect_repo with nonexistent path
            try:
                r = await session.call_tool(
                    "detect_repo",
                    arguments={"file_path": "/totally/nonexistent/path/file.ts"},
                )
                data = json.loads(r.content[0].text)
                t(
                    "detect_repo with nonexistent path returns no_repo",
                    data.get("data", {}).get("profile_status") in ("no_repo", "no_profile"),
                )
            except Exception as e:
                t("detect_repo handles nonexistent path", False, str(e)[:80])

            # get_archetype with invalid repo_id
            try:
                r = await session.call_tool(
                    "get_archetype",
                    arguments={
                        "repo": "deadbeef" * 8,
                        "file_path": str(EF_CLIENT / "src" / "index.tsx"),
                    },
                )
                data = json.loads(r.content[0].text)
                t(
                    "get_archetype with invalid repo returns archetype=None",
                    data.get("data", {}).get("archetype") is None,
                )
            except Exception as e:
                t("get_archetype handles invalid repo_id", False, str(e)[:80])

            # bootstrap_repo on nonexistent path
            try:
                r = await session.call_tool(
                    "bootstrap_repo",
                    arguments={
                        "path": "/no/such/repo",
                        "mode": "full",
                        "paths_glob": None,
                    },
                )
                data = json.loads(r.content[0].text)
                t(
                    "bootstrap_repo on nonexistent path returns failed",
                    "failed" in data.get("data", {}).get("status", ""),
                )
            except Exception as e:
                t("bootstrap_repo handles nonexistent path", False, str(e)[:80])

            # ----------------------------------------------------------------
            # Round 2: response envelope structure
            # ----------------------------------------------------------------
            section("Round 2 — response envelope structure")

            r = await session.call_tool(
                "detect_repo",
                arguments={"file_path": str(EF_CLIENT / "src" / "index.tsx")},
            )
            data = json.loads(r.content[0].text)
            t(
                "Response has api_version field",
                "api_version" in data,
            )
            t(
                "Response has data field",
                "data" in data,
            )
            t(
                "api_version is a string",
                isinstance(data.get("api_version"), str),
            )

            # Multiple sequential calls don't degrade
            for i in range(5):
                r = await session.call_tool(
                    "detect_repo",
                    arguments={"file_path": str(EF_CLIENT / "src" / "index.tsx")},
                )
                data = json.loads(r.content[0].text)
                if data.get("data", {}).get("repo_id") != client_repo_id:
                    t("Sequential calls return consistent repo_id", False, f"call {i+1}")
                    break
            else:
                t("5 sequential calls return consistent repo_id", True)


def main():
    asyncio.run(run_protocol_test())
    print("\n=== Summary ===")
    print(f"  Total: {len(PASS) + len(FAIL)}")
    print(f"  Pass: {len(PASS)}")
    print(f"  Fail: {len(FAIL)}")
    if FAIL:
        print("\n  FAILURES:")
        for name, info in FAIL:
            print(f"    - {name}{(': ' + info) if info else ''}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
