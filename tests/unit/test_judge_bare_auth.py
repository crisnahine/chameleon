"""Functional --bare auth probe for the judge spawn.

On current CLIs ``claude --bare`` no longer inherits OAuth/keychain
credentials: the spawn exits nonzero with a not-logged-in message while the
identical spawn without the flag works. Flag EXISTENCE is therefore not
enough to use it -- the first bare spawn doubles as a functional auth probe,
an auth-shaped failure falls back to a plain spawn within the same call, and
the outcome is cached per process plus a TTL marker in the data dir so each
session pays the discovery at most once.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from chameleon_mcp import judge


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_NOT_LOGGED_IN = _Proc(1, stdout="", stderr="Not logged in · Please run /login")


@pytest.fixture(autouse=True)
def _reset_bare_caches(monkeypatch, tmp_path):
    """Force flag support on, clear the auth caches, isolate the marker dir."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(judge, "_BARE_SUPPORTED", True)
    monkeypatch.setattr(judge, "_BARE_AUTH_OK", None)
    # These tests pin the synchronous spawn shape; the detached-child flag
    # would silently switch the budget when the auth cache reads failed.
    monkeypatch.setattr(judge, "_RUNNING_DETACHED", False)


def _fake_run_auth_broken(calls):
    """A subprocess.run double where --bare loses auth and plain works."""

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        if "--bare" in args:
            return _NOT_LOGGED_IN
        return _Proc(0, stdout="stream")

    return run


@pytest.mark.real_judge_spawn
def test_not_logged_in_bare_falls_back_to_plain_spawn(tmp_path):
    calls: list = []
    with patch("subprocess.run", side_effect=_fake_run_auth_broken(calls)):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    assert len(calls) == 2
    assert "--bare" in calls[0][0]
    assert "--bare" not in calls[1][0]


@pytest.mark.real_judge_spawn
def test_fallback_spawn_keeps_chameleon_disable_isolation(tmp_path):
    # --bare was protecting against inherited plugin hooks; the plain fallback
    # must still set CHAMELEON_DISABLE=1 so the user's installed chameleon
    # no-ops in the reviewer session (no Stop-hook recursion).
    calls: list = []
    with patch("subprocess.run", side_effect=_fake_run_auth_broken(calls)):
        judge._spawn_reviewer_status("prompt", tmp_path)
    _, fallback_kwargs = calls[1]
    assert fallback_kwargs["env"]["CHAMELEON_DISABLE"] == "1"


@pytest.mark.real_judge_spawn
def test_bare_success_keeps_flag(tmp_path):
    calls: list = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _Proc(0, stdout="stream")

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    assert len(calls) == 1
    assert "--bare" in calls[0]
    assert judge._BARE_AUTH_OK is True


def _review_naming_auth() -> str:
    """A real --bare review (exit 0 stream-json) whose FINDINGS name auth phrases.

    The finding titles/descriptions contain "authentication error" and "not
    logged in", the exact strings the auth-loss regex matches -- so a
    phrase-only probe would discard this genuine review.
    """
    return json.dumps(
        {
            "type": "result",
            "result": json.dumps(
                [
                    {
                        "severity": "medium",
                        "file": "auth.py",
                        "line": 42,
                        "title": "authentication error path unhandled",
                        "description": "the user is not logged in branch is skipped",
                    }
                ]
            ),
        }
    )


@pytest.mark.real_judge_spawn
def test_bare_success_reviewing_auth_code_not_mistaken_for_auth_loss(tmp_path):
    # A genuine --bare review (exit 0) whose findings name auth phrases must NOT be
    # read as an auth-loss body. A real review carries a findings array; an
    # auth-error body carries none. Without the structural guard, a clean review of
    # auth / login code is discarded, --bare is disabled globally for 24h, and the
    # clamped retry can drop the turn's findings.
    review = _review_naming_auth()
    calls: list = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _Proc(0, stdout=review)

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (review, None)
    # One spawn only: no needless fallback, and --bare stays enabled.
    assert len(calls) == 1
    assert "--bare" in calls[0]
    assert judge._BARE_AUTH_OK is True


@pytest.mark.real_judge_spawn
def test_bare_auth_error_wrapped_in_stream_json_is_still_detected(tmp_path):
    # Robustness: even if a --bare auth failure is delivered as an EXIT-0
    # stream-json result whose body is the login message (not a plain body), it
    # carries no findings array, so it is still detected as auth loss and falls
    # back to a plain spawn -- the structural check, not a text-presence check, is
    # what makes this hold.
    wrapped = json.dumps({"type": "result", "result": "Not logged in. Please run /login"})
    calls: list = []

    def run(args, **kwargs):
        calls.append(list(args))
        if "--bare" in args:
            return _Proc(0, stdout=wrapped)
        return _Proc(0, stdout="[]")

    with patch("subprocess.run", side_effect=run):
        out, fail = judge._spawn_reviewer_status("prompt", tmp_path)

    assert (out, fail) == ("[]", None)
    assert len(calls) == 2
    assert "--bare" in calls[0]
    assert "--bare" not in calls[1]
    assert judge._BARE_AUTH_OK is False


@pytest.mark.real_judge_spawn
def test_auth_failure_cached_in_process_no_reprobe(tmp_path):
    calls: list = []
    fake = _fake_run_auth_broken(calls)
    with patch("subprocess.run", side_effect=fake):
        judge._spawn_reviewer_status("prompt", tmp_path)
        calls.clear()
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    # Second call: one spawn, no --bare attempt at all.
    assert len(calls) == 1
    assert "--bare" not in calls[0][0]


@pytest.mark.real_judge_spawn
def test_auth_failure_marker_survives_process_restart(tmp_path):
    calls: list = []
    fake = _fake_run_auth_broken(calls)
    with patch("subprocess.run", side_effect=fake):
        judge._spawn_reviewer_status("prompt", tmp_path)
        assert judge._bare_auth_marker_path().is_file()
        # Simulate a fresh process: only the in-memory cache is lost.
        judge._BARE_AUTH_OK = None
        calls.clear()
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    assert len(calls) == 1
    assert "--bare" not in calls[0][0]


@pytest.mark.real_judge_spawn
def test_stale_marker_expires_and_bare_is_reprobed(tmp_path):
    marker = judge._bare_auth_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(int(time.time()) - judge._BARE_AUTH_TTL_SECONDS - 10), encoding="utf-8")

    calls: list = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _Proc(0, stdout="stream")

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == ("stream", None)
    # Expired marker: --bare is tried again (and succeeds here).
    assert "--bare" in calls[0]
    assert not marker.is_file()


@pytest.mark.real_judge_spawn
def test_nonzero_exit_without_auth_shape_does_not_retry(tmp_path):
    calls: list = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _Proc(1, stdout="boom", stderr="some unrelated crash")

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (None, "spawn_nonzero_exit")
    assert len(calls) == 1
    # A non-auth failure must not poison the bare-auth cache.
    assert judge._BARE_AUTH_OK is None
    assert not judge._bare_auth_marker_path().is_file()


@pytest.mark.real_judge_spawn
def test_fallback_failure_still_maps_to_spawn_nonzero_exit(tmp_path):
    def run(args, **kwargs):
        if "--bare" in args:
            return _NOT_LOGGED_IN
        return _Proc(1, stdout="still broken")

    with patch("subprocess.run", side_effect=run):
        assert judge._spawn_reviewer_status("prompt", tmp_path) == (None, "spawn_nonzero_exit")
    # The auth outcome was still recorded so the next spawn skips --bare.
    assert judge._BARE_AUTH_OK is False
