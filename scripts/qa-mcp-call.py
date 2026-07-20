#!/usr/bin/env python3
"""Call a chameleon MCP tool over the real stdio transport. Dev-only tooling.

An in-process import of `chameleon_mcp` exercises the function but not the wire:
it skips the server launch, the JSON-RPC framing, the pydantic argument
validation, and the envelope the client actually receives. A verification run
that only imports the module cannot tell a broken tool registration from a
working one, so this client launches the server exactly the way `.mcp.json`
does and speaks the protocol.

    qa-mcp-call.py <tool> '<json args>' [--plugin-root DIR] [--timeout SEC]
    qa-mcp-call.py --list [--plugin-root DIR]

Exit code is 0 when the call returns a result, 1 when the server reports an
error, 2 on a transport or launch failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "plugins" / "cache" / "chameleon" / "chameleon"


def _latest_cache_root() -> Path:
    """The version-keyed cache dir Claude Code would load, newest version first."""
    if not DEFAULT_ROOT.is_dir():
        raise SystemExit(f"no plugin cache at {DEFAULT_ROOT}")

    def key(p: Path) -> tuple:
        return tuple(int(x) if x.isdigit() else -1 for x in p.name.split("."))

    dirs = [p for p in DEFAULT_ROOT.iterdir() if p.is_dir()]
    if not dirs:
        raise SystemExit(f"no versions under {DEFAULT_ROOT}")
    return sorted(dirs, key=key)[-1]


def _server_cmd(plugin_root: Path) -> list[str]:
    """Read the launch command out of the plugin's own .mcp.json, as the host does."""
    reg = json.loads((plugin_root / ".mcp.json").read_text())
    spec = reg["mcpServers"]["chameleon-mcp"]
    argv = [spec["command"], *spec["args"]]
    return [a.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)) for a in argv]


def _frame(msg: dict) -> str:
    return json.dumps(msg) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tool", nargs="?")
    ap.add_argument("args", nargs="?", default="{}")
    ap.add_argument("--plugin-root")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--raw", action="store_true", help="print the whole JSON-RPC result")
    ap.add_argument("--timeout", type=float, default=180.0)
    opts = ap.parse_args()

    root = Path(opts.plugin_root) if opts.plugin_root else _latest_cache_root()
    if not opts.list and not opts.tool:
        ap.error("a tool name is required unless --list is given")

    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(root))
    proc = subprocess.Popen(
        _server_cmd(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(root),
    )

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "qa-mcp-call", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    if opts.list:
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    else:
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": opts.tool, "arguments": json.loads(opts.args)},
        }

    payload = _frame(init) + _frame(initialized) + _frame(call)
    try:
        out, err = proc.communicate(payload, timeout=opts.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"TRANSPORT TIMEOUT after {opts.timeout}s", file=sys.stderr)
        return 2

    responses = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            responses[msg["id"]] = msg

    if 1 not in responses:
        print("TRANSPORT FAILURE: no initialize response", file=sys.stderr)
        print(err[-2000:], file=sys.stderr)
        return 2

    reply = responses.get(2)
    if reply is None:
        print("TRANSPORT FAILURE: no response to the call", file=sys.stderr)
        print(err[-2000:], file=sys.stderr)
        return 2

    if opts.raw or "error" in reply:
        print(json.dumps(reply, indent=2))
        return 1 if "error" in reply else 0

    result = reply.get("result", {})
    if opts.list:
        print(json.dumps([t["name"] for t in result.get("tools", [])], indent=2))
        return 0

    # FastMCP returns the tool payload as a text content block; unwrap it so the
    # caller sees the tool's own envelope rather than the transport wrapper.
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                print(json.dumps(json.loads(block["text"]), indent=2))
            except json.JSONDecodeError:
                print(block["text"])
    if result.get("isError"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
