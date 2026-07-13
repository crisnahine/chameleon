"""An active /chameleon-pause-15m window silently disables the eval-call and
secret-detected-in-content security denies, so the status line must surface
it -- otherwise the user has no visible signal that those gates are off.

The statusline reads the same ``${PLUGIN_DATA}/<repo_id>/.pause_until`` file
`is_chameleon_suppressed` (optouts.py) checks, so it can never drift from what
the hooks actually honor.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from chameleon_mcp.optouts import write_pause
from chameleon_mcp.repo_id import _compute_repo_id

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugin" / "bin" / "chameleon-statusline.sh"


def _repo_with_profile(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True)
    (repo / ".chameleon" / "profile.json").write_text("{}", encoding="utf-8")
    return repo


def _write_cache(repo: Path, profiles: list[dict]) -> None:
    import json

    cdir = repo / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / ".chameleon-statusline-cache"
    cache.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")


def _write_pause(monkeypatch, plugin_data: Path, repo: Path, minutes: int) -> None:
    """Write `.pause_until` via the real `write_pause`, not a hand-rolled
    format, so the test stays honest about what the tool actually persists.
    `minutes` may be negative to produce an already-expired marker.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(plugin_data))
    repo_id = _compute_repo_id(repo)
    write_pause(repo_id, minutes)


def _run(
    repo: Path, plugin_data: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    payload = f'{{"workspace":{{"project_dir":"{repo}"}}}}'
    env = {**os.environ, "CHAMELEON_PLUGIN_DATA": str(plugin_data)}
    env.update(extra_env or {})
    return subprocess.run(
        [str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_active_pause_surfaced_via_cache_path(tmp_path, monkeypatch):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"
    _write_pause(monkeypatch, plugin_data, repo, 15)

    proc = _run(repo, plugin_data)
    assert proc.returncode == 0, proc.stderr
    assert "paused" in proc.stdout
    assert "repo (trusted)" in proc.stdout


def test_active_pause_surfaced_without_cache_fallback_path(tmp_path, monkeypatch):
    repo = _repo_with_profile(tmp_path)
    plugin_data = tmp_path / "plugin_data"
    _write_pause(monkeypatch, plugin_data, repo, 15)

    proc = _run(repo, plugin_data)
    assert proc.returncode == 0, proc.stderr
    assert "paused" in proc.stdout
    assert "repo" in proc.stdout


def test_expired_pause_is_not_surfaced(tmp_path, monkeypatch):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"
    _write_pause(monkeypatch, plugin_data, repo, -5)

    proc = _run(repo, plugin_data)
    assert proc.returncode == 0, proc.stderr
    assert "paused" not in proc.stdout


def test_no_pause_file_leaves_output_unchanged(tmp_path):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"  # never written to

    proc = _run(repo, plugin_data)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "🦎 chameleon │ repo (trusted)"


def test_corrupt_pause_file_fails_open(tmp_path):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"
    repo_id = _compute_repo_id(repo)
    repo_dir = plugin_data / repo_id
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".pause_until").write_bytes(b"not-a-timestamp\x00garbage")

    proc = _run(repo, plugin_data)
    assert proc.returncode == 0, proc.stderr
    assert "paused" not in proc.stdout


def test_kill_switch_suppresses_pause_display(tmp_path, monkeypatch):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"
    _write_pause(monkeypatch, plugin_data, repo, 15)

    proc = _run(repo, plugin_data, {"CHAMELEON_STATUSLINE_PAUSE": "0"})
    assert proc.returncode == 0, proc.stderr
    assert "paused" not in proc.stdout


def test_within_time_budget_when_paused(tmp_path, monkeypatch):
    repo = _repo_with_profile(tmp_path)
    _write_cache(repo, [{"name": "repo", "trust": "trusted"}])
    plugin_data = tmp_path / "plugin_data"
    _write_pause(monkeypatch, plugin_data, repo, 15)

    start = time.monotonic()
    proc = _run(repo, plugin_data)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert proc.returncode == 0
    # Generous bound for CI cold-start jitter; the repo_id derivation only runs
    # during the bounded pause window, not on every render.
    assert elapsed_ms < 3000, f"status line took {elapsed_ms:.0f}ms while paused"
