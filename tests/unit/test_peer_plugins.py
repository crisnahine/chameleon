"""Peer-plugin detection behind the SessionStart routing block.

``superpowers_installed()`` decides whether the digest carries the chameleon +
superpowers routing block. It reads Claude Code's own plugin registry and,
failing that, the plugin cache chameleon was installed into -- both by
existence check only, never by reading the peer plugin's content. Every seam
fails CLOSED, so an absent, unreadable, or malformed registry reads exactly
like "superpowers is not installed" and chameleon behaves as it does alone.

Both rungs are pinned in every test: a bare assertion would otherwise consult
the developer's real ~/.claude and pass or fail by machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.peer_plugins import superpowers_installed


def _write_registry(home: Path, payload: dict) -> None:
    """Materialize ~/.claude/plugins/installed_plugins.json under a fake home."""
    d = home / ".claude" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text(json.dumps(payload), encoding="utf-8")


def _entry(install_path: Path) -> dict:
    """One registry entry list, shaped like Claude Code's own v2 records."""
    return [{"scope": "user", "installPath": str(install_path), "version": "6.2.0"}]


def _empty_cache_root(tmp_path: Path) -> Path:
    """A plugin root whose cache holds chameleon but no superpowers.

    Shape: <cache>/<marketplace>/chameleon/<version> -- the value Claude Code
    puts in CLAUDE_PLUGIN_ROOT. Rung 2 walks up three parents from here.
    """
    root = tmp_path / "cache" / "some-marketplace" / "chameleon" / "4.5.15"
    root.mkdir(parents=True)
    return root


def _cache_root_with_superpowers(tmp_path: Path, *, with_skill: bool = True) -> Path:
    """A plugin root whose cache holds BOTH chameleon and a superpowers install.

    Mirrors the real layout: the returned path is chameleon's own version dir,
    and superpowers sits at <cache>/<marketplace>/superpowers/<version>/.
    """
    cache = tmp_path / "cache"
    chameleon_root = cache / "some-marketplace" / "chameleon" / "4.5.15"
    chameleon_root.mkdir(parents=True)
    sp = cache / "superpowers-marketplace" / "superpowers" / "6.2.0"
    if with_skill:
        (sp / "skills" / "using-superpowers").mkdir(parents=True)
        (sp / "skills" / "using-superpowers" / "SKILL.md").write_text("x", encoding="utf-8")
    else:
        sp.mkdir(parents=True)
    return chameleon_root


def test_registry_hit_when_superpowers_installed_and_on_disk(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    installed.mkdir()
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_family_plugins_alone_do_not_count(tmp_path, monkeypatch):
    """superpowers-dev and friends do not ship the skills the block routes to."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp-dev"
    installed.mkdir()
    _write_registry(
        home,
        {
            "version": 2,
            "plugins": {
                "superpowers-dev@mkt": _entry(installed),
                "superpowers-chrome@mkt": _entry(installed),
                "superpowers-lab@mkt": _entry(installed),
            },
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_kill_switch_suppresses_a_real_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    installed.mkdir()
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))
    monkeypatch.setenv("CHAMELEON_PEER_ROUTING", "0")

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_default_on_with_env_unset(tmp_path, monkeypatch):
    """The contract is default-ON: never asserted by setting the var to '1'."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    installed.mkdir()
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))
    monkeypatch.delenv("CHAMELEON_PEER_ROUTING", raising=False)

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_cache_rung_finds_superpowers_beside_chameleon(tmp_path, monkeypatch):
    """The registry is user-scoped and absent under some install shapes."""
    home = tmp_path / "home"
    home.mkdir()  # no registry file at all
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_cache_rung_requires_the_bootstrap_skill(tmp_path, monkeypatch):
    """A bare superpowers directory without using-superpowers is not an install."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(
        "CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path, with_skill=False))
    )

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_absent_registry_and_empty_cache_reads_as_not_installed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_malformed_registry_fails_closed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    d = home / ".claude" / "plugins"
    d.mkdir(parents=True)
    (d / "installed_plugins.json").write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_registry_with_unexpected_shape_fails_closed(tmp_path, monkeypatch):
    """A schema change upstream must read as 'absent', never crash the hook."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    for payload in ({"plugins": []}, {"plugins": {"superpowers@m": "nope"}}, {}, []):
        _write_registry(home, payload) if isinstance(payload, dict) else (
            home / ".claude" / "plugins" / "installed_plugins.json"
        ).write_text(json.dumps(payload), encoding="utf-8")
        with patch("pathlib.Path.home", return_value=home):
            assert superpowers_installed() is False, payload


def test_recorded_but_uninstalled_path_does_not_count(tmp_path, monkeypatch):
    """A stale registry row for a removed plugin is not an install."""
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(
        home,
        {"version": 2, "plugins": {"superpowers@mkt": _entry(tmp_path / "gone")}},
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_unset_plugin_root_does_not_crash_the_cache_rung(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CHAMELEON_PLUGIN_ROOT", raising=False)

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_pathological_cache_stays_bounded(tmp_path, monkeypatch):
    """A cache with many marketplaces must not spend the 3s SessionStart budget."""
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "cache"
    chameleon_root = cache / "mkt-0" / "chameleon" / "4.5.15"
    chameleon_root.mkdir(parents=True)
    for i in range(200):
        (cache / f"mkt-{i}" / "notsuperpowers" / "1.0.0").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(chameleon_root))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False
