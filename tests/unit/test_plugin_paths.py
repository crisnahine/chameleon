"""Unit tests for chameleon_mcp.plugin_paths — root + data-dir resolution.

The module reads CLAUDE_PLUGIN_ROOT / CHAMELEON_PLUGIN_ROOT / CHAMELEON_PLUGIN_DATA
at call time (not import time), so the autouse fixture just scrubs those three env
vars before each test and lets each test set exactly what it needs. CHAMELEON_PLUGIN_DATA
is pointed at tmp_path by default for isolation, mirroring the project's no-conftest
convention; tests that exercise the *unset* default pop it explicitly.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import chameleon_mcp.plugin_paths as pp
from chameleon_mcp.plugin_paths import (
    ensure_plugin_data_dir,
    plugin_data_dir,
    plugin_root,
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch):
    """Scrub the three resolution env vars; default data dir to tmp_path."""
    for var in ("CLAUDE_PLUGIN_ROOT", "CHAMELEON_PLUGIN_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    yield


# ---------------------------------------------------------------------------
# plugin_root — env override precedence
# ---------------------------------------------------------------------------


class TestPluginRootOverrides:
    def test_claude_plugin_root_wins(self, tmp_path: Path, monkeypatch):
        """CLAUDE_PLUGIN_ROOT is authoritative when both override vars are set."""
        claude = tmp_path / "claude_root"
        chameleon = tmp_path / "chameleon_root"
        claude.mkdir()
        chameleon.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(claude))
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(chameleon))

        assert plugin_root() == claude.resolve()

    def test_chameleon_root_used_when_claude_unset(self, tmp_path: Path, monkeypatch):
        chameleon = tmp_path / "chameleon_only"
        chameleon.mkdir()
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(chameleon))

        assert plugin_root() == chameleon.resolve()

    def test_returns_path_instance(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "p"))
        result = plugin_root()
        assert isinstance(result, Path)
        assert result.is_absolute()


# ---------------------------------------------------------------------------
# plugin_root — falsy / placeholder skips
# ---------------------------------------------------------------------------


class TestPluginRootSkips:
    def test_empty_claude_falls_through_to_chameleon(self, tmp_path: Path, monkeypatch):
        """An empty CLAUDE_PLUGIN_ROOT is falsy and skipped."""
        chameleon = tmp_path / "real"
        chameleon.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(chameleon))

        assert plugin_root() == chameleon.resolve()

    def test_unexpanded_placeholder_skipped(self, tmp_path: Path, monkeypatch):
        """A literal '${CLAUDE_PLUGIN_ROOT}' (un-substituted) is rejected via '${' check."""
        chameleon = tmp_path / "fallback_plugin"
        chameleon.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "${CLAUDE_PLUGIN_ROOT}")
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(chameleon))

        assert plugin_root() == chameleon.resolve()

    def test_placeholder_embedded_in_path_skipped(self, tmp_path: Path, monkeypatch):
        """'${' anywhere in the value disqualifies it, even mid-string."""
        chameleon = tmp_path / "good"
        chameleon.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/opt/${VAR}/plugin")
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(chameleon))

        assert plugin_root() == chameleon.resolve()

    def test_both_placeholders_fall_back_to_file_relative(self, monkeypatch):
        """When both overrides are placeholders, file-relative fallback is used."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "${A}")
        monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", "x${B}y")

        expected = Path(pp.__file__).resolve().parent.parent.parent
        assert plugin_root() == expected


# ---------------------------------------------------------------------------
# plugin_root — file-relative fallback
# ---------------------------------------------------------------------------


class TestPluginRootFallback:
    def test_fallback_when_no_env(self, monkeypatch):
        """With no override vars set, root is <module>/../../.. (plugin checkout root)."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CHAMELEON_PLUGIN_ROOT", raising=False)

        expected = Path(pp.__file__).resolve().parent.parent.parent
        assert plugin_root() == expected

    def test_fallback_root_contains_mcp_package(self, monkeypatch):
        """Sanity: the fallback root really is the checkout (has mcp/chameleon_mcp)."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CHAMELEON_PLUGIN_ROOT", raising=False)

        root = plugin_root()
        assert (root / "mcp" / "chameleon_mcp" / "plugin_paths.py").is_file()


# ---------------------------------------------------------------------------
# plugin_root — .resolve() normalization
# ---------------------------------------------------------------------------


class TestPluginRootResolve:
    def test_dotdot_collapsed(self, monkeypatch):
        """'..' segments in the override are collapsed by .resolve()."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/opt/foo/../bar")
        assert plugin_root() == Path("/opt/bar")

    def test_relative_override_resolved_against_cwd(self, monkeypatch):
        """A relative override becomes absolute relative to the current working dir."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "relplugin")
        assert plugin_root() == (Path.cwd() / "relplugin").resolve()

    def test_trailing_slash_normalized(self, monkeypatch):
        """Trailing slashes are stripped by .resolve()."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/opt/plugin/")
        assert plugin_root() == Path("/opt/plugin")


# ---------------------------------------------------------------------------
# plugin_data_dir
# ---------------------------------------------------------------------------


class TestPluginDataDir:
    def test_override_used_verbatim_when_absolute(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "explicit_data"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))
        assert plugin_data_dir() == target

    def test_default_when_unset(self, monkeypatch):
        """Unset CHAMELEON_PLUGIN_DATA -> ~/.local/share/chameleon."""
        monkeypatch.delenv("CHAMELEON_PLUGIN_DATA", raising=False)
        assert plugin_data_dir() == Path.home() / ".local" / "share" / "chameleon"

    def test_tilde_expanded(self, monkeypatch):
        """A leading ~ in the override is expanded to the user's home."""
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", "~/cham_data")
        assert plugin_data_dir() == Path.home() / "cham_data"

    def test_dotdot_not_resolved(self, monkeypatch):
        """plugin_data_dir only expanduser()s — it does NOT collapse '..' segments."""
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", "/tmp/foo/../bar")
        result = plugin_data_dir()
        assert result == Path("/tmp/foo/../bar")
        assert ".." in result.parts

    def test_returns_path_instance(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "d"))
        assert isinstance(plugin_data_dir(), Path)


# ---------------------------------------------------------------------------
# ensure_plugin_data_dir
# ---------------------------------------------------------------------------


class TestEnsurePluginDataDir:
    def test_creates_dir_and_returns_it(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "made"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))

        assert not target.exists()
        ret = ensure_plugin_data_dir()
        assert ret == target
        assert target.is_dir()

    def test_return_equals_plugin_data_dir(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "consistent"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))
        assert ensure_plugin_data_dir() == plugin_data_dir()

    def test_creates_nested_parents(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))

        ret = ensure_plugin_data_dir()
        assert ret.is_dir()
        assert (tmp_path / "a" / "b").is_dir()

    def test_mode_is_0700(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "secure"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))

        ret = ensure_plugin_data_dir()
        mode = stat.S_IMODE(os.stat(ret).st_mode)
        assert mode == 0o700

    def test_tightens_loose_existing_dir(self, tmp_path: Path, monkeypatch):
        """An existing world-readable dir is chmod'd down to 0700 (idempotent tighten)."""
        target = tmp_path / "wasloose"
        target.mkdir()
        os.chmod(target, 0o755)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o755

        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))
        ensure_plugin_data_dir()
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o700

    def test_idempotent_second_call(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "twice"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))

        first = ensure_plugin_data_dir()
        second = ensure_plugin_data_dir()
        assert first == second
        assert stat.S_IMODE(os.stat(second).st_mode) == 0o700

    def test_chmod_oserror_swallowed(self, tmp_path: Path, monkeypatch):
        """A failing chmod is caught (best-effort); the dir is still returned."""
        target = tmp_path / "chmod_fail"
        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(target))

        def _boom(*_a, **_k):
            raise OSError("read-only fs")

        monkeypatch.setattr(pp.os, "chmod", _boom)
        ret = ensure_plugin_data_dir()
        assert ret == target
        assert target.is_dir()


class TestSecureChmod:
    def test_applies_mode_on_posix(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "f"
        target.write_text("x")
        os.chmod(target, 0o644)
        monkeypatch.setattr(pp.os, "name", "posix")
        assert pp.secure_chmod(target, 0o600) is True
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def test_noop_on_windows(self, tmp_path: Path, monkeypatch):
        # On non-POSIX platforms the POSIX mode is meaningless; the helper skips
        # the chmod and reports that the mode was not enforced, rather than
        # silently pretending it applied.
        #
        # Swap plugin_paths' own `os` reference for a fake with name="nt" instead
        # of flipping the real global os.name: a global os.name="nt" makes
        # CPython 3.11 instantiate WindowsPath inside pytest's own path handling
        # and crash the session on a POSIX host.
        import types

        target = tmp_path / "f"
        target.write_text("x")
        called = {"n": 0}

        def _spy(*_a, **_k):
            called["n"] += 1

        monkeypatch.setattr(pp, "os", types.SimpleNamespace(name="nt", chmod=_spy))
        assert pp.secure_chmod(target, 0o600) is False
        assert called["n"] == 0

    def test_oserror_swallowed_returns_false(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "f"
        target.write_text("x")
        monkeypatch.setattr(pp.os, "name", "posix")

        def _boom(*_a, **_k):
            raise OSError("read-only fs")

        monkeypatch.setattr(pp.os, "chmod", _boom)
        assert pp.secure_chmod(target, 0o600) is False
