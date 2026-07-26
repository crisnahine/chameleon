"""Peer-plugin detection.

Chameleon composes with the superpowers plugin when both are installed: the
SessionStart digest then carries a routing block stating which skill owns
which phase of the work. Nothing here reads superpowers' own content -- a peer
plugin's files are third-party data, so detection is existence checks only and
never renders a byte of what it finds.

Detection runs on the SessionStart path, whose wrapper caps the whole Python
emission at 3 seconds (`plugin/hooks/session-start`), so the lookup is
bounded: one small JSON read against a known filename, never a recursive walk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Registry keys are "<name>@<marketplace>". The name is matched exactly so the
# sibling family plugins (superpowers-dev, superpowers-chrome, superpowers-lab,
# superpowers-developing-for-claude-code) never satisfy detection -- only core
# ships the skills the routing block points at.
_PEER_NAME = "superpowers"

# The bootstrap skill, not merely the plugin directory: a cache entry without it
# cannot auto-trigger anything, so routing to it would be a dead pointer.
_PEER_MARKER = ("skills", "using-superpowers", "SKILL.md")

# Directory entries listed per level before giving up. A real cache holds a
# handful of marketplaces and versions; the cap keeps a pathological cache from
# spending a SessionStart budget the wrapper caps at 3 seconds total.
_MAX_DIR_ENTRIES = 64


def _installed_from_registry() -> bool:
    """True when Claude Code's own plugin registry records superpowers on disk.

    Reads `~/.claude/plugins/installed_plugins.json`, the harness's own
    artifact, and confirms the recorded `installPath` still exists -- a stale
    record for an uninstalled plugin is not an install. Returns False on a
    missing, unreadable, or malformed registry.
    """
    registry = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not registry.is_file():
        return False
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except Exception:
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return False
    for key, entries in plugins.items():
        if not isinstance(key, str) or key.split("@", 1)[0] != _PEER_NAME:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            install_path = entry.get("installPath")
            if isinstance(install_path, str) and install_path:
                if Path(install_path).is_dir():
                    return True
    return False


def _bounded_listing(directory: Path) -> list[Path]:
    """Up to _MAX_DIR_ENTRIES children, in deterministic order; [] on any error."""
    try:
        return sorted(directory.iterdir())[:_MAX_DIR_ENTRIES]
    except OSError:
        return []


def _installed_from_cache() -> bool:
    """True when the plugin cache holds a superpowers install beside chameleon's.

    The registry is user-scoped and absent under some install shapes (a
    project-scoped install, a local marketplace, a dev checkout), so this walks
    the cache root chameleon itself was installed into, whose layout is
    ``<cache>/<marketplace>/<plugin>/<version>/``.

    Deliberately reads the environment rather than calling
    ``plugin_paths.plugin_root()``: that helper falls back to file-relative
    resolution, which would silently probe the real user cache from a test or
    a legacy checkout invocation. With no plugin root in the environment there
    is no cache to walk, and the answer is "not installed".

    Two shallow listings, both capped -- never a recursive walk, because this
    runs inside the SessionStart 3-second wrapper budget.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CHAMELEON_PLUGIN_ROOT")
    if not root or "${" in root:
        return False
    parents = Path(root).parents
    # <version>/<plugin>/<marketplace>/<cache> -- fewer parents than that means
    # this is not a marketplace-cache install shape.
    if len(parents) < 3:
        return False
    cache_root = parents[2]
    for marketplace in _bounded_listing(cache_root):
        peer = marketplace / _PEER_NAME
        if not peer.is_dir():
            continue
        for version in _bounded_listing(peer):
            if version.joinpath(*_PEER_MARKER).is_file():
                return True
    return False


def superpowers_installed() -> bool:
    """True when the superpowers core plugin is installed and present on disk.

    Existence checks only -- no byte of the peer plugin's content is read or
    rendered, so a peer install can never inject text into chameleon's own
    context blocks.

    Default-ON with a kill switch (``CHAMELEON_PEER_ROUTING=0``): local file
    reads only, no network and no repo-code execution. Fails CLOSED on any
    error -- a detection failure is indistinguishable from an absent plugin,
    and chameleon then behaves exactly as it does when superpowers is absent.
    """
    if os.environ.get("CHAMELEON_PEER_ROUTING") == "0":
        return False
    try:
        return _installed_from_registry() or _installed_from_cache()
    except Exception:
        return False
