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

import pytest

from chameleon_mcp.peer_plugins import _max_dir_entries, superpowers_installed


@pytest.fixture(autouse=True)
def _clear_peer_routing(monkeypatch):
    """Detection is a fact, not a policy: the routing kill switch is checked at
    the call site, so a developer exporting it must not steer these results."""
    monkeypatch.delenv("CHAMELEON_PEER_ROUTING", raising=False)


def _write_registry(home: Path, payload: dict) -> None:
    """Materialize ~/.claude/plugins/installed_plugins.json under a fake home."""
    d = home / ".claude" / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text(json.dumps(payload), encoding="utf-8")


def _plant_install(path: Path) -> Path:
    """A recorded install directory that carries the bootstrap skill.

    Rung 1 tests the same ``skills/using-superpowers/SKILL.md`` marker rung 2
    does, so a bare directory is not an install. A fixture that only mkdir()s
    would be asserting about a plugin chameleon deliberately does not recognize.
    """
    skill_dir = path / "skills" / "using-superpowers"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")
    return path


def _entry(install_path: Path) -> dict:
    """One registry entry list, shaped like Claude Code's own v2 records."""
    return [{"scope": "user", "installPath": str(install_path), "version": "6.2.0"}]


def _empty_cache_root(tmp_path: Path) -> Path:
    """A plugin root whose cache holds chameleon but no superpowers.

    Shape: <cache>/<marketplace>/chameleon/<version> -- the value Claude Code
    puts in CLAUDE_PLUGIN_ROOT. Rung 2 walks up three parents from here.
    """
    root = tmp_path / "cache" / "some-marketplace" / "chameleon" / "0.0.0-fixture"
    root.mkdir(parents=True)
    return root


def _plant_superpowers(marketplace: Path, version: str = "6.2.0") -> None:
    """Install superpowers under one marketplace dir, bootstrap skill included."""
    skill_dir = marketplace / "superpowers" / version / "skills" / "using-superpowers"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("x", encoding="utf-8")


def _cache_root_with_superpowers(tmp_path: Path, *, with_skill: bool = True) -> Path:
    """A plugin root whose cache holds BOTH chameleon and a superpowers install.

    Mirrors the real layout: the returned path is chameleon's own version dir,
    and superpowers sits at <cache>/<marketplace>/superpowers/<version>/.
    """
    cache = tmp_path / "cache"
    chameleon_root = cache / "some-marketplace" / "chameleon" / "0.0.0-fixture"
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
    _plant_install(installed)
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_family_plugins_alone_do_not_count(tmp_path, monkeypatch):
    """superpowers-dev and friends do not ship the skills the block routes to."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp-dev"
    _plant_install(installed)
    _write_registry(
        home,
        {
            "version": 2,
            "plugins": {
                "superpowers-dev@mkt": _entry(installed),
                "superpowers-chrome@mkt": _entry(installed),
                "superpowers-lab@mkt": _entry(installed),
                "superpowers-developing-for-claude-code@mkt": _entry(installed),
            },
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_kill_switch_does_not_reach_the_detector(tmp_path, monkeypatch):
    """The switch gates the routing block, not the fact. A caller asking
    whether superpowers is installed gets the truth regardless."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    _plant_install(installed)
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))
    monkeypatch.setenv("CHAMELEON_PEER_ROUTING", "0")

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_flat_top_level_registry_is_tolerated(tmp_path, monkeypatch):
    """scripts/prune-plugin-cache.sh reads this file as data.get('plugins', data);
    an install shape without the v2 wrapper must resolve the same way here."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    _plant_install(installed)
    _write_registry(home, {"superpowers@mkt": _entry(installed)})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

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

    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    for payload in ({"plugins": []}, {"plugins": {"superpowers@m": "nope"}}, {}, []):
        if isinstance(payload, dict):
            _write_registry(home, payload)
        else:
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps(payload), encoding="utf-8")
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


def test_cache_scan_gives_up_past_the_entry_cap(tmp_path, monkeypatch):
    """The cap is load-bearing, not decorative: an install planted beyond it is
    not found. Asserting only False on a large cache would pass with no cap at
    all, which is what the previous version of this test did.

    Giving up reads as "not installed", the safe direction -- a missing routing
    block costs guidance, where an overrun SessionStart budget costs the whole
    injection.
    """
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "cache"
    # Zero-padded so lexicographic order matches numeric order.
    chameleon_root = cache / "mkt-000" / "chameleon" / "0.0.0-fixture"
    chameleon_root.mkdir(parents=True)
    beyond = _max_dir_entries() + 20
    for i in range(beyond + 1):
        (cache / f"mkt-{i:03d}").mkdir(exist_ok=True)
    _plant_superpowers(cache / f"mkt-{beyond:03d}")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(chameleon_root))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False

    # Control: the identical install INSIDE the cap is found, proving the miss
    # above is the cap doing its job and not a broken fixture.
    _plant_superpowers(cache / "mkt-001")
    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def _write_settings(home: Path, enabled: dict) -> None:
    """Materialize ~/.claude/settings.json with an enabledPlugins map."""
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")


def test_authoritative_registry_negative_beats_a_stale_cache(tmp_path, monkeypatch):
    """Uninstalling drops the registry row but can leave the cache directory.
    A registry that parsed and omits superpowers is an answer, not a gap, so
    the cache rung must not overturn it -- otherwise the block would keep
    routing to skills the user removed."""
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(home, {"version": 2, "plugins": {"chameleon@mkt": _entry(tmp_path)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_disabled_in_settings_reads_as_absent(tmp_path, monkeypatch):
    """A plugin switched off in settings stays on disk but loads no skills."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    _plant_install(installed)
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    _write_settings(home, {"superpowers@mkt": False})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_enabled_true_and_missing_key_both_count_as_enabled(tmp_path, monkeypatch):
    """Only an explicit false suppresses: absence is not a negative."""
    home = tmp_path / "home"
    home.mkdir()
    installed = tmp_path / "sp"
    _plant_install(installed)
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    for enabled in ({"superpowers@mkt": True}, {"other@mkt": False}, {}):
        _write_settings(home, enabled)
        with patch("pathlib.Path.home", return_value=home):
            assert superpowers_installed() is True, enabled


def test_cache_rung_honors_the_disable_switch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()  # no registry, so the cache rung speaks
    _write_settings(home, {"superpowers@superpowers-marketplace": False})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_cache_rung_ignores_a_non_cache_layout(tmp_path, monkeypatch):
    """A source checkout or uvx invocation puts an arbitrary user directory at
    the third parent. Listing it would be both wasteful and a false-positive
    surface, so the scan requires a directory literally named `cache`."""
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "src" / "notcache" / "chameleon" / "0.0.0-fixture"
    fake.mkdir(parents=True)
    _plant_superpowers(tmp_path / "src" / "notcache")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(fake))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_cache_rung_rejects_family_plugins(tmp_path, monkeypatch):
    """AC2 holds on both rungs, not just the registry."""
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "cache"
    chameleon_root = cache / "mkt" / "chameleon" / "0.0.0-fixture"
    chameleon_root.mkdir(parents=True)
    for name in ("superpowers-dev", "superpowers-chrome", "superpowers-developing-for-claude-code"):
        skill = cache / "mkt" / name / "1.0.0" / "skills" / "using-superpowers"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(chameleon_root))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_oversize_registry_is_not_read(tmp_path, monkeypatch):
    """is_file() follows symlinks, so an unbounded read inside a 3s wrapper is a
    foot-gun. Past the cap the file is skipped, not parsed."""
    from chameleon_mcp.peer_plugins import _MAX_REGISTRY_BYTES

    home = tmp_path / "home"
    d = home / ".claude" / "plugins"
    d.mkdir(parents=True)
    payload = {"version": 2, "plugins": {"superpowers@mkt": _entry(tmp_path)}}
    blob = json.dumps(payload) + " " * (_MAX_REGISTRY_BYTES + 1)
    (d / "installed_plugins.json").write_text(blob, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_unreadable_entry_shape_defers_to_the_cache_rung(tmp_path, monkeypatch):
    """A registry that NAMES superpowers in a shape this code cannot parse is
    not an authoritative negative -- only a registry that parsed and genuinely
    omits it is. Otherwise a future schema change would permanently disable
    detection while a live install sits in the cache."""
    home = tmp_path / "home"
    home.mkdir()
    # entries as a dict rather than the v2 one-element list
    _write_registry(home, {"version": 3, "plugins": {"superpowers@mkt": {"installPath": "/x"}}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True


def test_parsed_registry_omitting_superpowers_stays_authoritative(tmp_path, monkeypatch):
    """The contrast case: a readable registry that simply lacks the key must
    still beat a stale cache directory."""
    home = tmp_path / "home"
    home.mkdir()
    _write_registry(home, {"version": 2, "plugins": {"chameleon@mkt": _entry(tmp_path)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_cache_root_with_superpowers(tmp_path)))

    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_a_recorded_directory_without_the_bootstrap_skill_is_not_an_install(tmp_path, monkeypatch):
    """Rung 1 used to accept any directory that merely existed while rung 2
    demanded the bootstrap skill, so the two rungs disagreed about what counts
    as superpowers -- and rung 1 is the one that answers on every ordinary
    install. Any plugin named `superpowers` in any marketplace satisfied it."""
    home = tmp_path / "home"
    home.mkdir()
    bare = tmp_path / "sp"
    bare.mkdir()  # deliberately NOT _plant_install: no skills/using-superpowers
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(bare)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))
    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is False


def test_config_dir_override_is_honored(tmp_path, monkeypatch):
    """CLAUDE_CONFIG_DIR relocates the whole config directory and the CLI obeys
    it, so answering from ~/.claude is wrong in both directions: a sandbox with
    no superpowers would inherit the real user's answer, and a user whose only
    registry lives in the relocated dir would be vetoed by a ~/.claude that has
    none. `real_home` here holds NO registry, so a hit can only come from the
    override."""
    real_home = tmp_path / "home"
    real_home.mkdir()
    relocated = tmp_path / "elsewhere"
    (relocated / "plugins").mkdir(parents=True)
    installed = _plant_install(tmp_path / "sp")
    (relocated / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))
    with patch("pathlib.Path.home", return_value=real_home):
        assert superpowers_installed() is False  # precondition: nothing in ~/.claude
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))
        assert superpowers_installed() is True


def test_an_unexpanded_config_dir_placeholder_falls_back_to_home(tmp_path, monkeypatch):
    """A shell that never expanded ${...} must not send detection to a literal
    directory of that name -- the same guard the cache rung applies to its own
    environment reads."""
    home = tmp_path / "home"
    home.mkdir()
    installed = _plant_install(tmp_path / "sp")
    _write_registry(home, {"version": 2, "plugins": {"superpowers@mkt": _entry(installed)}})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_empty_cache_root(tmp_path)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "${HOME}/.claude")
    with patch("pathlib.Path.home", return_value=home):
        assert superpowers_installed() is True
