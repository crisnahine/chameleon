"""Stop-hook duplication-lens config wiring, post phase-3 cutover.

The standalone duplication gate (its own `claude -p` spawn, its own
per-session cap, its own defer-matrix against the correctness judge) is gone
from the Stop pipeline. Duplication review is now one of the lenses
`stop/scheduler.py` may include in a single detached review job's
`JobRequest.lens_names` -- the generic scheduling contract (routing, the
session's one job slot, SubagentStop never scheduling, fail-open, digest/
session-cap dedup) is exercised once, lens-agnostically, in
test_correctness_judge_gate.py; this file keeps only what is specific to
duplication: the `enforcement.duplication_review` flag actually excludes
"duplication" from the launched job's lens set. Duplication FINDING
production (body-hash/semantic gather, confirm spawn, canonical Finding
shape) is covered at the lens level in test_stop_lens_duplication.py -- it
never runs synchronously at Stop anymore, so it cannot be pinned here.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chameleon_mcp.enforcement import EnforcementState, FileState, save_state


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    key_file = tmp_path / "hmac.key"
    key_file.write_bytes(b"k" * 32)
    key_file.chmod(0o600)
    monkeypatch.setenv("CHAMELEON_HMAC_KEY_PATH", str(key_file))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # stop/scheduler.py's session-doc/heartbeat files resolve via
    # profile.trust.repo_data_dir(repo_id), which reads this env var
    # directly (not the patched hook_helper._plugin_data_dir below) --
    # without it, every qualifying Stop's routing would read/write the
    # developer's REAL ~/.local/share/chameleon/.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    yield


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "shadow", duplication_review: bool = True):
        repo_id = "dup_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "duplication_review": duplication_review}}),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-dup"

        from chameleon_mcp.profile.trust import hash_profile

        trust_rec = MagicMock()
        trust_rec.grants_root.return_value = True
        trust_rec.hash_for_root.side_effect = lambda root: hash_profile(profile_dir)

        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id))
        stack.enter_context(
            patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec)
        )
        stack.enter_context(
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None)
        )
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )

        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(repo_id, session_id=session_id, command="pytest -q", exit_code=0)

        return repo, data_dir, session_id, file_path, profile_dir

    try:
        yield _factory
    finally:
        stack.close()


def _seed_edited(
    file_path: str, data_dir: Path, session_id: str, content: str = "export const C = 1\n"
):
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _run_stop(payload, env, *, launch_job):
    calls: list = []

    def _wrapped(request):
        calls.append(request)
        return launch_job(request)

    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False),
        patch("chameleon_mcp.stop.scheduler.launch_job", _wrapped),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    raw = "".join(cap).strip()
    return (json.loads(raw) if raw else {}), calls


def test_duplication_review_on_by_default_included_in_launch(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)

    out, calls = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        launch_job=lambda req: False,
    )

    assert len(calls) == 1
    assert "duplication" in calls[0].lens_names
    assert out.get("decision") != "block"


def test_duplication_review_disabled_excludes_lens(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(duplication_review=False)
    _seed_edited(file_path, data_dir, sid)

    out, calls = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        launch_job=lambda req: False,
    )

    assert len(calls) == 1
    assert "duplication" not in calls[0].lens_names
    assert set(calls[0].lens_names) == {"correctness", "idiom"}


def test_subagentstop_never_launches(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo()
    _seed_edited(file_path, data_dir, sid)

    out, calls = _run_stop(
        {
            "session_id": sid,
            "cwd": str(repo),
            "stop_hook_active": False,
            "hook_event_name": "SubagentStop",
        },
        env={"CHAMELEON_ENFORCE": "1"},
        launch_job=lambda req: False,
    )

    assert calls == []
    assert out == {}


def test_off_mode_never_launches(make_trusted_repo):
    repo, data_dir, sid, file_path, profile_dir = make_trusted_repo(mode="off")
    _seed_edited(file_path, data_dir, sid)

    out, calls = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False},
        env={"CHAMELEON_ENFORCE": "1"},
        launch_job=lambda req: False,
    )

    assert calls == []
    assert out == {}
