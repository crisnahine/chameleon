"""judge_async.py internals: the bare-auth fallback timeout seam and the
detached child's own budget/inflight-window mechanics.

Phase-3 cutover note: the Stop-time ROUTE SELECTION this file used to pin
(``CHAMELEON_JUDGE_ASYNC`` opt-in / auto-bare-fallback / forced-sync, driven
end-to-end through ``stop_backstop()``) is gone -- the live Stop pipeline no
longer calls ``_correctness_judge_gate``/``_judge_async_mode`` at all (see
``stop/pipeline.py``'s ``_run_review_job``, which routes through
``stop/scheduler.py`` instead; that module's own route/launch decisions are
pinned in test_stop_scheduler.py). ``judge.py`` and ``judge_async.py``
themselves are unchanged and still live (a later task deletes them), so
their own direct-call unit tests below -- unaffected by the pipeline cutover
-- stay exactly as they were.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from chameleon_mcp import judge, judge_async
from chameleon_mcp.optouts import _safe_session_marker

REPO_ID = "auto_async_repo_id"
SID = "s-auto-async"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # The bare-auth marker lives under the plugin data dir; isolate it so the
    # developer's real marker can never leak into a test verdict.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_JUDGE_ASYNC", raising=False)
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", None, raising=False)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False, raising=False)
    yield


# --- the timeout seam ---------------------------------------------------------


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.real_judge_spawn
def test_detached_child_with_marker_uses_fallback_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_BARE_SUPPORTED", True)
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", False)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", True)
    captured: dict = {}

    def run(args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        captured["args"] = list(args)
        return _Proc(0, stdout="stream")

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    assert captured["timeout"] == 180
    assert "--bare" not in captured["args"]


@pytest.mark.real_judge_spawn
def test_sync_spawn_with_marker_keeps_short_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_BARE_SUPPORTED", True)
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", False)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False)
    captured: dict = {}

    def run(args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return _Proc(0, stdout="stream")

    with patch("subprocess.run", side_effect=run):
        judge._spawn_reviewer_status("prompt", tmp_path)
    assert captured["timeout"] == 45


@pytest.mark.real_judge_spawn
def test_detached_child_without_marker_keeps_short_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_BARE_SUPPORTED", True)
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", True)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", True)
    captured: dict = {}

    def run(args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return _Proc(0, stdout="stream")

    with patch("subprocess.run", side_effect=run):
        judge._spawn_reviewer_status("prompt", tmp_path)
    assert captured["timeout"] == 45


def test_async_main_marks_detached_run(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False, raising=False)
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    f = repo_root / "src" / "a.ts"
    f.write_text("x\n", encoding="utf-8")
    repo_data = tmp_path / "data"
    repo_data.mkdir(parents=True, exist_ok=True)
    req = repo_data / f".judge_request.{_safe_session_marker(SID)}.json"
    req.write_text(
        json.dumps(
            {
                "repo_root": str(repo_root),
                "repo_id": REPO_ID,
                "session_id": SID,
                "abs_paths": [str(f)],
                "digests": {"src/a.ts": "d" * 16},
                "turn_key": "t" * 32,
                "intent_tokens": [],
                "started_ts": time.time(),
            }
        ),
        encoding="utf-8",
    )
    seen: dict = {}

    def fake_judge(*a, **k):
        seen["detached"] = judge._RUNNING_DETACHED
        return []

    with patch("chameleon_mcp.judge.run_correctness_judge", side_effect=fake_judge):
        assert judge_async.main([str(req)]) == 0
    assert seen["detached"] is True


def test_inflight_window_extends_when_marker_present(tmp_path, monkeypatch):
    inflight = tmp_path / f".judge_inflight.{_safe_session_marker(SID)}.json"
    payload = {"turn_key": "t", "started_ts": time.time() - 120, "pid": 1}

    # 120s old: past twice the 45s sync budget, but well inside twice the
    # 180s fallback budget the detached child is actually running under.
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", False)
    inflight.write_text(json.dumps(payload), encoding="utf-8")
    assert judge_async.is_inflight_fresh(tmp_path, SID) is True
    assert inflight.exists()

    # Without the marker the child runs the short budget; the same age is an
    # orphan and is swept.
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", True)
    assert judge_async.is_inflight_fresh(tmp_path, SID) is False
    assert not inflight.exists()


def test_fallback_threshold_registered_in_defaults():
    from chameleon_mcp._thresholds import DEFAULTS

    assert DEFAULTS["CORRECTNESS_JUDGE_FALLBACK_TIMEOUT_SECONDS"] == 180
