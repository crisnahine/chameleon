"""Hook interpreter resolution: the shared `_resolve-python.sh` ladder, the
shell-level degraded banner, and the Python-side fail-open banner.

Regression guard for the macOS bug where the hooks' interpreter ladder fell
through to a blind `python3` = /usr/bin/python3 = 3.9.x (below the >=3.11 floor,
no chameleon deps). On that interpreter every hook fail-opened silently — in
enforce mode a real violation passed unblocked with no user-visible signal.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVER = REPO_ROOT / "plugin" / "hooks" / "_resolve-python.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash required for hook-script tests")


def _write_stub(path: Path, *, ge_311: bool) -> None:
    """Write a fake `python` that answers the resolver's >=3.11 version probe.

    The resolver probes a bare interpreter with
    `-c 'import sys; raise SystemExit(0 if >=3.11 else 1)'`; the stub exits 0
    when it should pass as >=3.11, 1 otherwise. Any non-probe call exits 0.
    """
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


def _run_resolver(mcp_dir: Path, path_env: str) -> tuple[int, list[str]]:
    res = subprocess.run(
        [BASH, str(RESOLVER), str(mcp_dir)],
        capture_output=True,
        text=True,
        env={"PATH": path_env},
        timeout=30,
    )
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return res.returncode, lines


# --------------------------------------------------------------------------- #
# Resolver ladder
# --------------------------------------------------------------------------- #


def test_rejects_bare_python3_below_floor(tmp_path):
    """The core bug: only python3 = 3.9 on PATH, no uv -> resolver fails rather
    than picking it. (Old ladder picked it blindly and every hook fail-opened.)"""
    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=False)
    mcp = tmp_path / "mcp"  # no .venv
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 1
    assert lines == []


def test_uv_rung_when_only_old_python_and_uv(tmp_path):
    """Reporter's machine: no venv, no python3.11+, python3 = 3.9, uv present ->
    resolves to `uv run --project <mcp> python` (dep-complete, >=3.11)."""
    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=False)
    _touch_exec(binp / "uv")
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 0
    assert lines == [str(binp / "uv"), "run", "--project", str(mcp), "python"]


def test_broken_uv_rung_falls_through(tmp_path):
    """Finding 10: a uv whose probe fails (broken/locked lockfile, offline
    first-materialization, or a shadowing non-chameleon uv) must NOT be accepted.

    Before the probe, rung 3 accepted any `uv` on PATH after only `command -v`,
    so a uv that fails at call time poisoned EVERY hook for the whole session
    (CHAMELEON_PY non-empty -> the no-interpreter degraded banner never fires,
    each hook's `|| printf {}` swallows the failure). The probe runs the real
    `uv run --project <mcp> python` argv once; a non-zero exit falls through to
    rung 4, then to an empty resolution so the degraded banner can fire.
    """
    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=False)  # only sub-3.11 python on PATH
    broken_uv = binp / "uv"
    broken_uv.write_text("#!/bin/sh\nexit 1\n")  # `uv run ...` fails to materialize
    broken_uv.chmod(broken_uv.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 1
    assert lines == []


def test_version_named_python_preferred_over_uv(tmp_path):
    """A version-named python3.x (>=3.11 by name) wins over uv: it is faster to
    start and the hot path is stdlib-only."""
    binp = tmp_path / "bin"
    binp.mkdir()
    _touch_exec(binp / "python3.11")
    _touch_exec(binp / "uv")
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 0
    assert lines == [str(binp / "python3.11")]


def test_bare_python3_accepted_when_probe_passes(tmp_path):
    """A bare python3 that reports >=3.11 is accepted (no uv/venv/named present)."""
    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=True)
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 0
    assert lines == [str(binp / "python3")]


def test_bundled_venv_wins(tmp_path):
    """The bundled venv interpreter is rung 1."""
    mcp = tmp_path / "mcp"
    venv_bin = mcp / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _touch_exec(venv_bin / "python")
    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=False)  # would be rejected if reached
    rc, lines = _run_resolver(mcp, str(binp))
    assert rc == 0
    assert lines == [str(venv_bin / "python")]


def test_resolver_is_sourceable_without_side_effects(tmp_path):
    """Sourcing the resolver defines the function but must not resolve or print
    (only direct execution prints). The hooks invoke it as a subprocess, not by
    sourcing, but the dual-mode contract is kept so the script stays reusable."""
    script = tmp_path / "probe.sh"
    script.write_text(
        f'source "{RESOLVER}"\n'
        "if declare -f _cham_resolve_python >/dev/null; then echo DEFINED; fi\n"
    )
    res = subprocess.run(
        [BASH, str(script)], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}
    )
    assert res.stdout.strip() == "DEFINED"


# --------------------------------------------------------------------------- #
# Shell-level degraded handling in the hooks
# --------------------------------------------------------------------------- #


def _fake_plugin_root(tmp_path) -> Path:
    """A plugin root with the real hook scripts but an empty mcp/ (no venv)."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "mcp").mkdir()
    for f in (REPO_ROOT / "plugin" / "hooks").iterdir():
        if f.is_file():
            dst = root / "hooks" / f.name
            dst.write_text(f.read_text())
            dst.chmod(f.stat().st_mode)
    return root


# Coreutils the hooks reach for on the degraded path; symlinked into the isolated
# bin so the hook still runs. Anything not found on the host is skipped — the
# hooks degrade gracefully when a tool is absent.
_NEEDED_TOOLS = ("bash", "sh", "env", "date", "dirname", "mkdir", "uname", "rm", "cat", "sleep")


def _no_modern_python_bin(tmp_path) -> Path:
    """A single PATH dir that holds the coreutils the hooks need plus python3 /
    python stubs that report < 3.11 — and crucially NO python3.1x name and NO
    uv. The version-named rung trusts a binary's NAME, so the only portable way
    to assert "no >=3.11 interpreter resolves" on a host that ships python3.12 in
    /usr/bin (ubuntu) is to exclude /usr/bin from PATH entirely and provide a
    curated bin with no modern-python name on it."""
    binp = tmp_path / "nomodernpy"
    binp.mkdir()
    for tool in _NEEDED_TOOLS:
        src = shutil.which(tool)
        if src:
            (binp / tool).symlink_to(src)
    _write_stub(binp / "python3", ge_311=False)
    _write_stub(binp / "python", ge_311=False)
    return binp


def test_session_start_emits_degraded_banner_when_no_interpreter(tmp_path):
    root = _fake_plugin_root(tmp_path)
    binp = _no_modern_python_bin(tmp_path)  # python <3.11 only, no python3.1x, no uv
    log = tmp_path / "errors.log"
    res = subprocess.run(
        [str(root / "hooks" / "session-start")],
        input='{"hook_event_name":"SessionStart","session_id":"t","source":"startup","cwd":"/tmp"}',
        capture_output=True,
        text=True,
        env={
            "PATH": str(binp),
            "CLAUDE_PLUGIN_ROOT": str(root),
            "CHAMELEON_HOOK_ERROR_LOG": str(log),
        },
    )
    assert res.returncode == 0, f"stderr={res.stderr}"
    out = json.loads(res.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "degraded" in ctx
    assert "Python >=3.11" in ctx
    assert "/chameleon-doctor" in ctx
    assert "no-interpreter" in log.read_text()


def test_posttool_verify_fails_open_quietly_when_no_interpreter(tmp_path):
    root = _fake_plugin_root(tmp_path)
    binp = _no_modern_python_bin(tmp_path)
    log = tmp_path / "errors.log"
    res = subprocess.run(
        [str(root / "hooks" / "posttool-verify")],
        input='{"hook_event_name":"PostToolUse","tool_name":"Write","cwd":"/tmp",'
        '"tool_input":{"file_path":"/tmp/x.ts"},"tool_response":{"filePath":"/tmp/x.ts"}}',
        capture_output=True,
        text=True,
        env={
            "PATH": str(binp),
            "CLAUDE_PLUGIN_ROOT": str(root),
            "CHAMELEON_HOOK_ERROR_LOG": str(log),
        },
    )
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert res.stdout.strip() == "{}"
    assert "no-interpreter" in log.read_text()


def test_kill_switch_skips_resolution(tmp_path):
    """CHAMELEON_DISABLE short-circuits before any interpreter resolution."""
    root = _fake_plugin_root(tmp_path)
    res = subprocess.run(
        [str(root / "hooks" / "session-start")],
        input="{}",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(root), "CHAMELEON_DISABLE": "1"},
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "{}"


@pytest.mark.parametrize(
    "damage",
    [
        "missing",  # resolver file absent
        "corrupt",  # unterminated function => syntax error when parsed
        "truncated",  # cut mid-statement
        "unreadable",  # present but chmod 000
    ],
)
def test_preflight_fails_open_when_resolver_damaged(tmp_path, damage):
    """A damaged resolver must never abort the PreToolUse hook.

    Regression guard: the hooks run the resolver as a subprocess, not `source`.
    On bash 3.2 a syntax error in a *sourced* file exits 2 — and exit 2 from a
    PreToolUse hook BLOCKS the edit — while `source <missing>` exits 1. Either
    would turn a corrupt-on-disk helper (partial checkout, AV quarantine) into a
    hard block on every edit. Subprocess execution isolates the damage: the hook
    must still exit 0 and emit `{}`.
    """
    root = _fake_plugin_root(tmp_path)
    resolver = root / "hooks" / "_resolve-python.sh"
    if damage == "missing":
        resolver.unlink()
    elif damage == "corrupt":
        resolver.write_text("_cham_resolve_python() {\n  if [ \n")  # never closed
    elif damage == "truncated":
        resolver.write_text(resolver.read_text()[:120])
    elif damage == "unreadable":
        resolver.chmod(0o000)

    binp = tmp_path / "bin"
    binp.mkdir()
    _write_stub(binp / "python3", ge_311=True)  # a healthy interpreter exists...
    res = subprocess.run(
        [str(root / "hooks" / "preflight-and-advise")],
        input='{"hook_event_name":"PreToolUse","tool_name":"Edit","cwd":"/tmp",'
        '"tool_input":{"file_path":"/tmp/x.ts","new_string":"const a = 1"}}',
        capture_output=True,
        text=True,
        env={
            "PATH": f"{binp}:/usr/bin:/bin",
            "CLAUDE_PLUGIN_ROOT": str(root),
            "CHAMELEON_HOOK_ERROR_LOG": str(tmp_path / "errors.log"),
        },
    )
    if damage == "unreadable":
        resolver.chmod(0o644)  # let tmp cleanup remove it
    # ...but the damaged resolver yields no argv, so the hook degrades rather
    # than blocking: exit 0, `{}`, never exit 2.
    assert res.returncode == 0, f"{damage}: stderr={res.stderr}"
    assert res.stdout.strip() == "{}"


# --------------------------------------------------------------------------- #
# Python-side fail-open banner
# --------------------------------------------------------------------------- #


def _banner(repo_root: Path):
    import chameleon_mcp.hook_helper as hh

    return hh._interpreter_degraded_banner(repo_root)


def _seed_log(data_dir: Path, *, age_seconds: int, marker: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    log = data_dir / ".hook_errors.log"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_seconds))
    log.write_text(f"[{ts}] session-start {marker} (x)\n")
    return log


def test_banner_none_for_unprofiled_repo_no_side_effect(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    log = _seed_log(data_dir, age_seconds=10, marker="no-interpreter")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))
    repo = tmp_path / "repo"  # no .chameleon
    repo.mkdir()
    assert _banner(repo) is None
    # Side-effect free: no per-repo data dir created for an unprofiled repo.
    from chameleon_mcp.tools import _compute_repo_id

    assert not (data_dir / _compute_repo_id(repo)).exists()


def test_banner_surfaces_for_profiled_repo_with_recent_failopen(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    log = _seed_log(data_dir, age_seconds=10, marker="no-interpreter")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    banner = _banner(repo)
    assert banner is not None
    assert "fail-open" in banner
    assert "/chameleon-doctor" in banner
    # Re-show cooldown marker (DRIFT_BANNER_TTL_SECONDS, 7d) suppresses the
    # second call within the window.
    assert _banner(repo) is None


def test_banner_ignores_failopens_older_than_24h(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    log = _seed_log(data_dir, age_seconds=200_000, marker="no-interpreter")  # >24h
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    assert _banner(repo) is None


def test_banner_single_spawn_failure_below_threshold(tmp_path, monkeypatch):
    """A lone `failed (python=...)` line (could be a one-off timeout) does not
    alarm; only `no-interpreter` raises on the first, spawn-fails need >=3."""
    data_dir = tmp_path / "data"
    log = _seed_log(data_dir, age_seconds=10, marker="failed (python=/usr/bin/python3)")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    assert _banner(repo) is None


def test_banner_count_matches_triggering_reason(tmp_path, monkeypatch):
    """When no-interpreter fires, below-threshold one-off spawn-fails must not
    inflate the headline count (it reports the triggering reason's count)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    log = data_dir / ".hook_errors.log"
    now = time.time()
    lines = []
    for i in range(1):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10 - i))
        lines.append(f"[{ts}] session-start no-interpreter (x)")
    for i in range(2):  # two spawn-fails, below the >=3 alarm threshold
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 20 - i))
        lines.append(f"[{ts}] posttool-verify failed (python=/usr/bin/python3)")
    log.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(data_dir))
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    banner = _banner(repo)
    assert banner is not None
    assert "1 hook fail-open" in banner  # not "3"
    assert "no Python >=3.11 resolved" in banner
