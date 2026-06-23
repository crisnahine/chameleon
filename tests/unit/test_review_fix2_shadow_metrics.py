"""Regression tests for the shadow-report distinct_sessions accounting fix.

The metric rows historically carried no session id, so build_shadow_report fell
back to file_rel as a session proxy. That made ``distinct_sessions`` a silent
relabel of ``distinct_files``: a lead reading the panel saw a second dimension
that did not exist. The fix is two-part:

- metrics.emit_hook_metric now accepts and records a ``session_id`` so a real
  session can be attributed to a would_block row.
- build_shadow_report counts only rows carrying a real session id; with none it
  reports ``distinct_sessions`` as None (unknown), never a copy of
  ``distinct_files``.

Isolation: each test writes its own metrics segment under tmp_path and passes
metrics_path + a fixed now, so no env or real data dir is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chameleon_mcp.metrics import emit_hook_metric
from chameleon_mcp.shadow_report import build_shadow_report

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _ts(epoch: float) -> str:
    return time.strftime(_TS, time.gmtime(epoch))


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_distinct_sessions_is_none_not_a_copy_of_distinct_files(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    # Two would_block rows on two distinct files, NEITHER carrying a session id.
    # Pre-fix this reported distinct_sessions == distinct_files == 2.
    _write(
        base,
        [
            {
                "ts": _ts(now - 100),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/a.ts",
                "line": 3,
            },
            {
                "ts": _ts(now - 90),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/b.ts",
                "line": 5,
            },
        ],
    )

    report = build_shadow_report("R", 7, now=now, metrics_path=base)
    rule = report["rules"]["import-preference-violation"]

    assert rule["distinct_files"] == 2
    # Honest unknown, NOT a silent mirror of distinct_files.
    assert rule["distinct_sessions"] is None
    assert rule["distinct_sessions"] != rule["distinct_files"]


def test_distinct_sessions_counts_real_session_ids(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    # Three would_block rows across THREE files but only TWO distinct sessions:
    # distinct_sessions must be 2, not 3, proving it is a real second dimension.
    _write(
        base,
        [
            {
                "ts": _ts(now - 100),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/a.ts",
                "line": 3,
                "session_id": "sess-1",
            },
            {
                "ts": _ts(now - 90),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/b.ts",
                "line": 5,
                "session_id": "sess-1",
            },
            {
                "ts": _ts(now - 80),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/c.ts",
                "line": 7,
                "session_id": "sess-2",
            },
        ],
    )

    report = build_shadow_report("R", 7, now=now, metrics_path=base)
    rule = report["rules"]["import-preference-violation"]

    assert rule["distinct_files"] == 3
    assert rule["distinct_sessions"] == 2


def test_partial_session_ids_count_only_the_known_ones(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    # One row with a session id, one without, on two distinct files. The known
    # session is counted; the unattributed row does not inflate the count by
    # falling back to its file_rel.
    _write(
        base,
        [
            {
                "ts": _ts(now - 100),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "eval-call",
                "file_rel": "src/a.ts",
                "line": 3,
                "session_id": "sess-1",
            },
            {
                "ts": _ts(now - 90),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "eval-call",
                "file_rel": "src/b.ts",
                "line": 5,
            },
        ],
    )

    report = build_shadow_report("R", 7, now=now, metrics_path=base)
    rule = report["rules"]["eval-call"]

    assert rule["distinct_files"] == 2
    assert rule["distinct_sessions"] == 1


def test_emit_hook_metric_persists_session_id_end_to_end(tmp_path: Path, monkeypatch):
    # Exercises the metrics.py param through the real write path, then reads it
    # back through build_shadow_report so the threaded session id is proven to
    # land in distinct_sessions rather than being dropped.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    base = tmp_path / "metrics.jsonl"
    emit_hook_metric(
        "posttool-verify",
        elapsed_ms=0,
        repo_id="R",
        advisory_emitted=True,
        would_block=True,
        rule="import-preference-violation",
        file_rel="src/a.ts",
        line=3,
        session_id="sess-xyz",
    )

    row = json.loads(base.read_text(encoding="utf-8").splitlines()[0])
    assert row["session_id"] == "sess-xyz"

    report = build_shadow_report("R", 7, now=time.time(), metrics_path=base)
    assert report["rules"]["import-preference-violation"]["distinct_sessions"] == 1


def test_emit_hook_metric_session_id_defaults_to_none(tmp_path: Path, monkeypatch):
    # The param is optional: a caller that does not thread a session id still
    # writes a valid row, and the field is an explicit null (unknown), never the
    # file_rel.
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    base = tmp_path / "metrics.jsonl"
    emit_hook_metric(
        "posttool-verify",
        elapsed_ms=0,
        repo_id="R",
        advisory_emitted=True,
        would_block=True,
        rule="naming-convention-violation",
        file_rel="src/a.ts",
        line=3,
    )

    row = json.loads(base.read_text(encoding="utf-8").splitlines()[0])
    assert row["session_id"] is None
