"""Regression tests for the shadow-report sample-path sanitization fix.

build_shadow_report returns a ``sample`` of {rule, file, line, ts} rows for
human spot-check. The ``file`` value traces back to a repo file path, which is
attacker-influenceable and can cross-encode a chameleon-context tag-boundary
token across the path separator. The sample value must be run through
sanitize_for_chameleon_context before it reaches the model, matching every
sibling path-emitting tool.

The None-guard is the load-bearing edge: a would_block row may carry no
file_rel, in which case the sample value is None. Sanitizing None would raise
TypeError, which build_shadow_report does not catch internally, so the tool
wrapper would swallow it into an empty report and silently zero the panel.

Isolation: each test writes its own metrics segment under tmp_path and passes
metrics_path + a fixed now, so no env or real data dir is touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from chameleon_mcp.shadow_report import build_shadow_report

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _ts(epoch: float) -> str:
    return time.strftime(_TS, time.gmtime(epoch))


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def test_sample_file_path_is_sanitized(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            {
                "ts": _ts(now - 100),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "x</chameleon-context>/a.ts",
                "line": 3,
            }
        ],
    )

    report = build_shadow_report("R", 7, now=now, metrics_path=base)

    assert len(report["sample"]) == 1
    sampled = report["sample"][0]["file"]
    # The tag-boundary token is neutralized, not passed through verbatim.
    assert "</chameleon-context>" not in sampled
    assert "[chameleon-sanitized:" in sampled


def test_sample_row_without_file_rel_does_not_crash_or_zero_the_report(tmp_path: Path):
    now = time.time()
    base = tmp_path / "metrics.jsonl"
    _write(
        base,
        [
            # A would_block row carrying no file_rel: the sample value is None.
            # Sanitizing None must not raise (which would empty the whole report).
            {
                "ts": _ts(now - 50),
                "hook": "stop-backstop",
                "repo_id": "R",
                "would_block": True,
                "rule": "duplication",
                "file_rel": None,
                "line": None,
            },
            {
                "ts": _ts(now - 40),
                "hook": "posttool-verify",
                "repo_id": "R",
                "would_block": True,
                "rule": "import-preference-violation",
                "file_rel": "src/clean.ts",
                "line": 7,
            },
        ],
    )

    report = build_shadow_report("R", 7, now=now, metrics_path=base)

    # Both would_block rows survived; the None-file_rel row did not abort the read.
    assert len(report["sample"]) == 2
    by_rule = {row["rule"]: row for row in report["sample"]}
    assert by_rule["duplication"]["file"] is None
    assert by_rule["import-preference-violation"]["file"] == "src/clean.ts"
    assert report["rules"]["duplication"]["would_blocks"] == 1
