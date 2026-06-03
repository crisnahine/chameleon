"""The status line must hold a constant jq spawn count regardless of profile
count (the per-field/per-profile jq loop blew past the <100ms budget at ~12+
profiles), and it must strip multibyte Unicode controls (bidi overrides,
zero-width, C1) from the attacker-controllable cache, not just C0/DEL.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "chameleon-statusline.sh"

# A bidi right-to-left override and a zero-width space embedded in a profile
# name: both survive `tr -d '[:cntrl:]'` because they are multibyte Unicode.
BIDI_OVERRIDE = "‮"
ZERO_WIDTH = "​"


def _write_cache(tmp_path: Path, profiles: list[dict]) -> Path:
    import json

    cdir = tmp_path / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / ".chameleon-statusline-cache"
    cache.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return cache


def _jq_counting_path(tmp_path: Path) -> tuple[str, Path]:
    """Build a PATH whose `jq` is a shim that appends a line to a counter file
    per invocation, then delegates to the real jq."""
    real_jq = subprocess.run(
        ["bash", "-lc", "command -v jq"], capture_output=True, text=True
    ).stdout.strip()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    counter = tmp_path / "jq_calls"
    shim = shim_dir / "jq"
    shim.write_text(
        f'#!/usr/bin/env bash\necho x >> "{counter}"\nexec "{real_jq}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    base = os.environ.get("PATH", "")
    return f"{shim_dir}:{base}", counter


def _run(tmp_path: Path, path_env: str) -> subprocess.CompletedProcess:
    payload = f'{{"workspace":{{"project_dir":"{tmp_path}"}}}}'
    # Force the C locale: BSD `tr` in a UTF-8 locale incidentally drops some
    # multibyte controls, masking the bug. Under `LC_ALL=C` (GNU tr, minimal
    # CI/Docker images) the bidi/zero-width chars survive `tr -d '[:cntrl:]'`,
    # which is the worst case the sanitizer must handle deterministically.
    env = {**os.environ, "PATH": path_env, "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(
        [str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_jq_spawn_count_is_constant_not_per_profile(tmp_path):
    path_env, counter = _jq_counting_path(tmp_path)
    profiles = [{"name": f"repo-{i}", "trust": "trusted"} for i in range(15)]
    _write_cache(tmp_path, profiles)

    proc = _run(tmp_path, path_env)
    assert proc.returncode == 0
    calls = counter.read_text().count("x") if counter.exists() else 0
    # 15 profiles must not produce ~33 (2N+3) jq spawns. A single collapsed
    # pass keeps this small and independent of profile count.
    assert calls <= 4, f"jq spawned {calls} times for 15 profiles (expected constant)"


def test_jq_spawn_count_does_not_grow_with_profiles(tmp_path):
    small_dir = tmp_path / "small"
    big_dir = tmp_path / "big"
    small_dir.mkdir()
    big_dir.mkdir()

    path_small, counter_small = _jq_counting_path(small_dir)
    path_big, counter_big = _jq_counting_path(big_dir)

    _write_cache(small_dir, [{"name": "r0", "trust": "trusted"}])
    _write_cache(big_dir, [{"name": f"r{i}", "trust": "trusted"} for i in range(20)])

    assert _run(small_dir, path_small).returncode == 0
    assert _run(big_dir, path_big).returncode == 0

    n_small = counter_small.read_text().count("x") if counter_small.exists() else 0
    n_big = counter_big.read_text().count("x") if counter_big.exists() else 0
    assert n_small == n_big, f"jq count grew with profiles: {n_small} -> {n_big}"


def test_bidi_and_zero_width_stripped_from_output(tmp_path):
    name = f"repo{BIDI_OVERRIDE}name{ZERO_WIDTH}"
    _write_cache(tmp_path, [{"name": name, "trust": "trusted"}])
    proc = _run(tmp_path, os.environ.get("PATH", ""))
    assert proc.returncode == 0
    assert BIDI_OVERRIDE not in proc.stdout, "bidi override leaked into status line"
    assert ZERO_WIDTH not in proc.stdout, "zero-width char leaked into status line"
    # The visible name text still renders.
    assert "reponame" in proc.stdout


def _path_without_jq(tmp_path: Path) -> str:
    """A PATH that has every tool the script needs (bash, env, tr, stat, date,
    python3, ...) symlinked in, but deliberately no jq, so the python fallback
    branch runs."""
    needed = [
        "bash",
        "env",
        "cat",
        "tr",
        "stat",
        "date",
        "grep",
        "head",
        "sed",
        "basename",
        "dirname",
        "python3",
        "seq",
    ]
    bindir = tmp_path / "nojq_bin"
    bindir.mkdir()
    for tool in needed:
        real = subprocess.run(
            ["bash", "-lc", f"command -v {tool}"], capture_output=True, text=True
        ).stdout.strip()
        if real:
            (bindir / tool).symlink_to(real)
    return str(bindir)


def test_bidi_stripped_in_jq_absent_fallback(tmp_path):
    """With jq removed from PATH the python fallback runs; it must strip the
    same multibyte controls."""
    name = f"a{BIDI_OVERRIDE}b{ZERO_WIDTH}c"
    _write_cache(tmp_path, [{"name": name, "trust": "trusted"}])
    proc = _run(tmp_path, _path_without_jq(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert BIDI_OVERRIDE not in proc.stdout
    assert ZERO_WIDTH not in proc.stdout
    assert "abc" in proc.stdout


def test_within_time_budget_at_many_profiles(tmp_path):
    _write_cache(tmp_path, [{"name": f"r{i}", "trust": "trusted"} for i in range(15)])
    start = time.monotonic()
    proc = _run(tmp_path, os.environ.get("PATH", ""))
    elapsed_ms = (time.monotonic() - start) * 1000
    assert proc.returncode == 0
    # Generous bound for CI cold-start jitter; the point is it does not balloon
    # with profile count the way the 2N+3 loop did.
    assert elapsed_ms < 800, f"status line took {elapsed_ms:.0f}ms"
