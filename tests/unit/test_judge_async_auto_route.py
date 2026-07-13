"""Auto-routing of the correctness judge through the detached async path.

When a prior spawn proved ``claude --bare`` loses credentials on this install,
the plain fallback spawn pays the full session primer and cannot fit the
synchronous Stop budget (the stop-backstop wrapper caps the hook at 55s). The
judge route therefore auto-prefers the detached async path on POSIX even
without ``CHAMELEON_JUDGE_ASYNC=1``; the detached child runs the plain spawn
under the generous ``CORRECTNESS_JUDGE_FALLBACK_TIMEOUT_SECONDS`` budget and
its findings deliver at the next user prompt. An explicit
``CHAMELEON_JUDGE_ASYNC=0`` forces sync regardless. Isolation mirrors
test_correctness_judge_gate; no real subprocess is ever spawned here.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp import judge, judge_async
from chameleon_mcp.enforcement import EnforcementState, FileState, save_state
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


def _write_bare_auth_marker(tmp_path: Path) -> None:
    (tmp_path / ".bare_auth_failed").write_text(str(int(time.time())), encoding="utf-8")


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory():
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "enforcement": {
                        "mode": "enforce",
                        "idiom_review": False,
                        # multi_lens_review (default-on) replaces the single
                        # correctness-judge spawn this routing test exercises.
                        "multi_lens_review": False,
                        "correctness_judge": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / REPO_ID
        data_dir.mkdir(parents=True, exist_ok=True)
        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.side_effect = lambda root: hash_profile(profile_dir)

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID))
        stack.enter_context(
            patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec)
        )
        stack.enter_context(
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None)
        )
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        # idiom_review=False only silences idiom/principle content; the
        # independent "no passing test run" reminder still fires for a real
        # source edit with no recorded test run. Record one so this gate's own
        # surface (async judge routing) is reached undisturbed, mirroring
        # test_idiom_review.py's isolation for the same reminder.
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(REPO_ID, session_id=SID, command="pytest -q", exit_code=0)

        return repo, data_dir, file_path

    try:
        yield _factory
    finally:
        stack.close()


class _FakeProc:
    pid = 4242


_REAL_POPEN = judge_async.subprocess.Popen


def _run_stop(repo, env):
    """Run stop_backstop, recording detached judge_async child spawns.

    Other Popen users on the Stop path (git probes) pass through to the real
    Popen; only the ``python -m chameleon_mcp.judge_async`` spawn is faked and
    recorded, so ``child_spawns`` counts exactly the detached judge launches.
    """
    cap: list = []
    child_spawns: list = []

    def selective_popen(args, **kwargs):
        argv = [str(a) for a in (args if isinstance(args, (list, tuple)) else [args])]
        if any("chameleon_mcp.judge_async" in a for a in argv):
            child_spawns.append(argv)
            return _FakeProc()
        return _REAL_POPEN(args, **kwargs)

    with ExitStack() as stack:
        payload = {"session_id": SID, "cwd": str(repo), "stop_hook_active": False}
        stack.enter_context(patch("sys.stdin", io.StringIO(json.dumps(payload))))
        out = stack.enter_context(patch("sys.stdout"))
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False)
        )
        mock_rcj = stack.enter_context(
            patch("chameleon_mcp.judge.run_correctness_judge", return_value=[])
        )
        stack.enter_context(patch.object(judge_async.subprocess, "Popen", selective_popen))
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return (json.loads(s) if s else {}), mock_rcj, child_spawns


def _touch_edited_file(file_path: str, data_dir: Path):
    Path(file_path).write_text("x = 1\n", encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, SID)


def _events() -> list[dict]:
    from chameleon_mcp.exec_log import read_check_events

    out = read_check_events(REPO_ID, SID, limit=200)
    return [e for e in out["events"] if e.get("check") == "correctness_judge"]


def _request_path(data_dir: Path) -> Path:
    return data_dir / f".judge_request.{_safe_session_marker(SID)}.json"


# --- route selection ----------------------------------------------------------


def test_bare_auth_marker_routes_async_without_env_var(make_trusted_repo, tmp_path):
    repo, data_dir, file_path = make_trusted_repo()
    _touch_edited_file(file_path, data_dir)
    _write_bare_auth_marker(tmp_path)

    out, mock_rcj, child_spawns = _run_stop(repo, env={"CHAMELEON_ENFORCE": "1"})

    # The sync pipeline never ran in the hook process; the detached child owns it.
    mock_rcj.assert_not_called()
    assert len(child_spawns) == 1
    req = _request_path(data_dir)
    assert req.is_file()
    request = json.loads(req.read_text(encoding="utf-8"))
    assert request["abs_paths"] == [file_path]
    assert (data_dir / f".judge_inflight.{_safe_session_marker(SID)}.json").is_file()
    assert out == {}
    assert any(
        e["status"] == "spawned"
        and (e.get("detail") or {}).get("mode") == "async_auto_bare_fallback"
        for e in _events()
    )


def test_env_zero_with_marker_forces_sync(make_trusted_repo, tmp_path):
    repo, data_dir, file_path = make_trusted_repo()
    _touch_edited_file(file_path, data_dir)
    _write_bare_auth_marker(tmp_path)

    _, mock_rcj, child_spawns = _run_stop(
        repo, env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_JUDGE_ASYNC": "0"}
    )

    mock_rcj.assert_called_once()
    assert child_spawns == []
    assert not _request_path(data_dir).exists()
    assert any(
        e["status"] == "spawned" and (e.get("detail") or {}).get("mode") == "sync"
        for e in _events()
    )


def test_no_marker_defaults_to_sync(make_trusted_repo):
    repo, data_dir, file_path = make_trusted_repo()
    _touch_edited_file(file_path, data_dir)

    _, mock_rcj, child_spawns = _run_stop(repo, env={"CHAMELEON_ENFORCE": "1"})

    mock_rcj.assert_called_once()
    assert child_spawns == []
    assert not _request_path(data_dir).exists()
    assert any(
        e["status"] == "spawned" and (e.get("detail") or {}).get("mode") == "sync"
        for e in _events()
    )


def test_opt_in_env_var_records_opt_in_mode(make_trusted_repo):
    repo, data_dir, file_path = make_trusted_repo()
    _touch_edited_file(file_path, data_dir)

    _, mock_rcj, child_spawns = _run_stop(
        repo, env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_JUDGE_ASYNC": "1"}
    )

    mock_rcj.assert_not_called()
    assert len(child_spawns) == 1
    assert _request_path(data_dir).is_file()
    assert any(
        e["status"] == "spawned" and (e.get("detail") or {}).get("mode") == "async_opt_in"
        for e in _events()
    )


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
