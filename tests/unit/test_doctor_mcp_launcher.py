"""The doctor `mcp_server_launcher` check reports whether `uvx` resolves, so a
green report never hides a dead MCP surface.

The MCP server is launched as `uvx --from ${CLAUDE_PLUGIN_ROOT}/mcp chameleon-mcp`
(.mcp.json). That is a hard dependency separate from the hook interpreter ladder:
the hooks can resolve a Python via the bundled venv or a version-named python3.x
with no uv present, so `hook_interpreter_deps` can pass while every MCP tool
(/chameleon-init, refresh, status, codebase queries) is unavailable. This check
closes that gap."""

from __future__ import annotations

import shutil

from chameleon_mcp import tools


def _launcher_check(monkeypatch, *, uvx: bool, uv: bool) -> dict:
    """Run doctor with uvx/uv presence forced, return the launcher check."""
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "uvx":
            return "/fake/bin/uvx" if uvx else None
        if name == "uv":
            return "/fake/bin/uv" if uv else None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    checks = tools.doctor().get("data", {}).get("checks", [])
    check = next((c for c in checks if c.get("name") == "mcp_server_launcher"), None)
    assert check is not None, "doctor did not emit the mcp_server_launcher check"
    return check


def test_launcher_ok_when_uvx_present(monkeypatch):
    check = _launcher_check(monkeypatch, uvx=True, uv=True)
    assert check["status"] == "ok"
    assert check["detail"] == "/fake/bin/uvx"


def test_launcher_warn_when_only_uv_present(monkeypatch):
    check = _launcher_check(monkeypatch, uvx=False, uv=True)
    assert check["status"] == "warn"
    assert "uvx" in check["detail"]


def test_launcher_error_when_neither_present(monkeypatch):
    check = _launcher_check(monkeypatch, uvx=False, uv=False)
    assert check["status"] == "error"
    detail = check["detail"].lower()
    assert "uvx" in detail and "uv" in detail
    # actionable: names the install source
    assert "astral.sh" in detail


def test_launcher_error_flips_overall(monkeypatch):
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name in ("uvx", "uv"):
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    env = tools.doctor().get("data", {})
    assert env.get("overall") == "error"
