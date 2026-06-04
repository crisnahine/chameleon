"""Unit tests for chameleon_mcp.shadow_report — the would_block read-back.

build_shadow_report aggregates metrics.jsonl (current + rotated segments) into
per-rule would-block counts, distinct files/sessions, a turn-level idiom-review
counter, a sampled file:line list, and a promotion verdict. These tests pin:

- rotation merge (current + .1/.2 backups all read),
- the truncated-window flag when rotation dropped the older tail,
- repo_id and window filtering,
- the verdict logic (would_block / insufficient_data / safe_to_enforce) by COUNT,
- no false-positive fraction is ever emitted,
- the idiom-review gate is a separate counter, not a per-rule candidate,
- defensive parsing of malformed lines,
- the sample cap.

Isolation: each test writes its own metrics segments under tmp_path and passes
metrics_path + a fixed now, so no env or real data dir is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chameleon_mcp._thresholds import threshold_int
from chameleon_mcp.shadow_report import build_shadow_report

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _ts(epoch: float) -> str:
    return time.strftime(_TS, time.gmtime(epoch))


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def _row(**kw) -> dict:
    base = {
        "ts": kw.get("ts"),
        "hook": kw.get("hook", "posttool-verify"),
        "repo_id": kw.get("repo_id", "R"),
        "would_block": kw.get("would_block", False),
        "advisory_emitted": kw.get("advisory_emitted", True),
        "rule": kw.get("rule"),
        "file_rel": kw.get("file_rel"),
        "line": kw.get("line"),
    }
    return base


def test_would_block_counts_group_by_rule(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(
                ts=_ts(now - 100),
                would_block=True,
                rule="import-preference-violation",
                file_rel="src/a.ts",
                line=3,
            ),
            _row(
                ts=_ts(now - 90),
                would_block=True,
                rule="import-preference-violation",
                file_rel="src/b.ts",
                line=7,
            ),
            _row(
                ts=_ts(now - 80),
                would_block=True,
                rule="jsx-presence-mismatch",
                file_rel="src/a.ts",
            ),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    rules = rep["rules"]
    assert rules["import-preference-violation"]["would_blocks"] == 2
    assert rules["import-preference-violation"]["distinct_files"] == 2
    assert rules["jsx-presence-mismatch"]["would_blocks"] == 1
    assert rules["import-preference-violation"]["verdict"] == "would_block"


def test_no_false_positive_fraction_key(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(base, [_row(ts=_ts(now - 10), would_block=True, rule="x", file_rel="f.ts")])
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    blob = json.dumps(rep).lower()
    assert "false_positive" not in blob
    assert "fp_rate" not in blob
    assert "fp_fraction" not in blob


def test_safe_to_enforce_requires_enough_edits_and_no_block(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    min_edits = threshold_int("SHADOW_PROMOTION_MIN_EDITS")
    rows = [
        _row(ts=_ts(now - 10), would_block=False, advisory_emitted=False, rule=None)
        for _ in range(min_edits + 5)
    ]
    # An advisory-only emission for a rule that never would-blocked.
    rows.append(_row(ts=_ts(now - 5), would_block=False, rule="import-preference-violation"))
    _write(base, rows)
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["total_edits"] >= min_edits
    assert rep["window_truncated"] is False
    assert rep["rules"]["import-preference-violation"]["verdict"] == "safe_to_enforce"
    assert rep["rules"]["import-preference-violation"]["advisory_only"] == 1


def test_insufficient_data_when_too_few_edits(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(ts=_ts(now - 10), would_block=False, rule="import-preference-violation"),
            _row(ts=_ts(now - 9), would_block=False, advisory_emitted=False),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["import-preference-violation"]["verdict"] == "insufficient_data"


def test_window_truncated_when_rotation_dropped_older_tail(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    # A rotation backup exists and the oldest retained row is only 5 days old,
    # but the window asks for 21 days, so the older tail was rotated away.
    _write(
        base,
        [_row(ts=_ts(now - 5 * 86400), would_block=False, advisory_emitted=False)],
    )
    (tmp_path / "metrics.jsonl.1").write_text("", encoding="utf-8")
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["window_truncated"] is True


def test_young_repo_without_rotation_is_not_truncated(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    # No rotation backup: the whole (short) history is present, so a 5-day-old
    # oldest row inside a 21-day window is a young repo, not a truncated window.
    _write(
        base,
        [_row(ts=_ts(now - 5 * 86400), would_block=False, advisory_emitted=False)],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["window_truncated"] is False


def test_window_not_truncated_when_old_rows_present(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [_row(ts=_ts(now - 25 * 86400), would_block=False, advisory_emitted=False)],
    )
    (tmp_path / "metrics.jsonl.1").write_text("", encoding="utf-8")
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["window_truncated"] is False


def test_truncated_window_blocks_safe_verdict(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    min_edits = threshold_int("SHADOW_PROMOTION_MIN_EDITS")
    rows = [
        _row(ts=_ts(now - 86400), would_block=False, advisory_emitted=False)
        for _ in range(min_edits + 5)
    ]
    rows.append(_row(ts=_ts(now - 3600), would_block=False, rule="import-preference-violation"))
    _write(base, rows)
    (tmp_path / "metrics.jsonl.1").write_text("", encoding="utf-8")
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["window_truncated"] is True
    # Enough edits and zero would-blocks, but truncation forbids "safe".
    assert rep["rules"]["import-preference-violation"]["verdict"] == "insufficient_data"


def test_rotated_segments_are_merged(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(base, [_row(ts=_ts(now - 10), would_block=True, rule="x", file_rel="cur.ts")])
    _write(
        tmp_path / "metrics.jsonl.1",
        [_row(ts=_ts(now - 20), would_block=True, rule="x", file_rel="r1.ts")],
    )
    _write(
        tmp_path / "metrics.jsonl.2",
        [_row(ts=_ts(now - 30), would_block=True, rule="x", file_rel="r2.ts")],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["x"]["would_blocks"] == 3
    assert rep["rules"]["x"]["distinct_files"] == 3


def test_repo_id_filter_excludes_other_repos(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(ts=_ts(now - 10), repo_id="R", would_block=True, rule="x", file_rel="a"),
            _row(ts=_ts(now - 10), repo_id="OTHER", would_block=True, rule="x", file_rel="b"),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["x"]["would_blocks"] == 1


def test_window_excludes_old_rows(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(ts=_ts(now - 5 * 86400), would_block=True, rule="x", file_rel="recent"),
            _row(ts=_ts(now - 25 * 86400), would_block=True, rule="x", file_rel="old"),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["x"]["would_blocks"] == 1


def test_idiom_review_is_separate_counter(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(ts=_ts(now - 10), hook="stop-idiom-review", would_block=True, rule=None),
            _row(ts=_ts(now - 9), hook="stop-idiom-review", would_block=True, rule=None),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["idiom_review"]["would_blocks"] == 2
    # No idiom-review entry leaks into the per-rule promotion candidates.
    assert rep["rules"] == {}


def test_malformed_lines_skipped(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    good = json.dumps(_row(ts=_ts(now - 10), would_block=True, rule="x", file_rel="a"))
    base.write_text(
        good + "\n" + "{not json\n" + "[1,2,3]\n" + "null\n" + "\n" + good + "\n",
        encoding="utf-8",
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["x"]["would_blocks"] == 2


def test_sample_capped(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    cap = threshold_int("SHADOW_REPORT_SAMPLE_CAP")
    rows = [
        _row(ts=_ts(now - 10), would_block=True, rule="x", file_rel=f"f{i}.ts", line=i)
        for i in range(cap + 10)
    ]
    _write(base, rows)
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert len(rep["sample"]) == cap
    assert rep["sample_truncated"] is True
    assert rep["sample"][0]["rule"] == "x"


def test_missing_log_returns_empty_report(tmp_path: Path):
    now = time.time()
    base = tmp_path / "does-not-exist.jsonl"
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"] == {}
    assert rep["total_edits"] == 0
    assert rep["window_truncated"] is False


def test_unattributed_would_block_bucketed(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            _row(
                ts=_ts(now - 10), hook="stop-backstop", would_block=True, rule=None, file_rel="x.ts"
            )
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    assert rep["rules"]["(unattributed)"]["would_blocks"] == 1


def test_nonpositive_window_falls_back_to_default(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(base, [_row(ts=_ts(now - 10), would_block=True, rule="x", file_rel="a")])
    rep = build_shadow_report("R", 0, now=now, metrics_path=base)
    assert rep["window_days"] == threshold_int("SHADOW_REPORT_WINDOW_DAYS")


def test_override_rows_counted_apart_from_advisory_only(tmp_path: Path):
    # An inline chameleon-ignore override (hook="override", override=True) must
    # NOT inflate advisory_only: it is a bypass of a block-eligible rule, not an
    # advisory-only emission, so it would over-count the promotion denominator.
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            {
                "ts": _ts(now - 10),
                "hook": "override",
                "repo_id": "R",
                "would_block": False,
                "rule": "eval-call",
                "override": True,
                "file_rel": "a.ts",
            },
            # A genuine advisory-only emission for the same rule.
            _row(ts=_ts(now - 9), hook="preflight-and-advise", rule="eval-call", file_rel="b.ts"),
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    entry = rep["rules"]["eval-call"]
    assert entry["overrides"] == 1
    assert entry["advisory_only"] == 1
    assert entry["would_blocks"] == 0


def test_override_flag_on_other_hook_still_bucketed_as_override(tmp_path: Path):
    # The override flag is honored even if the row's hook name differs, so a
    # future emit site that sets override=True is never miscounted as advisory.
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            {
                "ts": _ts(now - 10),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": False,
                "rule": "import-preference-violation",
                "override": True,
                "file_rel": "a.ts",
            }
        ],
    )
    rep = build_shadow_report("R", 21, now=now, metrics_path=base)
    entry = rep["rules"]["import-preference-violation"]
    assert entry["overrides"] == 1
    assert entry["advisory_only"] == 0
    # The override row must not be counted as a clean verify edit either.
    assert rep["total_edits"] == 0
