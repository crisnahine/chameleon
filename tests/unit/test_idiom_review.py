"""Stop-hook idiom-review tests, post phase-3 cutover.

The legacy guaranteed-interrupt gate (`_idiom_review_gate`) is no longer
called from the Stop pipeline: idiom review is now a scoped detector lens
(`stop/lenses/idiom.py`, Task 3) that runs INSIDE the async review job --
"compliant turns hear nothing" replaces "block once per session regardless
of whether anything was violated." Every scoping/language/archetype/
citation/fail-open behavior the legacy gate's content-rendering pinned is
now pinned at the lens level (see test_stop_lens_idiom.py); this file keeps
only the pipeline-level contract: a governed edit with real taught idioms
present never interrupts the turn, in any mode, and the durable
`enforcement.idiom_review: false` switch actually excludes the idiom lens
from the launched job's lens set (not just from some in-turn text -- there
is none to disclose into anymore).

`stop.scheduler.launch_job` is neutralized by the autouse conftest guard;
these tests never observe real job content (that is the lens's own test
file's job), only that the Stop hook itself no longer interrupts.
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


@pytest.fixture
def make_trusted_repo(tmp_path):
    stack = ExitStack()

    def _factory(*, mode: str = "enforce", stop_block_cap: int = 3):
        repo_id = "idiom_repo_id"
        repo = tmp_path / "repo"
        profile_dir = repo / ".chameleon"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.joinpath("config.json").write_text(
            json.dumps({"enforcement": {"mode": mode, "stop_block_cap": stop_block_cap}}),
            encoding="utf-8",
        )
        profile_dir.joinpath("profile.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

        data_dir = tmp_path / repo_id
        data_dir.mkdir(parents=True, exist_ok=True)

        file_path = str(repo / "src" / "Widget.ts")
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)

        session_id = "s-idiom"

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

        return repo, data_dir, session_id, file_path, profile_dir, repo_id

    try:
        yield _factory
    finally:
        stack.close()


def _env(tmp_path: Path) -> dict:
    return {
        "CHAMELEON_ENFORCE": "1",
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
        # stop/scheduler.py's session-doc/heartbeat files resolve via
        # profile.trust.repo_data_dir(repo_id), which reads this env var
        # directly (NOT the patched hook_helper._plugin_data_dir) -- without
        # it, the model-review routing every qualifying Stop now triggers
        # would read/write the developer's REAL ~/.local/share/chameleon/.
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
    }


def _run_stop(payload, env):
    cap = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
        patch("chameleon_mcp.hook_helper._stop_file_still_blockable", return_value=False),
    ):
        out.write = cap.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()
    s = "".join(cap).strip()
    return json.loads(s) if s else {}


def _touch_edited_file(file_path: str, data_dir: Path, session_id: str, content: str = "x = 1\n"):
    Path(file_path).write_text(content, encoding="utf-8")
    st = EnforcementState()
    st.files[file_path] = FileState()
    save_state(st, data_dir, session_id)


def _write_idioms(profile_dir: Path, text: str = "- Always wrap DB calls in a transaction.\n"):
    profile_dir.joinpath("idioms.md").write_text(text, encoding="utf-8")


def _seed_passing_test_run(tmp_path: Path, repo_id: str, session_id: str, env: dict) -> None:
    # Isolates these tests from the independent test-run-reminder advisory
    # (test_idiom_review_test_signal.py covers that surface on its own).
    with patch.dict(os.environ, env, clear=False):
        from chameleon_mcp.exec_log import append_exec_log

        append_exec_log(repo_id, session_id=session_id, command="pytest -q", exit_code=0)


@pytest.mark.parametrize("mode", ["enforce", "shadow", "off"])
def test_governed_edit_with_idioms_never_interrupts(make_trusted_repo, tmp_path, mode):
    repo, data_dir, sid, file_path, profile_dir, repo_id = make_trusted_repo(mode=mode)
    env = _env(tmp_path)
    _seed_passing_test_run(tmp_path, repo_id, sid, env)
    _touch_edited_file(file_path, data_dir, sid)
    _write_idioms(profile_dir)

    out = _run_stop({"session_id": sid, "cwd": str(repo), "stop_hook_active": False}, env=env)

    # No once-per-session block, no shadow would-block marker, no "review the
    # idioms below" content: the idiom lens is a scoped detector that runs
    # inside the async review job now, never in-turn Stop content on its own.
    assert out.get("decision") != "block"
    assert out == {}


def test_repeated_stops_stay_silent_no_once_per_session_marker(make_trusted_repo, tmp_path):
    # The legacy `.idiom_reviewed.<session>` once-per-session marker is gone
    # with the interrupt it gated -- confirm several Stops over the SAME
    # session stay silent rather than "unblocking" after a first block.
    repo, data_dir, sid, file_path, profile_dir, repo_id = make_trusted_repo(mode="enforce")
    env = _env(tmp_path)
    _seed_passing_test_run(tmp_path, repo_id, sid, env)
    _write_idioms(profile_dir)

    for i in range(3):
        _touch_edited_file(file_path, data_dir, sid, content=f"x = {i}\n")
        out = _run_stop({"session_id": sid, "cwd": str(repo), "stop_hook_active": False}, env=env)
        assert out.get("decision") != "block"


def test_idiom_review_false_excludes_idiom_lens_from_launched_job(make_trusted_repo, tmp_path):
    # The durable `enforcement.idiom_review: false` off-switch has no more
    # in-turn text to disclose into (a compliant/silent turn has nothing to
    # show), but it must still functionally exclude the idiom lens from the
    # job the scheduler launches -- verified directly against the JobRequest
    # rather than any rendered reason string.
    repo, data_dir, sid, file_path, profile_dir, repo_id = make_trusted_repo(mode="enforce")
    profile_dir.joinpath("config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce", "idiom_review": False}}),
        encoding="utf-8",
    )
    env = _env(tmp_path)
    _seed_passing_test_run(tmp_path, repo_id, sid, env)
    _touch_edited_file(file_path, data_dir, sid)
    _write_idioms(profile_dir)

    calls: list = []

    def fake_launch(request):
        calls.append(request)
        return False

    with patch("chameleon_mcp.stop.scheduler.launch_job", fake_launch):
        out = _run_stop({"session_id": sid, "cwd": str(repo), "stop_hook_active": False}, env=env)

    assert out.get("decision") != "block"
    assert len(calls) == 1
    assert "idiom" not in calls[0].lens_names
    assert set(calls[0].lens_names) == {"correctness", "duplication"}


def test_fails_open_on_malformed_state(make_trusted_repo, tmp_path):
    repo, data_dir, sid, file_path, profile_dir, repo_id = make_trusted_repo(mode="enforce")
    _write_idioms(profile_dir)
    from chameleon_mcp.enforcement import _state_path

    _state_path(data_dir, sid).write_text("{ not json", encoding="utf-8")

    out = _run_stop(
        {"session_id": sid, "cwd": str(repo), "stop_hook_active": False}, env=_env(tmp_path)
    )
    assert out.get("decision") != "block"
