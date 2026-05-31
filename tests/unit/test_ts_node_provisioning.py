"""Provisioning + graceful degradation for the TypeScript extractor's node deps.

Covers relocating node_modules out of the (possibly read-only, rebuilt-per-
version) plugin cache into the writable per-user data dir, and the bootstrap
orchestrator degrading to a clean report when Node/npm is unavailable instead
of aborting the whole run.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from chameleon_mcp.extractors.typescript import (
    NodeUnavailableError,
    TypeScriptExtractor,
)


def _isolate_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point plugin-data + plugin-root at empty temp dirs.

    Drops the real CLAUDE_PLUGIN_ROOT (set by the live session) so plugin_root()
    resolves to our temp dir, not the actual checkout.
    """
    data = tmp_path / "data"
    plugin = tmp_path / "plugin"
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(plugin))
    return data, plugin


def _fake_install(node_modules: Path) -> Path:
    """Create a minimal but *complete* typescript install (the entry module that
    require('typescript') loads), so readiness checks see a real install rather
    than a half-written tree."""
    lib = node_modules / "typescript" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "typescript.js").write_text("// stub\n", encoding="utf-8")
    return node_modules


def test_node_modules_dir_is_version_scoped_under_data_dir(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    from chameleon_mcp import __version__

    d = TypeScriptExtractor()._node_modules_dir()
    assert d == tmp_path / "data" / "node-deps" / __version__
    # Never points into the plugin cache dir.
    assert "node-deps" in str(d)
    assert "plugin" not in d.name


def test_ensure_prefers_existing_data_dir_install(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    ext = TypeScriptExtractor()
    nm = _fake_install(ext._node_modules_dir() / "node_modules")
    # Already provisioned -> returned as-is, no npm needed.
    assert ext._ensure_node_modules() == nm


def test_ensure_falls_back_to_legacy_without_writing_plugin_dir(tmp_path, monkeypatch):
    _data, plugin = _isolate_paths(monkeypatch, tmp_path)
    # Data dir is empty; the legacy <plugin>/mcp/node_modules has typescript.
    legacy = _fake_install(plugin / "mcp" / "node_modules")
    ext = TypeScriptExtractor()
    assert ext._ensure_node_modules() == legacy
    # Confirms the data-dir install was NOT triggered (read-only legacy reuse).
    assert not (ext._node_modules_dir() / "node_modules").exists()


def test_ensure_raises_node_unavailable_when_npm_missing(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)  # both dirs empty
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(NodeUnavailableError):
        TypeScriptExtractor()._ensure_node_modules()


def test_ensure_raises_when_install_succeeds_but_typescript_absent(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)  # plugin dir has no mcp/package.json to seed
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")

    import subprocess as _sp

    class _OK:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _OK())
    with pytest.raises(NodeUnavailableError):
        TypeScriptExtractor()._ensure_node_modules()


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="requires non-root POSIX to enforce directory permissions",
)
def test_ensure_raises_node_unavailable_on_readonly_data_dir(tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(ro / "data"))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CHAMELEON_PLUGIN_ROOT", str(tmp_path / "plugin"))
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")
    os.chmod(ro, 0o500)
    try:
        # A read-only data dir is the locked-down case this relocation targets;
        # it must degrade to NodeUnavailableError, not a raw OSError.
        with pytest.raises(NodeUnavailableError):
            TypeScriptExtractor()._ensure_node_modules()
    finally:
        os.chmod(ro, 0o700)


def test_prune_removes_old_other_versions_but_keeps_recent(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    from chameleon_mcp import __version__

    ext = TypeScriptExtractor()
    root = ext._node_modules_dir().parent
    old = root / "0.0.1"
    (old / "node_modules").mkdir(parents=True)
    recent = root / "0.0.2"
    recent.mkdir(parents=True)
    (root / __version__).mkdir(parents=True, exist_ok=True)

    # Age the old dir past the prune TTL; leave the recent one fresh so it's
    # treated as possibly still in use by another live version.
    stamp = time.time() - 8 * 24 * 3600
    os.utime(old, (stamp, stamp))

    ext._prune_stale_node_deps()

    names = {p.name for p in root.iterdir()}
    assert "0.0.1" not in names  # old + stale -> pruned
    assert "0.0.2" in names  # recent -> kept (may still be in use)
    assert __version__ in names  # current version -> never pruned


def test_ensure_waits_for_concurrent_install_then_returns(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    ext = TypeScriptExtractor()
    data_nm = ext._node_modules_dir() / "node_modules"

    import chameleon_mcp.locks as locks

    def fake_lock(path, **_kwargs):
        # Simulate a live peer that just finished installing, then report the
        # lock as held so our caller takes the wait path and picks up the tree.
        _fake_install(data_nm)
        raise locks.LockHeldError(path, 999999, 0.0)

    monkeypatch.setattr(locks, "acquire_advisory_lock", fake_lock)
    assert ext._ensure_node_modules() == data_nm


def test_install_stages_then_atomically_promotes(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")

    ext = TypeScriptExtractor()
    target = ext._node_modules_dir()
    data_nm = target / "node_modules"

    import subprocess as _sp

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, cwd=None, **_kw):
        # Simulate npm populating the STAGING dir's node_modules (cwd is staging).
        _fake_install(Path(cwd) / "node_modules")
        return _Result()

    monkeypatch.setattr(_sp, "run", fake_run)

    result = ext._ensure_node_modules()

    assert result == data_nm
    assert ext._node_modules_ready(data_nm)  # promoted, complete
    # No staging dirs left behind under node-deps/.
    siblings = [p.name for p in target.parent.iterdir()]
    assert not any(".staging-" in name for name in siblings)


def test_install_replaces_existing_partial_target(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")

    ext = TypeScriptExtractor()
    target = ext._node_modules_dir()
    data_nm = target / "node_modules"

    # A crashed prior install: target exists but is NOT ready (no lib/typescript.js).
    (target / "node_modules" / "typescript").mkdir(parents=True)
    (target / "stale_marker.txt").write_text("old", encoding="utf-8")
    assert not ext._node_modules_ready(data_nm)

    import subprocess as _sp

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, cwd=None, **_kw):
        _fake_install(Path(cwd) / "node_modules")
        return _Result()

    monkeypatch.setattr(_sp, "run", fake_run)

    result = ext._ensure_node_modules()

    assert result == data_nm
    assert ext._node_modules_ready(data_nm)  # partial target atomically replaced
    assert not (target / "stale_marker.txt").exists()  # old tree gone
    assert not any(".staging-" in p.name for p in target.parent.iterdir())


def test_install_raises_and_cleans_staging_when_promote_fails(tmp_path, monkeypatch):
    _isolate_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/npm")

    ext = TypeScriptExtractor()
    target = ext._node_modules_dir()

    import subprocess as _sp

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, cwd=None, **_kw):
        _fake_install(Path(cwd) / "node_modules")
        return _Result()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("os.rename", lambda _src, _dst: (_ for _ in ()).throw(OSError("disk full")))

    # A failed atomic promote degrades to NodeUnavailableError, not a raw OSError.
    with pytest.raises(NodeUnavailableError):
        ext._ensure_node_modules()
    # ...and leaves no staging dir behind.
    assert not any(".staging-" in p.name for p in target.parent.iterdir())


def test_bootstrap_degrades_when_node_unavailable(tmp_path, monkeypatch):
    from chameleon_mcp.bootstrap import orchestrator

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "a.ts").write_text("export const x: number = 1;\n", encoding="utf-8")

    def _boom(self, repo_root, paths=None, **kw):  # noqa: ARG001
        raise NodeUnavailableError("npm not found")

    monkeypatch.setattr(orchestrator.TypeScriptExtractor, "parse_repo", _boom, raising=True)
    report = orchestrator._bootstrap_single(repo)
    assert report.status == "failed_node_unavailable"
    assert "npm" in (report.error or "")
