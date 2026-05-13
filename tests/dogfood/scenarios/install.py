"""Phase 0: install + verify scenarios."""
from __future__ import annotations

import os
import sys

from tests.dogfood.scenario import Result, Scenario


def _run_install_verify(ctx) -> Result:
    plugin_root = ctx.plugin_root

    failures: list[str] = []

    # .claude-plugin/plugin.json exists
    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    if not plugin_json.is_file():
        failures.append(f"missing {plugin_json}")

    # mcp/.venv/bin/python exists
    venv_python = plugin_root / "mcp" / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        failures.append(f"missing {venv_python}")

    # All 4 hook scripts present + executable
    for hook_name in ("session-start", "preflight-and-advise", "posttool-recorder", "callout-detector"):
        hook_path = plugin_root / "hooks" / hook_name
        if not hook_path.is_file():
            failures.append(f"missing hook: {hook_name}")
        elif not os.access(hook_path, os.X_OK):
            failures.append(f"hook not executable: {hook_name}")

    # chameleon_mcp/__init__.py importable from mcp/
    init_py = plugin_root / "mcp" / "chameleon_mcp" / "__init__.py"
    if not init_py.is_file():
        failures.append("missing mcp/chameleon_mcp/__init__.py")
    else:
        mcp_dir = str(plugin_root / "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        try:
            import importlib
            importlib.import_module("chameleon_mcp")
        except ImportError as exc:
            failures.append(f"chameleon_mcp not importable: {exc}")

    if failures:
        return Result(status="FAIL", notes="; ".join(failures))
    return Result(status="PASS")


SCENARIOS = [
    Scenario(
        id="0.1",
        name="install + verify",
        family="install",
        needs_claude=False,
        cost="free",
        requires=[],
        run=_run_install_verify,
    ),
]
