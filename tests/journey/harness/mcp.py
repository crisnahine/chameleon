"""Direct MCP tool calls via stdio. RUNNER INSTRUMENTATION ONLY.

Use for state introspection (e.g., list_profiles to verify registration),
NOT for replacing user-facing flows. Bypassing /chameleon-init with
bootstrap_repo() defeats the test purpose.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def call_mcp_tool(
    tool_name: str,
    plugin_root: Path,
    env: dict[str, str],
    timeout_s: int = 30,
    **args: Any,
) -> dict:
    """Spawn the MCP server, call one tool, return its envelope.

    Each call is a fresh subprocess. For batched calls use a session
    (deferred to v2).
    """
    server_cmd = [
        str(plugin_root / "mcp" / ".venv" / "bin" / "python"),
        "-m",
        "chameleon_mcp.server",
    ]
    proc_env = os.environ.copy()
    proc_env.update(env)
    proc_env["PYTHONPATH"] = str(plugin_root / "mcp")

    init_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "journey-harness", "version": "1.0"},
        },
    })
    initialized_notification = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    call_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    })
    stdin_payload = init_msg + "\n" + initialized_notification + "\n" + call_msg + "\n"

    proc = subprocess.run(
        server_cmd,
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=proc_env,
        check=False,
    )

    # Parse last JSON-RPC response (id=2)
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2:
            if "result" in obj:
                return obj["result"]
            if "error" in obj:
                raise RuntimeError(f"MCP error: {obj['error']}")
    raise RuntimeError(
        f"no response for tool {tool_name!r}; stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
