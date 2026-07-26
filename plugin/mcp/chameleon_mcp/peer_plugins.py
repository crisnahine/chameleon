"""Peer-plugin detection.

Chameleon composes with the superpowers plugin when both are installed: the
SessionStart digest then carries a routing block stating which skill owns
which phase of the work. Nothing here reads superpowers' own content -- a peer
plugin's files are third-party data, so detection is existence checks only and
never renders a byte of what it finds.

Detection runs on the SessionStart path, whose wrapper caps the whole Python
emission at 3 seconds (`plugin/hooks/session-start`) and loses the ENTIRE
injection on overrun, so the whole lookup stays cheap: two small JSON reads against
known filenames (the plugin registry and the settings file, the latter read once and
threaded to both rungs), then at most two shallow directory levels against a known
path shape. Nothing here ever walks a tree recursively.

The question answered is "is superpowers going to be active in this session",
not merely "do its files exist" -- a plugin present on disk but switched off in
settings is not composing with anything, and routing the model to skills that
never load is worse than staying quiet.
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

# The plugin cache is the only layout rung 2 understands:
# <...>/cache/<marketplace>/<plugin>/<version>. Requiring the literal directory
# name keeps a source-checkout or uvx invocation -- where CLAUDE_PLUGIN_ROOT's
# third parent is an arbitrary user directory -- from being listed at all.
_CACHE_DIR_NAME = "cache"

# Registry files run to a few KB. Anything past this is not a registry, and
# is_file() follows symlinks, so an unbounded read_text() inside a 3-second
# wrapper is a needless foot-gun.
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024


def _max_dir_entries() -> int:
    """Directory entries CONSIDERED per level of the cache scan.

    The listing itself is not bounded -- sorting for deterministic order
    requires draining the iterator first -- so this caps the stat calls fanned
    out per level, not the read of the directory. Past this many, detection
    gives up and reads as "not installed", which is the safe direction.
    """
    try:
        from chameleon_mcp._thresholds import threshold_int

        # threshold_int does not floor operator overrides, and a negative value
        # would slice as "all but the last N" -- inverting the cap instead of
        # tightening it. Clamp so an override can only ever consider fewer.
        return max(0, threshold_int("PEER_CACHE_MAX_DIR_ENTRIES"))
    except Exception:
        return 64


def _config_dir() -> Path:
    """The Claude Code config directory actually in force for this session.

    ``CLAUDE_CONFIG_DIR`` relocates it, and the CLI honors it -- a session under
    a relocated config answered from the developer's real ``~/.claude`` gets the
    wrong answer in BOTH directions: a sandbox with no superpowers inherits the
    block from the default config, and a user whose only registry lives in the
    relocated dir is vetoed by a ``~/.claude`` that has none. The same non-empty
    / unexpanded-placeholder guard the cache rung uses on its own env reads.
    """
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    if value and "${" not in value:
        return Path(value)
    return Path.home() / ".claude"


def _read_json(path: Path, max_bytes: int) -> object | None:
    """Parse `path` as JSON, or None when it is absent, oversize, or malformed."""
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _disabled_plugin_keys() -> frozenset[str]:
    """The plugin keys settings explicitly switch OFF.

    The config directory's `settings.json` (`~/.claude` unless
    `CLAUDE_CONFIG_DIR` relocates it) carries `enabledPlugins`, keyed the same
    way as the registry ("<name>@<marketplace>"). A plugin set to false stays
    installed on disk while its skills never load, so treating it as present
    would route the model to skills that are not in the session.

    Only an explicit false lands in the set: an unreadable settings file, a
    missing key, or a plugin enabled per-project rather than per-user all read
    as "not disabled", so this can only ever suppress detection, never cause it.

    Read ONCE per detection and threaded to both rungs. Re-reading per candidate
    would parse this file up to sixty-four times on a cache scan, inside a hook
    the wrapper hard-kills at three seconds.

    Scope is the USER-level settings file only. A plugin disabled for one
    project via that project's .claude/settings.json still reads as enabled
    here, so the routing block would appear in a session where superpowers does
    not load. Closing that needs the project root threaded in plus the full
    settings precedence chain; the boundary is stated rather than half-covered.
    """
    data = _read_json(_config_dir() / "settings.json", _MAX_REGISTRY_BYTES)
    if not isinstance(data, dict):
        return frozenset()
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return frozenset()
    return frozenset(k for k, v in enabled.items() if isinstance(k, str) and v is False)


def _registry_verdict(disabled: frozenset[str]) -> bool | None:
    """Whether Claude Code's registry records superpowers installed on disk.

    Returns True (recorded, the recorded install still carries the bootstrap
    skill, and not disabled), None (no information: the file is absent,
    oversize, malformed, or names superpowers in a shape this code cannot
    parse), or False for every other parsed outcome -- the registry omits
    superpowers, OR lists it at a path that no longer holds the bootstrap skill,
    OR lists it disabled, OR lists it with no entries.

    Both rungs test the SAME marker. Accepting a bare directory here while the
    cache rung demanded the bootstrap skill meant the two disagreed about what
    counts as superpowers, and this is the rung that answers on every ordinary
    install -- so any plugin named `superpowers` in any marketplace satisfied it.

    False is deliberately wider than "not listed". Each of those cases is a
    readable registry saying superpowers will not load, which a leftover cache
    directory must not overturn; only a registry that could not be consulted at
    all lets the cache rung speak. The practical asymmetry: a stale absolute
    installPath (a renamed home directory, a relocated install) reads as False
    and suppresses the block even though the cache holds a live copy. That is
    the fail-closed direction and intentional, but it is not obvious.
    """
    data = _read_json(_config_dir() / "plugins" / "installed_plugins.json", _MAX_REGISTRY_BYTES)
    if not isinstance(data, dict):
        return None
    # A v2 registry nests under "plugins"; tolerate a flat top-level map too,
    # matching the repo's other reader of this file (scripts/prune-plugin-cache.sh).
    plugins = data.get("plugins", data)
    if not isinstance(plugins, dict):
        return None
    unreadable = False
    for key, entries in plugins.items():
        if not isinstance(key, str) or key.split("@", 1)[0] != _PEER_NAME:
            continue
        if not isinstance(entries, list):
            # The registry names superpowers in a shape this code cannot read
            # (a future schema storing the entry as a dict, say). That is not
            # the same as "the registry does not list it", so it must not read
            # as an authoritative negative -- let the cache rung speak instead.
            unreadable = True
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                unreadable = True
                continue
            install_path = entry.get("installPath")
            if isinstance(install_path, str) and install_path:
                # The same bootstrap-skill marker rung 2 requires. Rung 1 used to
                # accept any directory that merely existed, so the two rungs
                # disagreed about what counts as superpowers -- and rung 1 is the
                # one that answers on every normal install. One stat either way.
                if Path(install_path).joinpath(*_PEER_MARKER).is_file() and key not in disabled:
                    return True
            else:
                unreadable = True
    return None if unreadable else False


def _bounded_listing(directory: Path) -> list[Path]:
    """Up to the per-level cap of children, deterministic order; [] on any error."""
    try:
        return sorted(directory.iterdir())[: _max_dir_entries()]
    except OSError:
        return []


def _installed_from_cache(disabled: frozenset[str]) -> bool:
    """True when the plugin cache holds an enabled superpowers install.

    Consulted only when the registry could not answer, since a cached directory
    is weaker evidence than the registry: it survives an uninstall.

    Deliberately reads the environment rather than calling
    ``plugin_paths.plugin_root()``: that helper falls back to file-relative
    resolution, which would silently probe the real user cache from a test or a
    legacy checkout invocation. With no plugin root in the environment there is
    no cache to walk, and the answer is "not installed".

    Two shallow listings, both capped, and only under a directory actually named
    `cache` -- never a recursive walk, because this runs inside the SessionStart
    3-second wrapper budget.
    """
    for var in ("CLAUDE_PLUGIN_ROOT", "CHAMELEON_PLUGIN_ROOT"):
        root = os.environ.get(var)
        if not root or "${" in root:
            continue
        parents = Path(root).parents
        # <version>/<plugin>/<marketplace>/<cache> -- fewer parents than that,
        # or a third parent not named `cache`, is not a marketplace install.
        if len(parents) < 3 or parents[2].name != _CACHE_DIR_NAME:
            continue
        for marketplace in _bounded_listing(parents[2]):
            peer = marketplace / _PEER_NAME
            if not peer.is_dir() or f"{_PEER_NAME}@{marketplace.name}" in disabled:
                continue
            for version in _bounded_listing(peer):
                if version.joinpath(*_PEER_MARKER).is_file():
                    return True
    return False


def superpowers_installed() -> bool:
    """True when the superpowers core plugin will be active in this session.

    Existence checks only -- no byte of the peer plugin's content is read or
    rendered, so a peer install can never inject text into chameleon's own
    context blocks. Local file reads throughout: no network, no repo-code
    execution.

    Answers only the factual question its name asks. Whether a caller ACTS on
    the answer is that caller's policy -- the routing block's kill switch is
    checked where the block is assembled, so a later consumer cannot silently
    inherit an unrelated feature's off-switch.

    Fails CLOSED on any error: a detection failure is indistinguishable from an
    absent plugin, so chameleon behaves exactly as it does without superpowers.
    """
    try:
        disabled = _disabled_plugin_keys()
        verdict = _registry_verdict(disabled)
        if verdict is not None:
            return verdict
        return _installed_from_cache(disabled)
    except Exception:
        return False
