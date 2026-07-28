"""`scripts/prune-plugin-cache.sh` reclaims a cached plugin build only once no
session is still running it.

Claude Code drops a pid-named marker under ``<version>/.in_use/`` when a session
loads that build, and never removes it when the session dies. Testing the
marker directory for bare existence therefore pins every build that was ever
loaded -- on a long-lived machine, every build ever installed -- so the cache
this script exists to reclaim only ever grows. The gate asks whether a recorded
pid is still running instead, and every uncertain answer resolves to keep:
reclaiming disk is never worth deleting the plugin a live session is executing
from.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "prune-plugin-cache.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash required for shell-script tests")


def _cache(tmp_path: Path, versions: dict[str, list[int] | None]) -> tuple[Path, Path]:
    """A fake plugin cache. Each version maps to the pids in its .in_use dir,
    or None for no marker at all."""
    cache_dir = tmp_path / "cache"
    for version, pids in versions.items():
        (cache_dir / version).mkdir(parents=True)
        (cache_dir / version / "plugin.json").write_text("{}", encoding="utf-8")
        if pids is None:
            continue
        marker = cache_dir / version / ".in_use"
        marker.mkdir()
        for pid in pids:
            (marker / str(pid)).write_text(
                json.dumps({"pid": pid, "procStart": "Tue Jul 28 11:48:44 2026"}),
                encoding="utf-8",
            )
    installed = tmp_path / "installed_plugins.json"
    installed.write_text(
        json.dumps({"plugins": {"chameleon@chameleon": [{"version": "9.9.9"}]}}),
        encoding="utf-8",
    )
    return cache_dir, installed


def _run(cache_dir: Path, installed: Path, *args: str, path: str | None = None) -> str:
    env = {
        **os.environ,
        "CHAMELEON_PLUGIN_CACHE_DIR": str(cache_dir),
        "CHAMELEON_INSTALLED_PLUGINS_JSON": str(installed),
    }
    if path is not None:
        env["PATH"] = path
    res = subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


# Everything the script forks EXCEPT ps, so a PATH built from these exercises
# the no-ps branch without breaking the script itself.
_TOOLS_WITHOUT_PS = ("python3", "basename", "find", "rm", "dirname", "cat", "uname")


def _bin_without_ps(tmp_path: Path) -> str:
    binp = tmp_path / "bin_no_ps"
    binp.mkdir()
    for tool in _TOOLS_WITHOUT_PS:
        src = shutil.which(tool)
        if src:
            (binp / tool).symlink_to(src)
    return str(binp)


# A pid that is certainly not running. The kernel caps pids well below this on
# both Linux (/proc/sys/kernel/pid_max) and macOS.
_DEAD_PID = 4_194_303


def test_marker_whose_sessions_are_all_dead_is_prunable(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [_DEAD_PID]})
    out = _run(cache_dir, installed, "--apply")
    assert "prune 1.0.0" in out
    assert not (cache_dir / "1.0.0").exists()


def test_empty_marker_directory_is_prunable(tmp_path):
    """A session that removed its own pid file but left the directory records
    no session at all -- there is nothing that could still be running."""
    cache_dir, installed = _cache(tmp_path, {"1.0.0": []})
    out = _run(cache_dir, installed, "--apply")
    assert "prune 1.0.0" in out
    assert not (cache_dir / "1.0.0").exists()


def test_live_session_pins_its_version(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [os.getpid()]})
    out = _run(cache_dir, installed, "--apply")
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_one_live_pid_among_dead_ones_still_pins(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [_DEAD_PID, os.getpid(), _DEAD_PID + 1]})
    out = _run(cache_dir, installed, "--apply")
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_unrecognised_marker_shape_is_kept(tmp_path):
    """A marker file not named after a pid is a shape this check does not
    understand; keeping the build is the only safe reading."""
    cache_dir, installed = _cache(tmp_path, {"1.0.0": []})
    (cache_dir / "1.0.0" / ".in_use" / "session-abc").write_text("{}", encoding="utf-8")
    out = _run(cache_dir, installed, "--apply")
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_marker_that_is_a_file_not_a_directory_is_kept(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": None})
    (cache_dir / "1.0.0" / ".in_use").write_text("legacy", encoding="utf-8")
    out = _run(cache_dir, installed, "--apply")
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_unmarked_version_is_still_prunable(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": None})
    out = _run(cache_dir, installed, "--apply")
    assert "prune 1.0.0" in out
    assert not (cache_dir / "1.0.0").exists()


def test_current_version_is_kept_even_with_dead_markers(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"9.9.9": [_DEAD_PID]})
    out = _run(cache_dir, installed, "--apply")
    assert "keep 9.9.9 (current)" in out
    assert (cache_dir / "9.9.9").is_dir()


def test_without_ps_every_marked_version_is_kept(tmp_path):
    """`ps` is how a recorded session is checked for liveness. Without it
    nothing can be proven dead, so a marked build must be kept -- pruning
    when the answer is unknowable is how a live session loses its plugin."""
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [_DEAD_PID]})
    out = _run(cache_dir, installed, "--apply", path=_bin_without_ps(tmp_path))
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_without_ps_an_unmarked_version_is_still_prunable(tmp_path):
    """The no-ps fail-safe covers only the liveness question. A version no
    session ever loaded has no marker to be uncertain about."""
    cache_dir, installed = _cache(tmp_path, {"1.0.0": None})
    out = _run(cache_dir, installed, "--apply", path=_bin_without_ps(tmp_path))
    assert "prune 1.0.0" in out
    assert not (cache_dir / "1.0.0").exists()


def test_unreadable_marker_directory_is_kept(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [_DEAD_PID]})
    marker = cache_dir / "1.0.0" / ".in_use"
    marker.chmod(0o000)
    try:
        out = _run(cache_dir, installed, "--apply")
    finally:
        marker.chmod(0o755)
    assert "keep 1.0.0" in out
    assert (cache_dir / "1.0.0").is_dir()


def test_dry_run_is_the_default_and_deletes_nothing(tmp_path):
    cache_dir, installed = _cache(tmp_path, {"1.0.0": [_DEAD_PID]})
    out = _run(cache_dir, installed)
    assert "dry run" in out
    assert (cache_dir / "1.0.0").is_dir()
