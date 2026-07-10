"""Plugin-root resolution.

The Python MCP server can run from three different locations:

1. From inside the cloned plugin repo (legacy: `python -m chameleon_mcp.server`).
2. From the Claude Code plugin cache, with `.venv` built via `uv sync`.
3. From `uvx`'s isolated venv, with the Python code living in
   `~/.cache/uv/.../site-packages/chameleon_mcp/`.

In case (3), `Path(__file__).parent.parent.parent` no longer points to the
plugin root, so the extractors can't find sibling artifacts like
`scripts/ts_dump.mjs` or `mcp/node_modules/typescript`. Claude Code sets
`CLAUDE_PLUGIN_ROOT` when it spawns hooks and MCP servers; we use that as
the authoritative source and fall back to file-relative resolution only
for legacy invocations.
"""

from __future__ import annotations

import os
from pathlib import Path


def secure_chmod(path: Path, mode: int) -> bool:
    """Apply a POSIX permission mode, no-op on platforms without POSIX semantics.

    Returns True when the mode was applied, False when it was skipped (non-POSIX
    platform) or failed (OSError). The boolean makes the platform difference
    explicit to callers instead of silently masking it: on Windows the POSIX
    `mode` is largely meaningless and `os.chmod` cannot enforce 0700/0600-style
    restrictions, so security-sensitive dirs there rely on filesystem ACLs, not
    this call. Never raises, so the create/secure paths stay fail-open.
    """
    if os.name != "posix":
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def plugin_root() -> Path:
    """Return the absolute path to the chameleon plugin's install directory.

    Resolution order:
    1. `CLAUDE_PLUGIN_ROOT` — set by Claude Code when spawning MCP / hooks.
    2. `CHAMELEON_PLUGIN_ROOT` — test override.
    3. File-relative fallback: assumes this module lives at
       `<plugin_root>/mcp/chameleon_mcp/plugin_paths.py`.
    """
    for var in ("CLAUDE_PLUGIN_ROOT", "CHAMELEON_PLUGIN_ROOT"):
        value = os.environ.get(var)
        if value and "${" not in value:
            return Path(value).resolve()

    here = Path(__file__).resolve()
    return here.parent.parent.parent


def plugin_data_dir() -> Path:
    """Return the per-user chameleon plugin data directory.

    Override with CHAMELEON_PLUGIN_DATA for testing.
    Default: ~/.local/share/chameleon
    """
    override = os.environ.get("CHAMELEON_PLUGIN_DATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "chameleon"


def ensure_plugin_data_dir() -> Path:
    """Return the per-user data dir, created and locked to mode 0700.

    The dir holds per-user secrets (HMAC key, trust records, drift/index DBs).
    0700 on the root blocks other local users from traversing into any child,
    regardless of each child's own mode. The chmod is idempotent so an upgrade
    tightens a previously world-readable dir.
    """
    d = plugin_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    secure_chmod(d, 0o700)
    return d
