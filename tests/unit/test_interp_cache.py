"""Interpreter-resolution cache and fast-mode probe bounding in
`hooks/_resolve-python.sh`.

The resolver caches its winning argv under
`${CHAMELEON_PLUGIN_DATA:-$HOME/.local/share/chameleon}/interp.cache`
(line 1 = the mcp_dir the argv was resolved for, following lines = argv
tokens) so per-edit hooks skip the ladder — a warm resolve is pure shell
builtins. `CHAMELEON_INTERP_CACHE=0` bypasses both read and write. The five
per-edit/per-turn hooks set `CHAMELEON_RESOLVE_FAST=1`, which drops the uv
probe cap 30s -> 5s and bounds the probe with a background poll loop where no
timeout(1)/gtimeout(1) exists (Git Bash / MSYS, coreutils-less macOS) — the
path that previously ran UNCAPPED.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVER = REPO_ROOT / "hooks" / "_resolve-python.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash required for hook-script tests")


def _write_stub(path: Path, *, ge_311: bool) -> None:
    """A fake `python` that answers the resolver's >=3.11 version probe."""
    exit_code = 0 if ge_311 else 1
    path.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do case "$a" in *version_info*) exit {exit_code};; esac; done\n'
        "exit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _touch_exec(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_resolver(
    mcp_dir: Path, path_env: str, extra_env: dict | None = None, timeout: int = 30
) -> tuple[int, list[str]]:
    env = {"PATH": path_env}
    if extra_env:
        env.update(extra_env)
    res = subprocess.run(
        [BASH, str(RESOLVER), str(mcp_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return res.returncode, lines


# The cache WRITE path forks coreutils (mkdir/mv/rm); any real hook PATH has
# them, so the curated test bins symlink them in. The read/hit path stays
# builtins-only — asserted by the poisoned-PATH test, whose second run gets an
# empty PATH dir on purpose.
_CACHE_WRITE_TOOLS = ("mkdir", "mv", "rm")


def _link_write_tools(binp: Path) -> None:
    for tool in _CACHE_WRITE_TOOLS:
        src = shutil.which(tool)
        if src:
            (binp / tool).symlink_to(src)


def _seed(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A ladder-resolvable bin (python3.11 stub), an empty mcp dir, a data dir."""
    binp = tmp_path / "bin"
    binp.mkdir()
    _touch_exec(binp / "python3.11")
    _link_write_tools(binp)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    data = tmp_path / "data"
    return binp, mcp, data


# --------------------------------------------------------------------------- #
# Cache write / read
# --------------------------------------------------------------------------- #


def test_resolve_writes_cache_with_argv(tmp_path):
    """A successful ladder resolve persists mcp_dir + argv to interp.cache."""
    binp, mcp, data = _seed(tmp_path)
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    cache = data / "interp.cache"
    assert cache.is_file()
    assert cache.read_text().splitlines() == [str(mcp), str(binp / "python3.11")]


def test_cache_hit_survives_poisoned_path(tmp_path):
    """A warm resolve is served purely from the cache: with PATH pointing at an
    empty dir (no python, no coreutils) the cached argv still resolves — the
    hit path is shell builtins only."""
    binp, mcp, data = _seed(tmp_path)
    rc, _ = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, lines = _run_resolver(mcp, str(empty), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    assert lines == [str(binp / "python3.11")]


def test_deleted_cached_binary_reresolves_and_repairs(tmp_path):
    """A cached argv whose leading binary is gone is a miss: the ladder re-runs
    and the cache is rewritten with the new winner."""
    binp, mcp, data = _seed(tmp_path)
    rc, _ = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    (binp / "python3.11").unlink()
    bin_b = tmp_path / "bin_b"
    bin_b.mkdir()
    _touch_exec(bin_b / "python3.12")
    _link_write_tools(bin_b)
    rc, lines = _run_resolver(mcp, str(bin_b), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    assert lines == [str(bin_b / "python3.12")]
    cache = data / "interp.cache"
    assert cache.read_text().splitlines() == [str(mcp), str(bin_b / "python3.12")]


def test_cache_mcp_dir_mismatch_is_miss(tmp_path):
    """A cache recorded for a different mcp_dir (e.g. an older plugin version's
    install path) must not be served; the ladder re-runs and the cache is
    re-keyed to the current mcp_dir."""
    binp, mcp, data = _seed(tmp_path)
    decoy = tmp_path / "decoy-python"
    _touch_exec(decoy)  # exists and is executable, so only the key can reject it
    data.mkdir()
    (data / "interp.cache").write_text(f"{tmp_path / 'other-mcp'}\n{decoy}\n")
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    assert (data / "interp.cache").read_text().splitlines() == [
        str(mcp),
        str(binp / "python3.11"),
    ]


def test_corrupt_cache_is_miss_not_error(tmp_path):
    """Garbage cache content (binary bytes, no valid key) degrades to a plain
    ladder resolve — exit 0, correct argv, cache repaired."""
    binp, mcp, data = _seed(tmp_path)
    data.mkdir()
    (data / "interp.cache").write_bytes(b"\x00\xff\xfe garbage\nmore\x00junk\n")
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    assert (data / "interp.cache").read_text().splitlines() == [
        str(mcp),
        str(binp / "python3.11"),
    ]


def test_unwritable_cache_dir_fails_open(tmp_path):
    """An unwritable data dir must not affect resolution: the write is skipped
    silently and the ladder result still prints."""
    binp, mcp, data = _seed(tmp_path)
    data.mkdir()
    data.chmod(0o555)
    try:
        rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data)})
    finally:
        data.chmod(0o755)
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    assert not (data / "interp.cache").exists()


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #


def test_cache_kill_switch_never_creates_file(tmp_path):
    """CHAMELEON_INTERP_CACHE=0 bypasses the write: no interp.cache appears."""
    binp, mcp, data = _seed(tmp_path)
    rc, lines = _run_resolver(
        mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data), "CHAMELEON_INTERP_CACHE": "0"}
    )
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    assert not (data / "interp.cache").exists()


def test_cache_kill_switch_bypasses_read(tmp_path):
    """CHAMELEON_INTERP_CACHE=0 bypasses the read too: a valid cached entry is
    ignored and the ladder answers."""
    binp, mcp, data = _seed(tmp_path)
    decoy = tmp_path / "decoy-python"
    _touch_exec(decoy)
    data.mkdir()
    (data / "interp.cache").write_text(f"{mcp}\n{decoy}\n")
    rc, lines = _run_resolver(
        mcp, str(binp), {"CHAMELEON_PLUGIN_DATA": str(data), "CHAMELEON_INTERP_CACHE": "0"}
    )
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    # The stale decoy entry is preserved untouched (no write under the switch).
    assert (data / "interp.cache").read_text().splitlines() == [str(mcp), str(decoy)]


def test_no_home_no_plugin_data_disables_cache(tmp_path):
    """With neither CHAMELEON_PLUGIN_DATA nor HOME, caching is disabled rather
    than falling back to a world-writable temp dir (a planted cache there could
    hand every hook an attacker-controlled interpreter argv)."""
    binp, mcp, _ = _seed(tmp_path)
    tmpd = tmp_path / "tmpd"
    tmpd.mkdir()
    rc, lines = _run_resolver(mcp, str(binp), {"TMPDIR": str(tmpd)})
    assert rc == 0
    assert lines == [str(binp / "python3.11")]
    assert not list(tmpd.rglob("interp.cache"))


# --------------------------------------------------------------------------- #
# Fast-mode probe bounding
# --------------------------------------------------------------------------- #

# Coreutils the resolver's fast no-timeout path forks: uname (platform branch)
# and sleep (the poll loop). Symlinked into the curated bin so PATH can exclude
# timeout/gtimeout deliberately.
_FAST_PATH_TOOLS = ("bash", "sh", "uname", "sleep")


def _curated_bin_no_timeout(tmp_path: Path) -> Path:
    binp = tmp_path / "notimeout"
    binp.mkdir()
    for tool in _FAST_PATH_TOOLS:
        src = shutil.which(tool)
        if src:
            (binp / tool).symlink_to(src)
    return binp


def _write_hung_uv(binp: Path) -> None:
    uv = binp / "uv"
    uv.write_text("#!/bin/sh\nsleep 60\nexit 1\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_fast_mode_bounds_hung_uv_probe_without_timeout(tmp_path):
    """The core regression: on a host with no timeout/gtimeout binary the uv
    probe used to run UNCAPPED. Under CHAMELEON_RESOLVE_FAST=1 a hung uv must
    be hard-killed by the poll loop in ~5s, and the resolver must fall through
    to the degraded exit (rc 1) instead of hanging the per-edit hook."""
    binp = _curated_bin_no_timeout(tmp_path)
    _write_stub(binp / "python3", ge_311=False)  # rung 4 fallthrough also fails
    _write_hung_uv(binp)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    start = time.monotonic()
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_RESOLVE_FAST": "1"}, timeout=25)
    elapsed = time.monotonic() - start
    assert rc == 1
    assert lines == []
    assert elapsed < 20, f"probe not bounded: took {elapsed:.1f}s"


def test_fast_mode_healthy_probe_resolves_through_poll_loop(tmp_path):
    """A HEALTHY quick probe under the fast no-timeout path must return its
    real exit status through the poll loop's reaping (not 124): a passing
    bare python3 resolves via rung 4."""
    binp = _curated_bin_no_timeout(tmp_path)
    _write_stub(binp / "python3", ge_311=True)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_RESOLVE_FAST": "1"}, timeout=25)
    assert rc == 0
    assert lines == [str(binp / "python3")]


@pytest.mark.skipif(
    shutil.which("timeout") is None and shutil.which("gtimeout") is None,
    reason="needs a real timeout(1)/gtimeout(1)",
)
def test_fast_mode_drops_uv_cap_to_5s_with_timeout_binary(tmp_path):
    """With a timeout binary present, fast mode caps the uv probe at 5s
    (generous mode keeps 30s — asserted indirectly: <20s rules the 30s cap out)."""
    binp = _curated_bin_no_timeout(tmp_path)
    t = shutil.which("timeout") or shutil.which("gtimeout")
    (binp / Path(t).name).symlink_to(t)
    _write_stub(binp / "python3", ge_311=False)
    _write_hung_uv(binp)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    start = time.monotonic()
    rc, lines = _run_resolver(mcp, str(binp), {"CHAMELEON_RESOLVE_FAST": "1"}, timeout=25)
    elapsed = time.monotonic() - start
    assert rc == 1
    assert lines == []
    assert elapsed < 20, f"fast cap not applied: took {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# Hook wiring
# --------------------------------------------------------------------------- #

_FAST_HOOKS = (
    "preflight-and-advise",
    "posttool-recorder",
    "posttool-verify",
    "callout-detector",
    "stop-backstop",
)


@pytest.mark.parametrize("hook", _FAST_HOOKS)
def test_fast_hooks_set_resolve_fast_for_resolver(hook):
    """Each per-edit/per-turn hook must invoke the resolver in fast mode."""
    text = (REPO_ROOT / "hooks" / hook).read_text()
    assert "CHAMELEON_RESOLVE_FAST=1" in text, f"{hook} does not set fast mode"


def test_session_start_keeps_generous_resolve():
    """SessionStart deliberately stays on the generous path (it may pay a cold
    uv materialization) and thereby warms the cache for the fast hooks."""
    text = (REPO_ROOT / "hooks" / "session-start").read_text()
    assert "CHAMELEON_RESOLVE_FAST=1" not in text
