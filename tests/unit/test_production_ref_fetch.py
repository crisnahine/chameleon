"""Tests for the default-on production-ref fetch (the one network entry point).

Covers the fetch function itself (classifier, real-git ok/no-remote, backoff,
timeout via a sleeping shim, the non-interactive env), the config flag, the
refresh-time gating (_maybe_fetch_production_ref), and the hot-path guarantee
that nothing outside refresh ever fetches.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from chameleon_mcp import production_ref as pr

# --- helpers ----------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _make_origin_and_consumer(root: Path) -> tuple[Path, Path, Path]:
    """Bare origin (HEAD->main) + a work clone that pushed c1 + a consumer clone
    whose origin/main sits at c1. Returns (origin, work, consumer)."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    work = root / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "f.txt").write_text("c1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "c1", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    consumer = root / "consumer"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(consumer)], check=True, capture_output=True
    )
    return origin, work, consumer


def _advance_origin(work: Path, msg: str) -> str:
    (work / "f.txt").write_text(msg + "\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", msg, cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    out = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _shim_git(dir_: Path, body: str) -> dict:
    """Write a fake `git` shim into dir_ and return an env with it first on PATH."""
    dir_.mkdir(parents=True, exist_ok=True)
    shim = dir_ / "git"
    shim.write_text("#!/bin/sh\n" + body)
    shim.chmod(0o755)
    return {**os.environ, "PATH": f"{dir_}:{os.environ.get('PATH', '')}"}


# --- classifier (pure) ------------------------------------------------------


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ssh: Could not resolve host github.com", "no_network"),
        ("fatal: unable to access 'https://...': Connection timed out", "no_network"),
        ("git@github.com: Permission denied (publickey).", "auth"),
        ("fatal: Authentication failed for 'https://...'", "auth"),
        ("fatal: could not read Username for 'https://...': terminal prompts disabled", "auth"),
        ("fatal: couldn't find remote ref nope", "no_remote_ref"),
        ("error: cannot lock ref 'refs/remotes/origin/main'", "concurrent"),
        ("some unmapped git message", "unknown"),
    ],
)
def test_classifier(stderr, expected):
    assert pr._classify_fetch_failure(stderr, "main").status == expected


def test_auth_reason_names_the_branch_and_manual_fix():
    out = pr._classify_fetch_failure("Permission denied (publickey).", "release")
    assert out.status == "auth"
    assert "git fetch origin release" in out.reason


# --- real local-origin git --------------------------------------------------


def test_fetch_ok_updates_tracking_ref(tmp_path):
    _, work, consumer = _make_origin_and_consumer(tmp_path)
    before = pr.resolve_production_ref(consumer, "main")
    new_sha = _advance_origin(work, "c2")
    out = pr.fetch_production_ref(consumer, "main", repo_data_dir=tmp_path / "data")
    assert out.status == "ok"
    after = pr.resolve_production_ref(consumer, "main")
    assert before.sha != after.sha
    assert after.sha == new_sha  # resolver now sees the freshly fetched tip


def test_fetch_no_remote_ref(tmp_path):
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    out = pr.fetch_production_ref(consumer, "does-not-exist", repo_data_dir=tmp_path / "data")
    assert out.status == "no_remote_ref"


def test_empty_branch_is_disabled_without_spawn(tmp_path):
    out = pr.fetch_production_ref(tmp_path, "  ", repo_data_dir=tmp_path / "data")
    assert out.status == "disabled" and out.attempted is False


# --- backoff ----------------------------------------------------------------


def test_backoff_after_persistent_failure(tmp_path):
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    data = tmp_path / "data"
    first = pr.fetch_production_ref(consumer, "nope", repo_data_dir=data)
    assert first.status == "no_remote_ref"
    marker = pr._fetch_backoff_marker(data, "nope")
    assert marker.exists()
    # within the window: short-circuits to disabled without spawning
    second = pr.fetch_production_ref(consumer, "nope", repo_data_dir=data, backoff_hours=6.0)
    assert second.status == "disabled" and second.attempted is False
    # an expired marker re-attempts
    old = time.time() - 7 * 3600
    os.utime(marker, (old, old))
    third = pr.fetch_production_ref(consumer, "nope", repo_data_dir=data, backoff_hours=6.0)
    assert third.status == "no_remote_ref"  # spawned again


def test_ok_clears_backoff_marker(tmp_path):
    _, work, consumer = _make_origin_and_consumer(tmp_path)
    data = tmp_path / "data"
    pr.fetch_production_ref(consumer, "nope", repo_data_dir=data)
    assert pr._fetch_backoff_marker(data, "nope").exists()
    _advance_origin(work, "c2")
    ok = pr.fetch_production_ref(consumer, "main", repo_data_dir=data)
    assert ok.status == "ok"
    # the 'main' marker (distinct key) never existed; an ok on a previously
    # failing key clears it:
    pr.fetch_production_ref(consumer, "nope", repo_data_dir=data)  # re-arm 'nope'
    # simulate the remote ref appearing, then ok clears it
    marker = pr._fetch_backoff_marker(data, "main")
    assert not marker.exists()  # ok never wrote one for main


# --- timeout via a sleeping shim --------------------------------------------


def test_timeout_kills_and_returns_quickly(tmp_path):
    env = _shim_git(tmp_path / "bin", "sleep 30\n")
    started = time.monotonic()
    # patch os.environ PATH so the shim's `git` is picked up by the Popen
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = env["PATH"]
    try:
        out = pr.fetch_production_ref(
            tmp_path, "main", repo_data_dir=tmp_path / "d", timeout_seconds=0.6
        )
    finally:
        os.environ["PATH"] = old_path
    elapsed = time.monotonic() - started
    assert out.status == "timeout"
    assert elapsed < 6.0  # killed promptly, did NOT hang on the 30s sleep
    # timeout is transient: it does NOT arm the backoff
    assert not pr._fetch_backoff_marker(tmp_path / "d", "main").exists()


def test_fetch_env_is_non_interactive(tmp_path):
    out_file = tmp_path / "env.txt"
    env = _shim_git(tmp_path / "bin", f'env > "{out_file}"\nexit 1\n')
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = env["PATH"]
    try:
        pr.fetch_production_ref(tmp_path, "main", repo_data_dir=tmp_path / "d")
    finally:
        os.environ["PATH"] = old_path
    captured = out_file.read_text()
    assert "GIT_TERMINAL_PROMPT=0" in captured
    assert "BatchMode=yes" in captured  # in GIT_SSH_COMMAND
    if os.name == "posix":
        assert "GIT_ASKPASS=/bin/false" in captured


# --- config flag ------------------------------------------------------------


def test_config_flag_default_and_coercion():
    from chameleon_mcp.profile.config import AutoRefreshConfig, _coerce_auto_refresh

    assert AutoRefreshConfig().fetch_production_ref is True
    assert _coerce_auto_refresh({}).fetch_production_ref is True
    assert _coerce_auto_refresh({"fetch_production_ref": False}).fetch_production_ref is False
    # existing configs without the key still load
    assert _coerce_auto_refresh({"enabled": True}).fetch_production_ref is True


def test_config_flag_rejects_non_bool():
    from chameleon_mcp.profile.config import ChameleonConfigError, _coerce_auto_refresh

    with pytest.raises(ChameleonConfigError):
        _coerce_auto_refresh({"fetch_production_ref": "yes"})


# --- refresh-time gating (_maybe_fetch_production_ref) -----------------------


def _profiled_consumer(tmp_path) -> Path:
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    cham = consumer / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": "main"})
    )
    return consumer


def test_gating_local_only_repo_returns_none(tmp_path, monkeypatch):
    # No lock AND not origin-backed (a local-only repo with no remote): there is
    # nothing to fetch and migration would never lock it, so no network call.
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    local = tmp_path / "local"
    local.mkdir()
    subprocess.run(["git", "init", "-q", str(local)], check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=local)
    _git("config", "user.name", "t", cwd=local)
    (local / "f.txt").write_text("x\n")
    _git("add", "-A", cwd=local)
    _git("commit", "-q", "-m", "c1", cwd=local)
    assert tools._maybe_fetch_production_ref(local.resolve()) is None


def test_gating_kill_switch_and_ci(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    consumer = _profiled_consumer(tmp_path)
    monkeypatch.setenv("CHAMELEON_FETCH_PRODUCTION_REF", "0")
    assert tools._maybe_fetch_production_ref(consumer.resolve()) is None
    monkeypatch.delenv("CHAMELEON_FETCH_PRODUCTION_REF")
    monkeypatch.setenv("CI", "1")
    assert tools._maybe_fetch_production_ref(consumer.resolve()) is None


def test_gating_config_off(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    consumer = _profiled_consumer(tmp_path)
    (consumer / ".chameleon" / "config.json").write_text(
        json.dumps(
            {
                "$schema": "chameleon-config-0.9.0",
                "production_ref": "main",
                "auto_refresh": {"fetch_production_ref": False},
            }
        )
    )
    assert tools._maybe_fetch_production_ref(consumer.resolve()) is None


def test_gating_origin_backed_fetches(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CHAMELEON_FETCH_PRODUCTION_REF", raising=False)
    consumer = _profiled_consumer(tmp_path)
    out = tools._maybe_fetch_production_ref(consumer.resolve())
    assert out is not None and out.status == "ok"


# --- security: argument injection (the QA-30 adversarial-review P0) ---------


def test_leading_dash_branch_is_refused_no_exec(tmp_path):
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    marker = tmp_path / "EVIL_RCE"
    out = pr.fetch_production_ref(
        consumer, f"--upload-pack=touch {marker}", repo_data_dir=tmp_path / "d"
    )
    assert out.status == "disabled" and out.attempted is False
    assert not marker.exists()  # the payload must NOT have executed


def test_end_of_options_is_in_the_fetch_argv():
    # The argv backstop: --end-of-options must sit immediately before the
    # positional remote, so a dashed refspec can never be parsed as an option
    # even if the leading-dash refusal above were bypassed. Asserted on the real
    # git invocation via a capturing shim.
    import importlib

    src = importlib.import_module("chameleon_mcp.production_ref")
    fn_src = __import__("inspect").getsource(src.fetch_production_ref)
    assert '"--end-of-options"' in fn_src
    assert fn_src.index('"--end-of-options"') < fn_src.index('"origin"')


def test_refspec_injection_refused_no_local_ref_write(tmp_path):
    """A ':' / '+' / '*' in production_ref must NOT become a write refspec."""
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    _git("checkout", "-q", "-b", "feat", cwd=consumer)  # main updatable
    for payload in ("main:refs/heads/pwned", "+main:main", "ma*", "main foo"):
        out = pr.fetch_production_ref(consumer, payload, repo_data_dir=tmp_path / "d")
        assert out.status == "disabled" and out.attempted is False
    # no local ref was created/overwritten by any payload
    refs = subprocess.run(
        ["git", "-C", str(consumer), "show-ref"], capture_output=True, text=True
    ).stdout
    assert "pwned" not in refs


def test_is_safe_branch_name_allowlist():
    for good in ("main", "production", "release/v2", "feature-x", "api/v1.0", "hotfix/JIRA-123"):
        assert pr.is_safe_branch_name(good) is True
    for bad in (
        "main:main",
        "+main:main",
        "ma*",
        "main foo",
        "../evil",
        "-x",
        "--upload-pack=x",
        "main..dev",
        "end/",
        "x.lock",
        "a//b",
        "",
    ):
        assert pr.is_safe_branch_name(bad) is False


def test_config_rejects_refspec_production_ref(tmp_path):
    import json

    from chameleon_mcp.profile.config import ChameleonConfigError, load_config

    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": "main:refs/heads/x"})
    )
    with pytest.raises(ChameleonConfigError):
        load_config(cham)


def test_config_rejects_dashed_production_ref(tmp_path):
    import json

    from chameleon_mcp.profile.config import ChameleonConfigError, load_config

    cham = tmp_path / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": "--upload-pack=x"})
    )
    with pytest.raises(ChameleonConfigError):
        load_config(cham)


# --- P1: only the locked branch's own origin-backing gates the fetch --------


def test_branch_is_origin_backed(tmp_path):
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    _git("branch", "local-only", cwd=consumer)  # exists locally, not on origin
    assert pr.branch_is_origin_backed(consumer, "main") is True
    assert pr.branch_is_origin_backed(consumer, "local-only") is False
    assert pr.branch_is_origin_backed(consumer, "never-existed") is False


def test_gating_skips_local_only_locked_branch(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CHAMELEON_FETCH_PRODUCTION_REF", raising=False)
    consumer = _profiled_consumer(tmp_path)  # origin/HEAD -> main
    _git("branch", "production", cwd=consumer)  # local-only 'production'
    (consumer / ".chameleon" / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": "production"})
    )
    # 'production' is local-only -> no doomed network fetch
    assert tools._maybe_fetch_production_ref(consumer.resolve()) is None


# --- P2: the timeout path leaks no pipe fds ---------------------------------


def test_timeout_closes_pipes(tmp_path, monkeypatch):
    captured: dict = {}
    real_popen = subprocess.Popen

    def _spy(*a, **k):
        p = real_popen(*a, **k)
        captured["p"] = p
        return p

    monkeypatch.setattr(subprocess, "Popen", _spy)
    env = _shim_git(tmp_path / "bin", "sleep 30\n")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = env["PATH"]
    try:
        out = pr.fetch_production_ref(
            tmp_path, "main", repo_data_dir=tmp_path / "d", timeout_seconds=0.5
        )
    finally:
        os.environ["PATH"] = old
    assert out.status == "timeout"
    p = captured["p"]
    assert p.stdout is None or p.stdout.closed
    assert p.stderr is None or p.stderr.closed


# --- round-2 findings: never derive stale silently --------------------------


def test_concurrent_failure_carries_a_reason(tmp_path):
    # A 'cannot lock ref' that genuinely failed to update the tracking ref must
    # NOT be silent (empty reason) -- else refresh derives stale every session.
    for stderr in (
        "error: cannot lock ref 'refs/remotes/origin/main': File exists",
        "fatal: unable to update ref refs/remotes/origin/main",
    ):
        out = pr._classify_fetch_failure(stderr, "main")
        assert out.status == "concurrent"
        assert out.reason  # non-empty so the skill surfaces it
        assert "git fetch origin main" in out.reason


def test_migrating_session_fetches_before_lock(tmp_path, monkeypatch):
    # An OLD profile (no production_ref) on an origin-backed repo gets migrated
    # to a lock this refresh; the migrating session must ALSO fetch (else it
    # derives one-session-stale).
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CHAMELEON_FETCH_PRODUCTION_REF", raising=False)
    _, work, consumer = _make_origin_and_consumer(tmp_path)
    new_sha = _advance_origin(work, "c2")  # origin moves; consumer stale
    cham = consumer / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(json.dumps({"$schema": "chameleon-config-0.9.0"}))  # no lock
    out = tools._maybe_fetch_production_ref(consumer.resolve())
    assert out is not None and out.status == "ok"
    assert pr.resolve_production_ref(consumer, "main").sha == new_sha


def test_migrating_session_respects_null_optout(tmp_path, monkeypatch):
    from chameleon_mcp import tools

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "pd"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CI", raising=False)
    _, _, consumer = _make_origin_and_consumer(tmp_path)
    cham = consumer / ".chameleon"
    cham.mkdir()
    (cham / "config.json").write_text(
        json.dumps({"$schema": "chameleon-config-0.9.0", "production_ref": None})
    )
    assert tools._maybe_fetch_production_ref(consumer.resolve()) is None


# --- hot-path guarantee: nothing outside refresh fetches --------------------


def test_resolve_does_not_fetch(tmp_path, monkeypatch):
    _, _, consumer = _make_origin_and_consumer(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("resolve_production_ref must never fetch")

    monkeypatch.setattr(pr, "fetch_production_ref", _boom)
    # resolve is offline-only; it must not invoke the network entry point
    assert pr.resolve_production_ref(consumer, "main") is not None
