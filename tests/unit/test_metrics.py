"""Unit tests for chameleon_mcp.metrics — the per-hook-call jsonl emitter.

emit_hook_metric appends one compact JSON line to ${CHAMELEON_PLUGIN_DATA}/
metrics.jsonl. It is best-effort: all errors are swallowed so a logging
failure never breaks a hook. These tests pin the exact emitted JSON shape,
field types/coercions, append semantics, path resolution, the rotate_if_needed
call, and the graceful-failure contract.

Isolation: there is no conftest in this suite, so each test resets the relevant
env (CHAMELEON_PLUGIN_DATA) inline via monkeypatch / patch.dict and writes only
under tmp_path. metrics.py reads CHAMELEON_PLUGIN_DATA at *call* time (inside
_metrics_path), so no importlib.reload is required.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.metrics import _metrics_path, emit_hook_metric

# Exact key order the emitter writes, in the order the dict literal defines them.
EXPECTED_KEYS = [
    "ts",
    "hook",
    "repo_id",
    "elapsed_ms",
    "advisory_emitted",
    "suppression_reason",
    "fail_open",
    "trust_state",
    "archetype",
    "confidence",
]

TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _read_lines(plugin_data: Path) -> list[str]:
    text = (plugin_data / "metrics.jsonl").read_text(encoding="utf-8")
    return text.splitlines()


def test_metrics_path_honors_plugin_data_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    assert _metrics_path() == tmp_path / "metrics.jsonl"


def test_metrics_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("CHAMELEON_PLUGIN_DATA", raising=False)
    expected = Path.home() / ".local" / "share" / "chameleon" / "metrics.jsonl"
    assert _metrics_path() == expected


def test_metrics_path_empty_env_falls_back_to_default(monkeypatch):
    # An empty string is falsy, so the code must fall back to the home default.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", "")
    expected = Path.home() / ".local" / "share" / "chameleon" / "metrics.jsonl"
    assert _metrics_path() == expected


def test_emits_one_jsonl_line_with_trailing_newline(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    emit_hook_metric("preflight", elapsed_ms=12, repo_id="r1", advisory_emitted=True)

    raw = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    # Exactly one record, one trailing newline.
    assert raw.count("\n") == 1
    lines = raw.splitlines()
    assert len(lines) == 1


def test_record_has_exact_key_set_and_order(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    emit_hook_metric("preflight", elapsed_ms=5, repo_id="abc", advisory_emitted=False)

    line = _read_lines(tmp_path)[0]
    record = json.loads(line)
    # Exact key set, no extras, no missing.
    assert set(record.keys()) == set(EXPECTED_KEYS)
    # Insertion order is preserved by json and by dict — pin the literal order.
    assert list(record.keys()) == EXPECTED_KEYS


def test_full_field_values_and_types(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    emit_hook_metric(
        "posttool",
        elapsed_ms=42,
        repo_id="repo-xyz",
        advisory_emitted=True,
        suppression_reason="paused",
        fail_open=True,
        trust_state="trusted",
        archetype="controller",
        confidence="high",
    )

    record = json.loads(_read_lines(tmp_path)[0])
    assert record["hook"] == "posttool"
    assert record["repo_id"] == "repo-xyz"
    assert record["elapsed_ms"] == 42
    assert isinstance(record["elapsed_ms"], int)
    assert record["advisory_emitted"] is True
    assert record["suppression_reason"] == "paused"
    assert record["fail_open"] is True
    assert record["trust_state"] == "trusted"
    assert record["archetype"] == "controller"
    assert record["confidence"] == "high"
    # timestamp is a UTC, Z-suffixed ISO-ish string
    assert isinstance(record["ts"], str)
    assert TS_RE.fullmatch(record["ts"])


def test_optional_fields_default_to_null(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    # Only the required kwargs supplied; the rest take their defaults.
    emit_hook_metric("sessionstart", elapsed_ms=0, repo_id=None, advisory_emitted=False)

    record = json.loads(_read_lines(tmp_path)[0])
    assert record["repo_id"] is None
    assert record["suppression_reason"] is None
    assert record["fail_open"] is False
    assert record["trust_state"] is None
    assert record["archetype"] is None
    assert record["confidence"] is None
    assert record["advisory_emitted"] is False
    assert record["elapsed_ms"] == 0


def test_elapsed_ms_float_is_truncated_to_int(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    # int() truncates toward zero; 3.9 -> 3, not rounded to 4.
    emit_hook_metric("posttool", elapsed_ms=3.9, repo_id="r", advisory_emitted=False)

    record = json.loads(_read_lines(tmp_path)[0])
    assert record["elapsed_ms"] == 3
    assert isinstance(record["elapsed_ms"], int)


def test_advisory_emitted_truthy_int_coerced_to_bool(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    # bool() coercion: 0 -> false (not the literal 0), 1 -> true.
    emit_hook_metric("posttool", elapsed_ms=1, repo_id="r", advisory_emitted=0)
    emit_hook_metric("posttool", elapsed_ms=1, repo_id="r", advisory_emitted=1, fail_open=1)

    recs = [json.loads(line) for line in _read_lines(tmp_path)]
    assert recs[0]["advisory_emitted"] is False
    assert recs[1]["advisory_emitted"] is True
    assert recs[1]["fail_open"] is True


def test_compact_separators_no_spaces(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    emit_hook_metric("preflight", elapsed_ms=7, repo_id="r1", advisory_emitted=True)

    line = _read_lines(tmp_path)[0]
    # separators=(",", ":") -> no ", " and no ": " in the serialized line.
    assert ", " not in line
    assert ": " not in line
    assert '"hook":"preflight"' in line


def test_appends_across_multiple_calls(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    emit_hook_metric("a", elapsed_ms=1, repo_id="r", advisory_emitted=False)
    emit_hook_metric("b", elapsed_ms=2, repo_id="r", advisory_emitted=True)
    emit_hook_metric("c", elapsed_ms=3, repo_id="r", advisory_emitted=False)

    lines = _read_lines(tmp_path)
    assert len(lines) == 3
    hooks = [json.loads(line)["hook"] for line in lines]
    assert hooks == ["a", "b", "c"]


def test_creates_parent_directory_when_missing(monkeypatch, tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "data"
    assert not nested.exists()
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(nested))

    emit_hook_metric("preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    assert (nested / "metrics.jsonl").is_file()


def test_non_ascii_preserved_not_escaped(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    # ensure_ascii=False means the raw unicode bytes land on disk, not \uXXXX.
    emit_hook_metric(
        "preflight",
        elapsed_ms=1,
        repo_id="rëpo-ünïcode",
        advisory_emitted=True,
        archetype="señor",
    )

    raw = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8")
    assert "rëpo-ünïcode" in raw
    assert "señor" in raw
    assert "\\u" not in raw
    record = json.loads(raw)
    assert record["repo_id"] == "rëpo-ünïcode"
    assert record["archetype"] == "señor"


def test_rotate_if_needed_is_called_with_metrics_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    expected_path = tmp_path / "metrics.jsonl"

    with patch("chameleon_mcp.metrics.rotate_if_needed") as mock_rotate:
        emit_hook_metric("preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    mock_rotate.assert_called_once()
    (called_arg,) = mock_rotate.call_args.args
    assert Path(called_arg) == expected_path


def test_rotation_failure_is_swallowed_and_no_write(monkeypatch, tmp_path: Path):
    """If rotate_if_needed raises, emit must swallow it and not raise.

    Since rotation happens before the open(), a raising rotate also means no
    line is written — the whole body is inside one try/except.
    """
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    with patch(
        "chameleon_mcp.metrics.rotate_if_needed",
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise.
        emit_hook_metric("preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    assert not (tmp_path / "metrics.jsonl").exists()


def test_write_failure_is_swallowed(monkeypatch, tmp_path: Path):
    """An open() / write failure must be swallowed; emit returns None silently."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    with patch("chameleon_mcp.metrics.open", side_effect=OSError("disk full")):
        result = emit_hook_metric(
            "preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True
        )

    assert result is None


def test_mkdir_failure_is_swallowed(monkeypatch, tmp_path: Path):
    """If the parent dir can't be created, emit must not raise and not write."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    with patch(
        "pathlib.Path.mkdir", side_effect=PermissionError("nope")
    ):
        result = emit_hook_metric(
            "preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True
        )

    assert result is None
    assert not (tmp_path / "metrics.jsonl").exists()


def test_returns_none_on_success(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    result = emit_hook_metric("preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True)
    assert result is None
    # And the line was actually written.
    assert len(_read_lines(tmp_path)) == 1


def test_each_line_is_independently_valid_json(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))

    for i in range(5):
        emit_hook_metric(f"h{i}", elapsed_ms=i, repo_id="r", advisory_emitted=bool(i % 2))

    for line in _read_lines(tmp_path):
        rec = json.loads(line)  # would raise if any line were malformed
        assert "ts" in rec


def test_module_does_not_consult_chameleon_disable(monkeypatch, tmp_path: Path):
    """metrics.py has no internal kill switch: CHAMELEON_DISABLE is enforced at
    the bash-hook layer (see hooks/*), not here. With DISABLE=1 set, calling the
    function directly still writes — documenting that the gate lives upstream."""
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_DISABLE", "1")

    emit_hook_metric("preflight", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    assert (tmp_path / "metrics.jsonl").is_file()
    assert len(_read_lines(tmp_path)) == 1


def test_plugin_data_resolution_is_per_call(monkeypatch, tmp_path: Path):
    """The path is resolved from the env at call time, so two different
    CHAMELEON_PLUGIN_DATA values route to two different files within one
    process — no import-time caching of the path."""
    a = tmp_path / "a"
    b = tmp_path / "b"

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(a))
    emit_hook_metric("first", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(b))
    emit_hook_metric("second", elapsed_ms=1, repo_id="r", advisory_emitted=True)

    assert json.loads(_read_lines(a)[0])["hook"] == "first"
    assert json.loads(_read_lines(b)[0])["hook"] == "second"
    assert len(_read_lines(a)) == 1
    assert len(_read_lines(b)) == 1


def test_no_env_leak_after_tests():
    """Sanity: this module reads env at call time, so once monkeypatch unwinds
    the env there is nothing cached. Asserts the helper still resolves cleanly."""
    # With CHAMELEON_PLUGIN_DATA unset by the surrounding test isolation, the
    # helper returns the home default and must not raise.
    saved = os.environ.pop("CHAMELEON_PLUGIN_DATA", None)
    try:
        p = _metrics_path()
        assert p.name == "metrics.jsonl"
    finally:
        if saved is not None:
            os.environ["CHAMELEON_PLUGIN_DATA"] = saved
