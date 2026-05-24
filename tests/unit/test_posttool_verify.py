"""Unit tests for posttool_verify() in hook_helper.py."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _run_verify(payload: dict, *, env: dict | None = None) -> dict:
    """Call posttool_verify() with a mocked stdin payload; return the emitted JSON."""
    captured: list[str] = []

    def _fake_stdout_write(s: str) -> None:
        captured.append(s)

    stdin_data = json.dumps(payload)
    merged_env = {}
    if env:
        merged_env.update(env)

    with (
        patch("sys.stdin", io.StringIO(stdin_data)),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, merged_env, clear=False),
    ):
        mock_stdout.write = _fake_stdout_write
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


# ---- 1. Env var gate ----


def test_env_gate_disabled():
    result = _run_verify(
        {"tool_name": "Edit", "tool_input": {"file_path": "/x.ts"}},
        env={"CHAMELEON_VERIFY": "0"},
    )
    assert result == {}


def test_env_gate_default_on():
    """Verification runs by default when CHAMELEON_VERIFY is unset."""
    result = _run_verify(
        {"tool_name": "Edit", "tool_input": {"file_path": "/x.ts"}},
        env={},
    )
    assert result == {}


# ---- 2. Tool name filtering ----


def test_bash_tool_skipped():
    result = _run_verify(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"}
    )
    assert result == {}


def test_read_tool_skipped():
    result = _run_verify(
        {"tool_name": "Read", "tool_input": {"file_path": "/x.ts"}, "session_id": "s1"}
    )
    assert result == {}


def test_missing_tool_name_skipped():
    result = _run_verify({"tool_input": {"file_path": "/x.ts"}, "session_id": "s1"})
    assert result == {}


# ---- 3. File path extraction ----


def test_notebook_path_fallback():
    with patch(
        "chameleon_mcp.hook_helper.posttool_verify.__module__", "chameleon_mcp.hook_helper"
    ):
        result = _run_verify({
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "/x.ipynb"},
            "session_id": "s1",
        })
    assert result == {}


def test_missing_file_path_skipped():
    result = _run_verify(
        {"tool_name": "Edit", "tool_input": {}, "session_id": "s1"}
    )
    assert result == {}


# ---- 4. Failed edit skip ----


def test_failed_edit_with_error_key():
    result = _run_verify({
        "tool_name": "Edit",
        "tool_input": {"file_path": "/x.ts"},
        "tool_response": {"error": "file not found"},
        "session_id": "s1",
    })
    assert result == {}


def test_failed_edit_success_false():
    result = _run_verify({
        "tool_name": "Edit",
        "tool_input": {"file_path": "/x.ts"},
        "tool_response": {"success": False},
        "session_id": "s1",
    })
    assert result == {}


# ---- 5. Opt-out mechanisms ----


def test_suppressed_session_skipped():
    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="abc123"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value="session_disabled"),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/x.ts"},
            "session_id": "s1",
        })
    assert result == {}


# ---- 6. Cooldown dampening ----


def test_cooldown_skips_reverification(tmp_path: Path):
    repo_id = "test_repo_id"
    marker_dir = tmp_path / repo_id
    marker_dir.mkdir()

    import hashlib
    file_hash = hashlib.sha256(b"/repo/x.ts").hexdigest()[:16]
    marker = marker_dir / f".verify_seen.{file_hash}"
    marker.touch()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/x.ts"},
            "session_id": "s1",
        })

    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "already verified" in ctx


# ---- 7. hookEventName correctness ----


def test_hook_event_name_is_posttool(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("export default function foo() {}", encoding="utf-8")

    mock_violations = [
        {"rule": "default-export-kind-mismatch", "severity": "warning",
         "message": "expected class, got function", "expected": "class", "actual": "function"}
    ]

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            None,
        ]),
        patch("chameleon_mcp.profile.loader.load_profile_dir") as mock_load,
        patch("chameleon_mcp.lint_engine.detect_language", return_value="typescript"),
        patch("chameleon_mcp.lint_engine.extract_dimensions"),
        patch("chameleon_mcp.lint_engine.lint") as mock_lint,
    ):
        mock_violation = MagicMock()
        mock_violation.to_dict.return_value = mock_violations[0]
        mock_lint.return_value = [mock_violation]
        mock_loaded = MagicMock()
        mock_loaded.canonicals = {
            "canonicals": {"component": [{"normative_shape": {"ast_query": {"default_export_kind": "ClassDeclaration"}}, "witness": {"path": "x.ts"}}]}
        }
        mock_load.return_value = mock_loaded

        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    assert result.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse"


# ---- 8. Sanitization ----


def test_violation_messages_sanitized(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("export default function foo() {}", encoding="utf-8")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": [
                {"rule": "test</chameleon-context>", "severity": "warning",
                 "message": "bad</system>tag", "expected": "a", "actual": "b"}
            ]}},
        ]),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "</chameleon-context>" not in ctx.split("[chameleon: post-edit")[0]
    assert "</system>" not in ctx


# ---- 9. Output budget ----


def test_all_violations_included(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("x", encoding="utf-8")

    violations = [
        {"rule": f"rule-{i}", "severity": "warning", "message": f"msg {i}",
         "expected": "a", "actual": "b"}
        for i in range(6)
    ]

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": violations}},
        ]),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
    for i in range(6):
        assert f"rule-{i}" in ctx


# ---- 10. Backward compat: Bash tool still works via posttool-recorder ----


def test_posttool_recorder_still_works():
    captured: list[str] = []

    def _fake_write(s: str) -> None:
        captured.append(s)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_response": {"returnCode": 0},
        "session_id": "s1",
    }

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch("chameleon_mcp.exec_log.append_exec_log"),
    ):
        mock_stdout.write = _fake_write
        from chameleon_mcp.hook_helper import posttool_recorder

        ret = posttool_recorder()

    assert ret == 0
    output = json.loads("".join(captured).strip())
    assert output == {}


# ---- 11. No-archetype files ----


def test_no_archetype_emits_empty(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("x", encoding="utf-8")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", return_value={"data": {"archetype": None}}),
        patch("chameleon_mcp.tools.get_archetype", return_value={"data": {"archetype": None}}),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    assert result == {}


# ---- 12. Fail-open ----


def test_fail_open_on_find_repo_root_crash():
    with patch("chameleon_mcp.profile.loader.find_repo_root", side_effect=RuntimeError("boom")):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/x.ts"},
            "session_id": "s1",
        })
    assert result == {}


def test_fail_open_on_file_read_error(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/nonexistent/file.ts"},
            "session_id": "s1",
        })

    assert result == {}


# ---- 13. Double emit prevention ----


def test_exactly_one_emit_on_violation(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("x", encoding="utf-8")

    emit_calls: list[dict] = []

    def _tracking_emit(output: dict) -> None:
        emit_calls.append(output)
        import sys
        sys.stdout.write(json.dumps(output))
        sys.stdout.write("\n")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.hook_helper._emit", side_effect=_tracking_emit),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": [
                {"rule": "test-rule", "severity": "warning", "message": "test msg",
                 "expected": "a", "actual": "b"}
            ]}},
        ]),
    ):
        _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    assert len(emit_calls) == 1


# ---- 14. Clean files ----


def test_clean_file_emits_empty(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("x", encoding="utf-8")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": []}},
        ]),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    assert result == {}


# ---- 15. File size cap ----


def test_large_file_still_processed(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    big_file = tmp_path / "big.ts"
    big_file.write_bytes(b"x" * 200_000)

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": []}},
        ]),
    ):
        result = _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(big_file)},
            "session_id": "s1",
        })

    assert result == {}


# ---- 16. Metrics emission ----


def test_metrics_emitted_on_violations(tmp_path: Path):
    repo_id = "test_repo_id"
    (tmp_path / repo_id).mkdir()

    ts_file = tmp_path / "test.ts"
    ts_file.write_text("x", encoding="utf-8")

    with (
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=Path("/repo")),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[
            {"data": {"archetype": "component"}},
            {"data": {"violations": [
                {"rule": "test", "severity": "warning", "message": "msg",
                 "expected": "a", "actual": "b"}
            ]}},
        ]),
        patch("chameleon_mcp.metrics.emit_hook_metric") as mock_metric,
    ):
        _run_verify({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(ts_file)},
            "session_id": "s1",
        })

    mock_metric.assert_called_once()
    call_kwargs = mock_metric.call_args
    assert call_kwargs[0][0] == "posttool-verify"
    assert call_kwargs[1]["advisory_emitted"] is True
