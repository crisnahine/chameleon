"""Shared scenario helpers."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def fresh_fixture_copy(plugin_root: Path, fixture_subpath: str) -> Path:
    """Copy a checked-in fixture to a fresh tmpdir; caller is responsible for cleanup."""
    src = plugin_root / fixture_subpath
    dst = Path(tempfile.mkdtemp(prefix="dogfood_fix_"))
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def run_real_claude(
    *,
    repo: Path,
    plugin_root: Path,
    prompt: str,
    allowed_tools: str,
    max_turns: int = 8,
    model: str = "sonnet",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke `claude -p` and return parsed stream-json events as a dict.

    Returns dict with keys: events (list), cost_usd (float), permission_denials,
    pretool_advisories (list of strings -- additionalContext blobs from PreToolUse:Edit).
    """
    proc = subprocess.run(
        ["claude", "-p", prompt,
         "--plugin-dir", str(plugin_root),
         "--output-format", "stream-json",
         "--include-hook-events",
         "--max-turns", str(max_turns),
         "--verbose",
         "--model", model,
         "--permission-mode", "acceptEdits",
         "--allowedTools", allowed_tools],
        cwd=str(repo),
        capture_output=True, text=True, timeout=300,
        env=env,
    )
    events = []
    pretool_advisories = []
    cost_usd = 0.0
    permission_denials: list = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(obj)
        if obj.get("type") == "system" and obj.get("subtype") == "hook_response":
            hook = obj.get("hook_name", "")
            if hook.startswith("PreToolUse"):
                stdout = obj.get("stdout", "")
                if "additionalContext" in stdout:
                    pretool_advisories.append(stdout)
        if obj.get("type") == "result":
            cost_usd = obj.get("total_cost_usd", 0.0)
            permission_denials = obj.get("permission_denials", [])
    return {
        "events": events,
        "pretool_advisories": pretool_advisories,
        "cost_usd": cost_usd,
        "permission_denials": permission_denials,
        "exit_code": proc.returncode,
        "stderr_tail": proc.stderr[-400:],
    }


def ensure_repo_trusted(repo: Path) -> None:
    """Best-effort bootstrap + trust on a real repo."""
    from chameleon_mcp.tools import bootstrap_repo, trust_profile
    if not (repo / ".chameleon" / "COMMITTED").exists():
        bootstrap_repo(str(repo))
    trust_profile(str(repo), repo.name)
