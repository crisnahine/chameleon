"""Unit tests for the Bash-mutation marking path in hook_helper.

posttool_recorder, after the HMAC exec-log append, runs a pure-regex pre-filter
over the command. When it finds a single-literal-target TS/Ruby write under a
trusted repo, it lints the on-disk file with the same in-process orchestrator
posttool_verify uses and records the result into EnforcementState.files, so the
existing Stop backstop re-lints and can block on an unresolved hard violation.

These tests drive _record_bash_write_mutations directly (the heavy resolution is
mocked) to assert the wiring: the trust gate, the language filter, the
hard-class partition feeding blockable_unresolved, and fail-open behavior. The
recorder envelope itself stays {} regardless.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _trusted_rec() -> MagicMock:
    rec = MagicMock()
    rec.grants_root.return_value = True
    return rec


def _untrusted_rec() -> MagicMock:
    rec = MagicMock()
    rec.grants_root.return_value = False
    return rec


def _write_ts(repo: Path, rel: str, body: str = "export const x = 1\n") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _call_mark(command: str, cwd: Path, session: str = "s1") -> None:
    from chameleon_mcp.hook_helper import _record_bash_write_mutations

    _record_bash_write_mutations(command, cwd, session)


def test_trusted_ts_write_records_violation(tmp_path: Path):
    """A trusted in-repo TS write with a hard violation lands in enforcement
    state with blockable_unresolved set, so the Stop backstop will re-lint it."""
    repo = tmp_path / "repo"
    target = _write_ts(repo, "src/a.ts")

    hard_v = {"rule": "phantom-import", "message": "x", "line": 1}
    recorded: dict = {}

    def _fake_record_violation(fs, *, now, archetype, hard_class=False):
        recorded["archetype"] = archetype
        recorded["hard_class"] = hard_class
        fs.blockable_unresolved = bool(hard_class)

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch(
            "chameleon_mcp.tools.get_archetype",
            return_value={"data": {"archetype": "ts-service"}},
        ),
        patch(
            "chameleon_mcp.hook_helper._lint_file_in_process",
            return_value=[hard_v],
        ),
        patch(
            "chameleon_mcp.enforcement_calibration.active_block_rules",
            return_value={"phantom-import"},
        ),
        patch(
            "chameleon_mcp.violation_class.hard_class_violations",
            return_value=[hard_v],
        ),
        patch("chameleon_mcp.violation_class.ignored_rules", return_value=None),
        patch(
            "chameleon_mcp.enforcement.record_violation",
            side_effect=_fake_record_violation,
        ),
        patch("chameleon_mcp.enforcement.save_state") as save_state,
    ):
        _call_mark(f"cat > {target}", repo)

    assert recorded["archetype"] == "ts-service"
    assert recorded["hard_class"] is True
    save_state.assert_called_once()


def test_untrusted_profile_is_skipped(tmp_path: Path):
    """A never-trusted profile must not drive enforcement state — no lint, no save."""
    repo = tmp_path / "repo"
    target = _write_ts(repo, "src/a.ts")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_untrusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch("chameleon_mcp.hook_helper._lint_file_in_process") as lint,
        patch("chameleon_mcp.enforcement.save_state") as save_state,
    ):
        _call_mark(f"cat > {target}", repo)

    lint.assert_not_called()
    save_state.assert_not_called()


def test_missing_trust_record_is_skipped(tmp_path: Path):
    repo = tmp_path / "repo"
    target = _write_ts(repo, "src/a.ts")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=None),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch("chameleon_mcp.hook_helper._lint_file_in_process") as lint,
        patch("chameleon_mcp.enforcement.save_state") as save_state,
    ):
        _call_mark(f"cat > {target}", repo)

    lint.assert_not_called()
    save_state.assert_not_called()


def test_non_ts_ruby_target_skipped_before_stat(tmp_path: Path):
    """A write to a non-source file never resolves an archetype, never lints."""
    repo = tmp_path / "repo"
    (repo).mkdir()
    (repo / "out.log").write_text("data\n", encoding="utf-8")

    with (
        patch("chameleon_mcp.hook_helper._lint_file_in_process") as lint,
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
    ):
        _call_mark(f"cat > {repo / 'out.log'}", repo)

    lint.assert_not_called()


def test_no_write_target_does_not_load_profile(tmp_path: Path):
    """A write-free command bails before any profile/trust resolution."""
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root") as find_root,
        patch("chameleon_mcp.profile.trust.trust_state_for") as trust,
    ):
        _call_mark("pytest -q tests/", tmp_path)

    find_root.assert_not_called()
    trust.assert_not_called()


def test_target_outside_repo_skipped(tmp_path: Path):
    """A target that resolves outside its own repo root is refused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    target = other / "a.ts"
    target.write_text("export const x = 1\n", encoding="utf-8")

    with (
        # find_repo_root returns a DIFFERENT root than where the file lives, so
        # relative_to raises and the file is skipped.
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch("chameleon_mcp.hook_helper._lint_file_in_process") as lint,
    ):
        _call_mark(f"cat > {target}", tmp_path)

    lint.assert_not_called()


def test_clean_file_recorded_clean_for_crossfile(tmp_path: Path):
    """A clean written file is recorded CLEAN (never as a violation).

    The Stop crossfile-existence pass iterates state.files and re-reads content
    live, so a Bash-written file that removed an export must be present there or
    its break is invisible -- the Edit-tool path records clean files the same way.
    Recording is clean: record_violation is never called, so nothing is armed.
    """
    repo = tmp_path / "repo"
    target = _write_ts(repo, "src/a.ts")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch(
            "chameleon_mcp.tools.get_archetype",
            return_value={"data": {"archetype": "ts-service"}},
        ),
        patch("chameleon_mcp.hook_helper._lint_file_in_process", return_value=[]),
        patch("chameleon_mcp.enforcement.record_violation") as record_violation,
        patch("chameleon_mcp.enforcement.save_state") as save_state,
    ):
        _call_mark(f"cat > {target}", repo)

    save_state.assert_called()
    record_violation.assert_not_called()


def test_inline_ignore_clears_hard_class(tmp_path: Path):
    """An inline chameleon-ignore on the written file drops the hard rule, so the
    recorded violation is advisory (hard_class False)."""
    repo = tmp_path / "repo"
    target = _write_ts(
        repo,
        "src/a.ts",
        body="// chameleon-ignore phantom-import\nexport const x = 1\n",
    )
    hard_v = {"rule": "phantom-import", "message": "x", "line": 1}
    recorded: dict = {}

    def _fake_record_violation(fs, *, now, archetype, hard_class=False):
        recorded["hard_class"] = hard_class

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch(
            "chameleon_mcp.tools.get_archetype",
            return_value={"data": {"archetype": "ts-service"}},
        ),
        patch("chameleon_mcp.hook_helper._lint_file_in_process", return_value=[hard_v]),
        patch(
            "chameleon_mcp.enforcement_calibration.active_block_rules",
            return_value={"phantom-import"},
        ),
        patch(
            "chameleon_mcp.enforcement.record_violation",
            side_effect=_fake_record_violation,
        ),
        patch("chameleon_mcp.enforcement.save_state"),
    ):
        _call_mark(f"cat > {target}", repo)

    # The hard rule was inline-ignored, so the violation is advisory only.
    assert recorded["hard_class"] is False


def test_lint_exception_fails_open(tmp_path: Path):
    """A sub-lint raising must not crash the marker; nothing is recorded."""
    repo = tmp_path / "repo"
    target = _write_ts(repo, "src/a.ts")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_trusted_rec()),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="rid"),
        patch(
            "chameleon_mcp.tools.get_archetype",
            return_value={"data": {"archetype": "ts-service"}},
        ),
        patch(
            "chameleon_mcp.hook_helper._lint_file_in_process",
            side_effect=RuntimeError("boom"),
        ),
        patch("chameleon_mcp.enforcement.save_state") as save_state,
    ):
        # Must not raise.
        _call_mark(f"cat > {target}", repo)

    save_state.assert_not_called()


# --- recorder-level wiring --------------------------------------------------


def _run_recorder(payload: dict, env: dict) -> dict:
    captured: list[str] = []

    def _w(s: str) -> None:
        captured.append(s)

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as out,
        patch.dict(os.environ, env, clear=False),
    ):
        out.write = _w
        from chameleon_mcp.hook_helper import posttool_recorder

        assert posttool_recorder() == 0
    s = "".join(captured).strip()
    return json.loads(s) if s else {}


def test_recorder_invokes_marker_for_bash(tmp_path: Path):
    """The recorder calls the marker after the exec-log append and still emits {}."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
    }
    with patch("chameleon_mcp.hook_helper._record_bash_write_mutations") as marker:
        result = _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cat > a.ts"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": str(repo),
            },
            env,
        )
    assert result == {}
    marker.assert_called_once()
    assert marker.call_args.args[0] == "cat > a.ts"


def test_recorder_skips_marker_when_verify_disabled(tmp_path: Path):
    """CHAMELEON_VERIFY=0 suppresses the Bash-mutation marking, same as the
    Edit verifier, while the exec-log append still runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
        "CHAMELEON_VERIFY": "0",
    }
    with patch("chameleon_mcp.hook_helper._record_bash_write_mutations") as marker:
        result = _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cat > a.ts"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": str(repo),
            },
            env,
        )
    assert result == {}
    marker.assert_not_called()


def test_recorder_marker_exception_fails_open(tmp_path: Path):
    """If the marker raises, the recorder still emits {} and returns 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "CHAMELEON_PLUGIN_DATA": str(tmp_path),
        "CHAMELEON_HMAC_KEY_PATH": str(tmp_path / "hmac.key"),
        "TMPDIR": str(tmp_path),
    }
    with patch(
        "chameleon_mcp.hook_helper._record_bash_write_mutations",
        side_effect=RuntimeError("boom"),
    ):
        result = _run_recorder(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cat > a.ts"},
                "tool_response": {"returnCode": 0},
                "session_id": "s1",
                "cwd": str(repo),
            },
            env,
        )
    assert result == {}
