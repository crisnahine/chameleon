"""Unit tests for the honest two-track longitudinal health signal.

build_longitudinal_signals combines two signals chameleon already records, each
labelled for what it measures:

- Track 1 (structural_conformance): the drift score relabelled, explicitly not a
  quality bar.
- Track 2 (enforcement_outcomes): aggregate would-block / idiom-review rates
  derived from the same would_block rows the shadow report reads.

These tests pin:

- the blind-spots disclaimer is attached at the top level and in Track 1,
- Track 2 rates are would-blocks / real edits, and null (not zero) over no edits,
- the idiom-review counter feeds idiom_review_rate, not block_rate,
- repo_id / window filtering flows through to the rates,
- window_truncated propagates from the shadow read,
- fail-open: an unreadable metrics path / missing drift store degrades to
  None / zeros rather than raising,
- the get_drift_status envelope carries the honest aliases,
- the get_longitudinal_signals tool wraps the dict in the standard envelope.

Isolation: each test writes its own metrics segments under tmp_path and passes
metrics_path + a fixed now; the drift store is stubbed via monkeypatch so no env
or real data dir is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import chameleon_mcp.shadow_report as shadow_report
from chameleon_mcp.shadow_report import (
    CONFORMANCE_DISCLAIMER,
    SIGNAL_BLIND_SPOTS,
    build_longitudinal_signals,
)

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _ts(epoch: float) -> str:
    return time.strftime(_TS, time.gmtime(epoch))


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def _row(**kw) -> dict:
    return {
        "ts": kw.get("ts"),
        "hook": kw.get("hook", "posttool-verify"),
        "repo_id": kw.get("repo_id", "R"),
        "would_block": kw.get("would_block", False),
        "advisory_emitted": kw.get("advisory_emitted", True),
        "rule": kw.get("rule"),
        "file_rel": kw.get("file_rel"),
        "line": kw.get("line"),
    }


def _stub_drift(monkeypatch, stats):
    """Make compute_drift_stats (imported inside _structural_conformance) return stats."""
    import chameleon_mcp.drift.observations as obs

    monkeypatch.setattr(obs, "compute_drift_stats", lambda repo_id, **kw: stats)


def test_disclaimer_lists_the_four_blind_spots():
    # The blind-spots line is load-bearing: it is the only thing stopping a low
    # score / all-zeros panel from reading as a correctness guarantee.
    assert "logic" in CONFORMANCE_DISCLAIMER
    assert "dataflow" in CONFORMANCE_DISCLAIMER
    assert "cross-file" in CONFORMANCE_DISCLAIMER
    assert "auth" in CONFORMANCE_DISCLAIMER
    assert "NOT a quality bar" in CONFORMANCE_DISCLAIMER
    assert SIGNAL_BLIND_SPOTS == ("logic", "dataflow", "cross-file", "auth checks")


def test_top_level_carries_disclaimer_and_blind_spots(tmp_path, monkeypatch):
    _stub_drift(monkeypatch, None)
    base = tmp_path / "metrics.jsonl"
    _write(base, [])
    out = build_longitudinal_signals("R", metrics_path=base)
    assert out["disclaimer"] == CONFORMANCE_DISCLAIMER
    assert out["blind_spots"] == list(SIGNAL_BLIND_SPOTS)


def test_block_rate_and_idiom_rate_over_real_edits(tmp_path, monkeypatch):
    now = time.time()
    _stub_drift(monkeypatch, {"score": 0.25, "count": 8})
    base = tmp_path / "metrics.jsonl"
    rows = [
        # Four clean verify rows = four real edits (the denominator).
        _row(ts=_ts(now - 10)),
        _row(ts=_ts(now - 20)),
        _row(ts=_ts(now - 30)),
        _row(ts=_ts(now - 40)),
        # One would-block edit.
        _row(
            ts=_ts(now - 15),
            would_block=True,
            rule="import-preference-violation",
            file_rel="src/a.ts",
            line=3,
        ),
        # One idiom-review would-block (turn-level, not a per-rule candidate).
        _row(ts=_ts(now - 12), hook="stop-idiom-review", would_block=True),
    ]
    _write(base, rows)

    out = build_longitudinal_signals("R", now=now, metrics_path=base)

    track1 = out["structural_conformance"]
    assert track1["score"] == 0.25
    assert track1["conformance"] == 0.75
    assert track1["observations"] == 8
    assert track1["is_quality_bar"] is False
    assert track1["disclaimer"] == CONFORMANCE_DISCLAIMER

    track2 = out["enforcement_outcomes"]
    assert track2["total_edits"] == 4
    assert track2["would_block_edits"] == 1
    assert track2["idiom_review_blocks"] == 1
    assert track2["block_rate"] == 0.25
    assert track2["idiom_review_rate"] == 0.25


def test_rates_are_null_not_zero_over_no_edits(tmp_path, monkeypatch):
    # A rate over zero edits is undefined, not zero. Reporting 0% would read as
    # "ran clean" when nothing ran at all.
    now = time.time()
    _stub_drift(monkeypatch, None)
    base = tmp_path / "metrics.jsonl"
    _write(base, [])
    out = build_longitudinal_signals("R", now=now, metrics_path=base)
    track2 = out["enforcement_outcomes"]
    assert track2["total_edits"] == 0
    assert track2["block_rate"] is None
    assert track2["idiom_review_rate"] is None
    assert out["structural_conformance"] is None


def test_repo_filtering_excludes_other_repos(tmp_path, monkeypatch):
    now = time.time()
    _stub_drift(monkeypatch, {"score": 0.0, "count": 3})
    base = tmp_path / "metrics.jsonl"
    rows = [
        _row(ts=_ts(now - 10), repo_id="R"),
        _row(ts=_ts(now - 11), repo_id="OTHER", would_block=True, rule="x"),
        _row(ts=_ts(now - 12), repo_id="OTHER"),
    ]
    _write(base, rows)
    out = build_longitudinal_signals("R", now=now, metrics_path=base)
    track2 = out["enforcement_outcomes"]
    assert track2["total_edits"] == 1
    assert track2["would_block_edits"] == 0
    assert track2["block_rate"] == 0.0


def test_window_truncated_propagates(tmp_path, monkeypatch):
    # Rotation dropped the older tail: the rates are a lower bound, and the flag
    # must surface so the reader does not treat them as full coverage.
    now = time.time()
    _stub_drift(monkeypatch, {"score": 0.1, "count": 5})
    base = tmp_path / "metrics.jsonl"
    rotated = tmp_path / "metrics.jsonl.1"
    _write(base, [_row(ts=_ts(now - 100))])
    _write(rotated, [_row(ts=_ts(now - 200))])
    out = build_longitudinal_signals("R", window_days=1, now=now, metrics_path=base)
    assert out["enforcement_outcomes"]["window_truncated"] is True


def test_fail_open_on_unreadable_metrics_and_drift(tmp_path, monkeypatch):
    # An unreadable metrics path and a raising drift store must degrade, not crash.
    import chameleon_mcp.drift.observations as obs

    def _boom(*a, **k):
        raise RuntimeError("drift store unreadable")

    monkeypatch.setattr(obs, "compute_drift_stats", _boom)
    monkeypatch.setattr(
        shadow_report,
        "build_shadow_report",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("metrics unreadable")),
    )
    out = build_longitudinal_signals("R", metrics_path=tmp_path / "missing.jsonl")
    assert out["structural_conformance"] is None
    assert out["enforcement_outcomes"]["block_rate"] is None
    assert out["disclaimer"] == CONFORMANCE_DISCLAIMER


def test_none_repo_id_returns_safe_empty(monkeypatch):
    out = build_longitudinal_signals(None)
    assert out["structural_conformance"] is None
    assert out["enforcement_outcomes"]["total_edits"] == 0
    assert out["disclaimer"] == CONFORMANCE_DISCLAIMER


def test_get_drift_status_envelope_carries_honest_aliases():
    # The tool must expose the relabelled field and the disclaimer alongside the
    # legacy observed_drift_score so a caller cannot read it as a quality bar.
    from unittest.mock import patch

    from chameleon_mcp import tools

    class _FakeTrust:
        granted_at = "2026-06-01T00:00:00Z"

    class _FakeDataDir:
        def __truediv__(self, _other):
            return self

        def is_dir(self):
            return True

    with (
        patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, "a" * 64)),
        patch(
            "chameleon_mcp.drift.observations.compute_drift_score",
            return_value=0.08,
        ),
        patch("chameleon_mcp.profile.trust.plugin_data_dir", return_value=_FakeDataDir()),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=_FakeTrust()),
    ):
        env = tools.get_drift_status("a" * 64)
    body = env["data"]
    assert body["observed_drift_score"] == 0.08
    assert body["structural_conformance_score"] == 0.08
    assert body["is_quality_bar"] is False
    assert body["conformance_disclaimer"] == CONFORMANCE_DISCLAIMER
    assert body["blind_spots"] == list(SIGNAL_BLIND_SPOTS)


def test_get_longitudinal_signals_tool_wraps_envelope():
    from unittest.mock import patch

    from chameleon_mcp import tools

    report = {
        "repo_id": "a" * 64,
        "window_days": 21,
        "blind_spots": list(SIGNAL_BLIND_SPOTS),
        "disclaimer": CONFORMANCE_DISCLAIMER,
        "structural_conformance": None,
        "enforcement_outcomes": {"block_rate": None, "total_edits": 0},
    }
    with (
        patch("chameleon_mcp.tools._resolve_repo_arg", return_value=(None, "a" * 64)),
        patch(
            "chameleon_mcp.shadow_report.build_longitudinal_signals",
            return_value=report,
        ) as b,
    ):
        env = tools.get_longitudinal_signals("a" * 64)
    body = env["data"]
    assert body == report
    assert body["disclaimer"] == CONFORMANCE_DISCLAIMER
    assert body["blind_spots"] == list(SIGNAL_BLIND_SPOTS)
    b.assert_called_once_with("a" * 64, None)


def test_get_longitudinal_signals_tool_rejects_empty_repo():
    from chameleon_mcp import tools

    env = tools.get_longitudinal_signals("")
    body = env["data"]
    assert body.get("status") == "failed"
