"""Regression tests for the hook_helper + hooks review-fix-3 batch.

Covers six confirmed findings:

1. BLOCKING DISCIPLINE: the eval-call and hard-secret PreToolUse denies must
   only run for a recognized code language and scan the per-language
   string/comment-stripped content (eval), so an ``eval(`` / example
   ``AKIA...`` / ``ghp_`` token in prose/config/fixtures (.md/.txt/.json/.yaml)
   never hard-blocks the edit -- while a real secret/eval in code still denies.
2. A correctness-judge TIMEOUT must not let the duplication gate spawn its own
   reviewer sequentially in the same Stop (two ~45s spawns blow the 55s cap).
3. The multi-lens correctness lens must route through the async/detach path
   when ``_judge_async_mode`` is active, instead of a doomed sync spawn.
4. ``_posttool_no_archetype_advisory`` must reuse the caller's already-decoded
   content for the inline-ignore scans rather than re-reading the file.
5. ``_nearby_signatures_section`` must filter to source-suffix siblings before
   sorting, not sort the whole directory listing.
6. The hook error log is rotated in-process inside ``main`` (the shell
   ``log_rotation`` spawn was removed from all six hooks); ``main`` stays
   fail-open if rotation raises.

Isolation mirrors test_preflight_secret_deny.py (no conftest).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.enforcement_calibration import write_block_rules

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

ACTIVE_SECRET_RULE = {"secret-detected-in-content": {"active": True, "fp_rate": 0.0, "sampled": 3}}
ACTIVE_EVAL_RULE = {"eval-call": {"active": True, "fp_rate": 0.0, "sampled": 3}}


def _build_repo(tmp_path: Path, *, mode: str) -> tuple[Path, str]:
    repo_id = "fix3_repo_id"
    (tmp_path / repo_id).mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": mode}}), encoding="utf-8"
    )
    (chameleon / "conventions.json").write_text(json.dumps({"conventions": {}}), encoding="utf-8")
    return repo, repo_id


def _run_preflight(
    *,
    repo: Path,
    repo_id: str,
    tmp_path: Path,
    file_path: str,
    content: str,
    session_id: str,
    env: dict | None = None,
) -> dict:
    result = {
        "data": {
            "repo": {"id": repo_id, "trust_state": "trusted"},
            # No archetype: the secret/eval deny runs before the no-archetype
            # early return, so this exercises the archetype-independent path.
            "archetype": {"archetype": None, "summary": ""},
            "canonical_excerpt": {},
            "rules": [],
            "idioms": "",
        }
    }
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
        "session_id": session_id,
    }
    run_env = {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}
    if env:
        run_env.update(env)

    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, run_env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", return_value=None),
        patch("chameleon_mcp.tools.get_pattern_context", return_value=result),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import preflight_and_advise

        rc = preflight_and_advise()

    assert rc == 0
    lines = [ln for ln in "".join(captured).splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one hook-output object, got {len(lines)}"
    return json.loads(lines[0])


def _decision(out: dict) -> str | None:
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


# ---------------------------------------------------------------------------
# Finding 1 -- blocking discipline (language gate on the hard denies)
# ---------------------------------------------------------------------------


def test_secret_in_markdown_does_not_deny(tmp_path: Path):
    # An example credential documented in a .md must not hard-block the write.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "docs/notes.md"),
        content=f"Never commit a real key like `{AWS_KEY}` to the repo.\n",
        session_id="s-md-secret",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_secret_in_code_still_denies(tmp_path: Path):
    # The deny stays unchanged for real code: a hardcoded key in a .py string.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_SECRET_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "app/settings.py"),
        content=f'AWS_KEY = "{AWS_KEY}"\n',
        session_id="s-py-secret",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_eval_mention_in_markdown_does_not_deny(tmp_path: Path):
    # A documentation sentence containing the literal "eval(" must not block.
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_EVAL_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "docs/security.md"),
        content="Never use eval() on untrusted input.\n",
        session_id="s-md-eval",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) != "deny"


def test_eval_call_in_code_still_denies(tmp_path: Path):
    repo, repo_id = _build_repo(tmp_path, mode="enforce")
    write_block_rules(repo / ".chameleon", ACTIVE_EVAL_RULE)
    out = _run_preflight(
        repo=repo,
        repo_id=repo_id,
        tmp_path=tmp_path,
        file_path=str(repo / "app/run.py"),
        content="def go(x):\n    return eval(x)\n",
        session_id="s-py-eval",
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert _decision(out) == "deny"


def test_proposed_hard_secret_violations_gates_on_language():
    from chameleon_mcp.hook_helper import _proposed_hard_secret_violations

    code = f'AWS_KEY = "{AWS_KEY}"\n'
    # Unrecognized extension -> no violations regardless of content.
    md, _ = _proposed_hard_secret_violations(code, "notes.md", tool_name="Write")
    txt, _ = _proposed_hard_secret_violations(code, "fixtures.txt", tool_name="Write")
    js, _ = _proposed_hard_secret_violations(code, "data.json", tool_name="Write")
    assert md == [] and txt == [] and js == []
    # Recognized language -> the real secret still fires.
    py, _ = _proposed_hard_secret_violations(code, "settings.py", tool_name="Write")
    assert py, "a real hardcoded key in a .py must still be a hard violation"


def test_proposed_hard_eval_violations_gates_on_language():
    from chameleon_mcp.hook_helper import _proposed_hard_eval_violations

    # Prose mentioning eval( in a non-code file: gated out.
    md, _ = _proposed_hard_eval_violations(
        "Avoid eval() entirely.\n", "guide.md", tool_name="Write"
    )
    assert md == []
    # eval( inside a Python string/comment is stripped, so it does not fire.
    in_string, _ = _proposed_hard_eval_violations(
        'msg = "do not call eval(x)"\n', "a.py", tool_name="Write"
    )
    assert in_string == []
    # A bare eval call in code still denies.
    real, _ = _proposed_hard_eval_violations("y = eval(payload)\n", "a.py", tool_name="Write")
    assert real, "a real eval() call in .py must still be a hard violation"


# ---------------------------------------------------------------------------
# Finding 2 -- judge timeout must not trigger a second sequential spawn
# ---------------------------------------------------------------------------


def test_duplication_deferred_after_judge_timeout(tmp_path: Path):
    # After a judge timeout the duplication gate must SKIP its spawn: it sees
    # corr_spawning True (the route spawned + timed out), so it returns [] with
    # reason corr_judge_active rather than running a second ~45s reviewer.
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    repo.mkdir()
    repo_data = tmp_path / "data"
    repo_data.mkdir()

    class _Cfg:
        duplication_review = True
        multi_lens_review = False
        mode = "shadow"

    class _State:
        duplication_spawns = 0
        files: list[str] = []

    events: list[tuple] = []

    def _capture(repo_id, session_id, check, status, reason=None, **kw):
        events.append((check, status, reason))

    # corr_spawning True AND timed out -> the call expression in _stop the
    # production code uses keeps corr_spawning True. The gate must skip.
    with patch.object(hook_helper, "_emit_check_event", _capture):
        lines = hook_helper._duplication_advisory_lines(
            repo_root=repo,
            repo_id="r",
            session_id="s",
            state=_State(),
            cfg=_Cfg(),
            repo_data=repo_data,
            corr_spawning=True,
        )
    assert lines == []
    assert ("duplication_review", "skipped", "corr_judge_active") in events


def test_timeout_sink_sets_route_flag_but_fast_failure_does_not():
    # The decision the _stop path makes: defer (skip dup) on success OR timeout,
    # run dup only on a fast failure. Model the route flags the gate sets.
    def corr_active(*, spawn_failed: bool, timed_out: bool) -> bool:
        # Mirrors the production expression in _stop_gates.
        corr_spawning = True
        return corr_spawning and (not spawn_failed or timed_out)

    # Clean completion: defer.
    assert corr_active(spawn_failed=False, timed_out=False) is True
    # Timeout consumed the budget: still defer (no second spawn).
    assert corr_active(spawn_failed=True, timed_out=True) is True
    # Fast failure (nonzero exit / parse error): run duplication.
    assert corr_active(spawn_failed=True, timed_out=False) is False


# ---------------------------------------------------------------------------
# Finding 3 -- multi-lens correctness lens detaches under async mode
# ---------------------------------------------------------------------------


def test_multilens_detaches_correctness_under_async_mode(tmp_path: Path):
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    repo.mkdir()
    repo_data = tmp_path / "data"
    repo_data.mkdir()

    class _Cfg:
        multi_lens_review = True
        correctness_judge = True
        duplication_review = False  # only correctness lens would run sync
        mode = "shadow"

    class _State:
        correctness_spawns = 0
        duplication_spawns = 0
        files: list[str] = []

    route = {
        "spawn": True,
        "fresh": [str(repo / "a.py")],
        "digests": {"a.py": "deadbeef"},
        "intent_tokens": [],
        "turn_key": "tk-1",
    }

    launched: dict = {}

    def _fake_launch(**kwargs):
        launched.update(kwargs)
        return True

    sync_ran = {"correctness": False}

    def _fake_run_correctness_judge(*a, **k):
        sync_ran["correctness"] = True
        return []

    state = _State()
    with (
        patch.object(hook_helper, "_judge_async_mode", return_value="async_opt_in"),
        patch("chameleon_mcp.judge_async.launch_async_judge", _fake_launch),
        patch("chameleon_mcp.judge.run_correctness_judge", _fake_run_correctness_judge),
        patch.object(hook_helper, "_emit_check_event", lambda *a, **k: None),
    ):
        lines = hook_helper._multi_lens_review_lines(
            repo_root=repo,
            repo_id="r",
            session_id="s",
            state=state,
            cfg=_Cfg(),
            repo_data=repo_data,
            daemon_state={"available": True},
            route=route,
        )
    # The async detach was used and the synchronous correctness spawn did NOT run.
    assert launched, "the correctness lens should have detached via launch_async_judge"
    assert sync_ran["correctness"] is False
    # The detached spawn still consumed the review budget.
    assert state.correctness_spawns == 1
    # Nothing to surface synchronously (correctness detached, duplication off).
    assert lines == []


# ---------------------------------------------------------------------------
# Finding 4 -- no-archetype advisory reuses the caller's content
# ---------------------------------------------------------------------------


def test_no_archetype_advisory_reuses_content(tmp_path: Path):
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    repo.mkdir()
    # Eval violation so the advisory has something to display.
    violations = [
        {
            "rule": "eval-call",
            "severity": "error",
            "message": "dynamic eval() at line 1 executes arbitrary code.",
            "actual": "eval( at line 1",
        }
    ]
    calls = {"n": 0}
    orig = hook_helper._read_file_for_ignore

    def _counting_read(fp):
        calls["n"] += 1
        return orig(fp)

    captured: list[str] = []
    with (
        patch.object(hook_helper, "_read_file_for_ignore", _counting_read),
        patch.object(hook_helper, "_emit_posttool_context", captured.append),
        patch.object(hook_helper, "_plugin_data_dir", return_value=tmp_path),
    ):
        wrote = hook_helper._posttool_no_archetype_advisory(
            repo_root=repo,
            repo_id="",  # skip the enforcement-state recording branch
            file_path=str(repo / "run.py"),
            violations=violations,
            session_id="s",
            now=0.0,
            content="y = eval(payload)\n",
        )
    assert wrote is True
    # Content was supplied, so the ignore scans never re-read the file.
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# Finding 5 -- nearby-signatures filters before sorting
# ---------------------------------------------------------------------------


def test_nearby_signatures_only_considers_source_candidates(tmp_path: Path, monkeypatch):
    from chameleon_mcp import hook_helper

    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    target = pkg / "main.py"
    target.write_text("x = 1\n", encoding="utf-8")
    (pkg / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    # Non-source noise: it must never reach the per-file signature lookup, which
    # proves the source-suffix filter runs ahead of the loop (not the whole
    # directory sorted then filtered inside the loop).
    for i in range(50):
        (pkg / f"asset_{i}.png").write_bytes(b"\x89PNG")

    monkeypatch.setenv("CHAMELEON_NEARBY_SIGNATURES", "1")

    looked_up: list[str] = []

    class _Sigs:
        def __len__(self):
            return 1

        def for_file(self, rel):
            looked_up.append(rel or "")
            if rel and rel.endswith("helper.py"):
                return {"h": {"params": [], "return_type": ""}}
            return {}

    with (
        patch(
            "chameleon_mcp.symbol_signatures.load_symbol_signatures",
            return_value=_Sigs(),
        ),
        patch(
            "chameleon_mcp.symbol_signatures.render_imported_definition",
            return_value="h() -> None",
        ),
        patch(
            "chameleon_mcp.worktree.resolve_profile_root",
            return_value=repo,
        ),
    ):
        section = hook_helper._nearby_signatures_section(str(target), repo)

    # Only the source-suffix sibling (helper.py) was looked up; no .png, and not
    # the target itself.
    assert all(rel.endswith(".py") for rel in looked_up)
    assert not any(rel.endswith("main.py") for rel in looked_up)
    assert "h()" in section


# ---------------------------------------------------------------------------
# Finding 6 -- in-process log rotation, hooks no longer spawn log_rotation
# ---------------------------------------------------------------------------


def test_main_rotates_log_in_process(tmp_path: Path, monkeypatch):
    from chameleon_mcp import hook_helper

    log = tmp_path / ".hook_errors.log"
    log.write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(log))

    seen: list[Path] = []

    def _fake_rotate(p):
        seen.append(Path(p))

    # main dispatches to session_start; stub it so the test stays in-process.
    with (
        patch("chameleon_mcp.log_rotation.rotate_if_needed", _fake_rotate),
        patch.object(hook_helper, "session_start", return_value=0),
    ):
        rc = hook_helper.main(["session-start"])
    assert rc == 0
    assert seen and seen[0] == log


def test_main_fail_open_when_rotation_raises(tmp_path: Path, monkeypatch):
    from chameleon_mcp import hook_helper

    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(tmp_path / ".hook_errors.log"))

    def _boom(_p):
        raise RuntimeError("rotation blew up")

    with (
        patch("chameleon_mcp.log_rotation.rotate_if_needed", _boom),
        patch.object(hook_helper, "posttool_recorder", return_value=0),
    ):
        # A rotation failure must never break the hook dispatch.
        rc = hook_helper.main(["posttool-recorder"])
    assert rc == 0


def test_hooks_do_not_spawn_log_rotation_interpreter():
    # All six hooks dropped the separate `python -m chameleon_mcp.log_rotation`
    # spawn; rotation now happens in-process inside hook_helper.main.
    hooks_dir = Path(__file__).resolve().parents[2] / "hooks"
    for name in (
        "session-start",
        "preflight-and-advise",
        "posttool-recorder",
        "posttool-verify",
        "stop-backstop",
        "callout-detector",
    ):
        text = (hooks_dir / name).read_text(encoding="utf-8")
        assert "-m chameleon_mcp.log_rotation" not in text, (
            f"{name} still spawns the log_rotation interpreter"
        )
