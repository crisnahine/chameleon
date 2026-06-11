"""Unit tests for the opt-in detached post-Stop judge runner (judge_async.py).

The launcher writes a request file and an in-flight marker, then detaches a
``python -m chameleon_mcp.judge_async`` child; the runner consumes the request,
writes a pending-findings file the next UserPromptSubmit delivers, marks the
reviewed files judged, and always clears the in-flight marker. subprocess.Popen
is patched everywhere -- no real child is ever spawned here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from chameleon_mcp import judge_async
from chameleon_mcp.judge import Finding
from chameleon_mcp.optouts import _safe_session_marker

SID = "s-async"
REPO_ID = "async_repo_id"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # The in-flight freshness window scales with the bare-auth state; isolate
    # the marker dir and the process cache so the developer's real marker can
    # never widen (or shrink) a window under test.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    from chameleon_mcp import judge

    monkeypatch.setattr(judge, "_BARE_AUTH_OK", None)
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False)
    yield


def _paths(repo_data: Path):
    safe = _safe_session_marker(SID)
    return (
        repo_data / f".judge_request.{safe}.json",
        repo_data / f".judge_inflight.{safe}.json",
        repo_data / f".judge_pending.{safe}.json",
    )


def _launch(repo_root: Path, repo_data: Path, **overrides):
    kwargs = dict(
        repo_root=repo_root,
        repo_data=repo_data,
        repo_id=REPO_ID,
        session_id=SID,
        fresh_abs_paths=[str(repo_root / "src" / "a.ts")],
        digests={"src/a.ts": "d" * 16},
        turn_key="t" * 32,
        intent_tokens=["25"],
    )
    kwargs.update(overrides)
    return judge_async.launch_async_judge(**kwargs)


# --- launch_async_judge -------------------------------------------------------


def test_launch_returns_false_on_non_posix(tmp_path):
    with patch.object(judge_async.os, "name", "nt"):
        assert _launch(tmp_path / "repo", tmp_path / "data") is False
    req, inflight, _ = _paths(tmp_path / "data")
    assert not req.exists() and not inflight.exists()


def test_launch_returns_false_on_popen_failure(tmp_path):
    repo_data = tmp_path / "data"
    with patch.object(judge_async.subprocess, "Popen", side_effect=OSError("no exec")):
        assert _launch(tmp_path / "repo", repo_data) is False
    # A failed launch leaves no request or in-flight marker to wedge routing.
    req, inflight, _ = _paths(repo_data)
    assert not req.exists() and not inflight.exists()


def test_launch_writes_request_and_inflight_before_spawn(tmp_path):
    repo_data = tmp_path / "data"
    seen = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kwargs):
        req, inflight, _ = _paths(repo_data)
        seen["request_exists"] = req.exists()
        seen["inflight_exists"] = inflight.exists()
        seen["args"] = args
        seen["start_new_session"] = kwargs.get("start_new_session")
        return FakeProc()

    with patch.object(judge_async.subprocess, "Popen", side_effect=fake_popen):
        assert _launch(tmp_path / "repo", repo_data) is True

    # Both files existed before the child was spawned.
    assert seen["request_exists"] is True
    assert seen["inflight_exists"] is True
    assert seen["start_new_session"] is True
    assert "-m" in seen["args"] and "chameleon_mcp.judge_async" in seen["args"]

    req, inflight, _ = _paths(repo_data)
    request = json.loads(req.read_text(encoding="utf-8"))
    assert request["turn_key"] == "t" * 32
    assert request["digests"] == {"src/a.ts": "d" * 16}
    assert request["intent_tokens"] == ["25"]
    assert request["session_id"] == SID
    marker = json.loads(inflight.read_text(encoding="utf-8"))
    assert marker["turn_key"] == "t" * 32
    assert marker["pid"] == 4242
    # No orphaned tmp files from the atomic writes.
    assert not list(repo_data.glob("*.tmp"))


# --- is_inflight_fresh ----------------------------------------------------------


def test_inflight_fresh_under_double_timeout(tmp_path):
    _, inflight, _ = _paths(tmp_path)
    inflight.parent.mkdir(parents=True, exist_ok=True)
    inflight.write_text(
        json.dumps({"turn_key": "t", "started_ts": time.time(), "pid": 1}), encoding="utf-8"
    )
    assert judge_async.is_inflight_fresh(tmp_path, SID) is True
    assert inflight.exists()


def test_inflight_stale_past_double_timeout_unlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_CORRECTNESS_JUDGE_TIMEOUT_SECONDS", "10")
    _, inflight, _ = _paths(tmp_path)
    inflight.write_text(
        json.dumps({"turn_key": "t", "started_ts": time.time() - 21, "pid": 1}),
        encoding="utf-8",
    )
    assert judge_async.is_inflight_fresh(tmp_path, SID) is False
    assert not inflight.exists()


def test_inflight_corrupt_marker_unlinked_and_false(tmp_path):
    _, inflight, _ = _paths(tmp_path)
    inflight.write_text("{broken", encoding="utf-8")
    assert judge_async.is_inflight_fresh(tmp_path, SID) is False
    assert not inflight.exists()


def test_inflight_missing_marker_false(tmp_path):
    assert judge_async.is_inflight_fresh(tmp_path, SID) is False


# --- main (the detached runner) ---------------------------------------------------


def _write_request(repo_root: Path, repo_data: Path, abs_path: str) -> Path:
    req, inflight, _ = _paths(repo_data)
    repo_data.mkdir(parents=True, exist_ok=True)
    req.write_text(
        json.dumps(
            {
                "repo_root": str(repo_root),
                "repo_id": REPO_ID,
                "session_id": SID,
                "abs_paths": [abs_path],
                "digests": {"src/a.ts": "d" * 16},
                "turn_key": "t" * 32,
                "intent_tokens": ["25"],
                "started_ts": time.time(),
            }
        ),
        encoding="utf-8",
    )
    inflight.write_text(
        json.dumps({"turn_key": "t" * 32, "started_ts": time.time(), "pid": 1}),
        encoding="utf-8",
    )
    return req


def test_main_writes_pending_marks_judged_clears_inflight(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    f = repo_root / "src" / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    repo_data = tmp_path / "data"
    req = _write_request(repo_root, repo_data, str(f))

    findings = [Finding(message="dropped await", confidence=0.8, file="src/a.ts", line=3)]
    with patch("chameleon_mcp.judge.run_correctness_judge", return_value=findings):
        rc = judge_async.main([str(req)])

    assert rc == 0
    _, inflight, pending = _paths(repo_data)
    assert not req.exists()  # request consumed
    assert not inflight.exists()  # marker cleared
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["turn_key"] == "t" * 32
    assert data["digests"] == {"src/a.ts": "d" * 16}
    assert data["findings"] == [
        {"file": "src/a.ts", "line": 3, "message": "dropped await", "confidence": 0.8}
    ]
    # The reviewed file is judged at its captured digest under the corr namespace.
    from chameleon_mcp.duplication_review import already_judged

    assert already_judged(repo_data, SID, "src/a.ts", "d" * 16, prefix=".corr_judged.") is True
    # No partial pending file left behind by the atomic write.
    assert not list(repo_data.glob("*.tmp"))


def test_main_clears_inflight_even_when_judge_raises(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_data = tmp_path / "data"
    req = _write_request(repo_root, repo_data, str(repo_root / "src" / "a.ts"))

    with patch("chameleon_mcp.judge.run_correctness_judge", side_effect=RuntimeError("boom")):
        rc = judge_async.main([str(req)])

    assert rc != 0
    _, inflight, pending = _paths(repo_data)
    assert not inflight.exists()
    assert not pending.exists()
    # The pipeline error landed in the session's check-event sidecar.
    from chameleon_mcp.exec_log import read_check_events

    events = read_check_events(REPO_ID, SID, limit=50)["events"]
    assert any(
        e["check"] == "correctness_judge"
        and e["status"] == "degraded_spawn"
        and e["reason"] == "pipeline_error"
        for e in events
    )


def test_main_spawn_failure_does_not_mark_judged(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    f = repo_root / "src" / "a.ts"
    f.write_text("x\n", encoding="utf-8")
    repo_data = tmp_path / "data"
    req = _write_request(repo_root, repo_data, str(f))

    def failing_judge(*a, **k):
        sink = k.get("event_sink")
        if sink is not None:
            sink("spawn_timeout", None)
        return []

    with patch("chameleon_mcp.judge.run_correctness_judge", side_effect=failing_judge):
        rc = judge_async.main([str(req)])

    assert rc == 0
    from chameleon_mcp.duplication_review import already_judged

    assert already_judged(repo_data, SID, "src/a.ts", "d" * 16, prefix=".corr_judged.") is False
    _, inflight, pending = _paths(repo_data)
    assert not inflight.exists()
    assert not pending.exists()


def test_main_translates_judge_facts_sink_kind(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    f = repo_root / "src" / "a.ts"
    f.write_text("x\n", encoding="utf-8")
    repo_data = tmp_path / "data"
    req = _write_request(repo_root, repo_data, str(f))

    def facts_judge(*a, **k):
        sink = k.get("event_sink")
        if sink is not None:
            sink("judge_facts_included", None)
        return []

    with patch("chameleon_mcp.judge.run_correctness_judge", side_effect=facts_judge):
        assert judge_async.main([str(req)]) == 0

    from chameleon_mcp.exec_log import read_check_events

    events = read_check_events(REPO_ID, SID, limit=50)["events"]
    # Same translation as the sync gate: own check name, not a degradation.
    assert any(e["check"] == "judge_facts" and e["status"] == "included" for e in events)
    assert not any(
        e["check"] == "correctness_judge"
        and e["status"] == "degraded_spawn"
        and str(e.get("reason", "")).startswith("judge_facts")
        for e in events
    )


def test_main_missing_request_fails_cleanly(tmp_path):
    assert judge_async.main([str(tmp_path / "no-such-request.json")]) != 0
    assert judge_async.main([]) != 0
