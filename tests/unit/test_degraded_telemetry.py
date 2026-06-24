"""Unit tests for chameleon_mcp.degraded_telemetry — the read-only surfacing of
hook degraded-delivery events.

Two persisted sources feed a cumulative degraded count:
- ``.hook_errors.log`` carries ``[ts] <hook> no-interpreter (...)`` and
  ``[ts] <hook> failed (python=...)`` lines written by the shell hooks when
  Python is absent or the spawn crashes (classes 2 and 3).
- ``metrics.jsonl`` carries ``fail_open: true`` rows for the in-process advisor
  failure (class 1), already emitted by metrics.emit_hook_metric.

These tests pin the pure parser, the metrics reader, and the combined summary.
Isolation: each test sets CHAMELEON_PLUGIN_DATA / CHAMELEON_HOOK_ERROR_LOG inline
and writes only under tmp_path; the module reads env at call time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chameleon_mcp.degraded_telemetry import (
    count_failopen_metrics,
    hook_error_log_path,
    parse_degradations,
    plugin_data_dir,
    read_degraded_summary,
)


def _metrics_row(*, fail_open: bool, age_seconds: float, now: float) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_seconds))
    return json.dumps({"ts": ts, "hook": "preflight-and-advise", "fail_open": fail_open})


def _line(hook: str, kind: str, *, age_seconds: float, now: float) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_seconds))
    if kind == "no_interpreter":
        return f"[{ts}] {hook} no-interpreter (no Python >=3.11, uv unavailable)"
    return f"[{ts}] {hook} failed (python=/usr/bin/python3)"


def test_parse_empty_text_returns_zeros():
    assert parse_degradations("", since_epoch=0.0) == (0, 0, None)


def test_parse_ignores_lines_without_a_timestamp_prefix():
    # Raw python stderr lands in the same log via 2>>"${LOG_FILE}"; those lines
    # have no [ts] prefix and must not be counted.
    text = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "RuntimeError: boom\n"
    )
    assert parse_degradations(text, since_epoch=0.0) == (0, 0, None)


def test_parse_counts_both_classes():
    now = time.time()
    text = "\n".join(
        [
            _line("preflight-and-advise", "no_interpreter", age_seconds=10, now=now),
            _line("session-start", "no_interpreter", age_seconds=20, now=now),
            _line("posttool-verify", "spawn_failed", age_seconds=30, now=now),
        ]
    )
    no_interp, spawn_fail, _ = parse_degradations(text, since_epoch=now - 86400)
    assert (no_interp, spawn_fail) == (2, 1)


def test_parse_collapses_identical_burst_but_keeps_distinct_seconds():
    # One broken session writes a burst of the SAME line at one second; that is a
    # single incident, not ~25. But two no-interpreter lines from different hooks
    # at different seconds (30s vs 40s apart) are genuinely distinct events.
    now = time.time()
    burst = _line("preflight-and-advise", "no_interpreter", age_seconds=10, now=now)
    burst_text = "\n".join([burst] * 25)
    no_interp, spawn_fail, _ = parse_degradations(burst_text, since_epoch=now - 86400)
    assert (no_interp, spawn_fail) == (1, 0)  # 25 identical -> 1 incident

    distinct = "\n".join(
        [
            _line("preflight-and-advise", "no_interpreter", age_seconds=30, now=now),
            _line("session-start", "no_interpreter", age_seconds=40, now=now),
        ]
    )
    no_interp, spawn_fail, _ = parse_degradations(distinct, since_epoch=now - 86400)
    assert (no_interp, spawn_fail) == (2, 0)  # 30s/40s pair stays distinct


def test_parse_window_excludes_events_before_cutoff():
    now = time.time()
    text = "\n".join(
        [
            _line("preflight-and-advise", "no_interpreter", age_seconds=10, now=now),  # in
            _line("session-start", "no_interpreter", age_seconds=200_000, now=now),  # >2d old
        ]
    )
    no_interp, spawn_fail, _ = parse_degradations(text, since_epoch=now - 86400)
    assert (no_interp, spawn_fail) == (1, 0)


def test_parse_malformed_timestamp_is_skipped():
    text = "[not-a-timestamp] preflight-and-advise no-interpreter (x)\n"
    assert parse_degradations(text, since_epoch=0.0) == (0, 0, None)


def test_parse_last_ts_is_the_most_recent_match():
    now = time.time()
    recent_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
    text = "\n".join(
        [
            _line("preflight-and-advise", "no_interpreter", age_seconds=500, now=now),
            _line("session-start", "spawn_failed", age_seconds=10, now=now),
        ]
    )
    _, _, last_ts = parse_degradations(text, since_epoch=now - 86400)
    assert last_ts == recent_ts


def test_plugin_data_dir_honors_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    assert plugin_data_dir() == tmp_path


def test_plugin_data_dir_default_when_unset(monkeypatch):
    monkeypatch.delenv("CHAMELEON_PLUGIN_DATA", raising=False)
    assert plugin_data_dir() == Path.home() / ".local" / "share" / "chameleon"


def test_hook_error_log_path_default_under_plugin_data(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    assert hook_error_log_path() == tmp_path / ".hook_errors.log"


def test_hook_error_log_path_honors_override(monkeypatch, tmp_path: Path):
    override = tmp_path / "custom" / "errors.log"
    monkeypatch.setenv("CHAMELEON_HOOK_ERROR_LOG", str(override))
    assert hook_error_log_path() == override


def test_count_failopen_missing_file_is_zero(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    assert count_failopen_metrics(since_epoch=0.0) == (0, None)


def test_count_failopen_counts_only_true_rows_in_window(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    now = time.time()
    rows = [
        _metrics_row(fail_open=True, age_seconds=10, now=now),  # counted
        _metrics_row(fail_open=False, age_seconds=20, now=now),  # not a fail-open
        _metrics_row(fail_open=True, age_seconds=200_000, now=now),  # out of window
        _metrics_row(fail_open=True, age_seconds=30, now=now),  # counted
    ]
    (tmp_path / "metrics.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    count, last_ts = count_failopen_metrics(since_epoch=now - 86400)
    assert count == 2
    assert last_ts == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))


def test_count_failopen_skips_malformed_lines(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    now = time.time()
    body = "not json\n" + _metrics_row(fail_open=True, age_seconds=5, now=now) + "\n{partial\n"
    (tmp_path / "metrics.jsonl").write_text(body, encoding="utf-8")
    count, _ = count_failopen_metrics(since_epoch=now - 86400)
    assert count == 1


def test_summary_all_zero_when_no_sources(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    summary = read_degraded_summary(window_days=7)
    assert summary == {
        "window_days": 7,
        "advisor_unavailable": 0,
        "no_interpreter": 0,
        "spawn_failed": 0,
        "total": 0,
        "last_ts": None,
    }


def test_summary_combines_both_sources(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    now = time.time()
    log_lines = [
        _line("preflight-and-advise", "no_interpreter", age_seconds=30, now=now),
        _line("session-start", "no_interpreter", age_seconds=40, now=now),
        _line("posttool-verify", "spawn_failed", age_seconds=50, now=now),
    ]
    (tmp_path / ".hook_errors.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    (tmp_path / "metrics.jsonl").write_text(
        _metrics_row(fail_open=True, age_seconds=60, now=now) + "\n", encoding="utf-8"
    )
    summary = read_degraded_summary(window_days=7)
    assert summary["advisor_unavailable"] == 1
    assert summary["no_interpreter"] == 2
    assert summary["spawn_failed"] == 1
    assert summary["total"] == 4


def test_summary_last_ts_is_most_recent_across_sources(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    now = time.time()
    # Most recent event is the metrics fail_open at age 5.
    (tmp_path / ".hook_errors.log").write_text(
        _line("session-start", "no_interpreter", age_seconds=500, now=now) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "metrics.jsonl").write_text(
        _metrics_row(fail_open=True, age_seconds=5, now=now) + "\n", encoding="utf-8"
    )
    summary = read_degraded_summary(window_days=7)
    assert summary["last_ts"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5))


def test_summary_counts_recent_event_at_end_of_large_log(monkeypatch, tmp_path: Path):
    # A large log must still surface a recent event near the end (bounded tail).
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    now = time.time()
    noise = ("x" * 200 + "\n") * 2000  # ~400 KiB of non-matching noise
    recent = _line("preflight-and-advise", "no_interpreter", age_seconds=10, now=now)
    (tmp_path / ".hook_errors.log").write_text(noise + recent + "\n", encoding="utf-8")
    summary = read_degraded_summary(window_days=7)
    assert summary["no_interpreter"] == 1


def test_summary_never_raises_on_corrupt_sources(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("CHAMELEON_HOOK_ERROR_LOG", raising=False)
    (tmp_path / ".hook_errors.log").write_bytes(b"\xff\xfe partial \x00 line no ts\n")
    (tmp_path / "metrics.jsonl").write_bytes(b"\x00\x01 not json at all\n")
    summary = read_degraded_summary(window_days=7)
    assert summary["total"] == 0
