"""Unit tests for chameleon_mcp.review_ledger.build_override_audit.

Drives the real combine: drift.db rule_overrides (durable) joined with the
shadow metrics log would_block counts, over a shared window. Asserts the
override-rate math, the min-events floor, the high-rate flag, the bare-blanket
abuse flag, and fail-open on missing data.

Isolation: CHAMELEON_PLUGIN_DATA at a fresh tmp_path (both drift.db and
metrics.jsonl live under it); the drift conn cache is cleared per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.drift import observations as obs
from chameleon_mcp.drift.observations import record_override
from chameleon_mcp.metrics import emit_hook_metric
from chameleon_mcp.review_ledger import build_override_audit

REPO_A = "a" * 64
REPO_B = "b" * 64


def _close_drift_conns() -> None:
    for conn in list(obs._DRIFT_CONN.values()):
        try:
            conn.close()
        except Exception:
            pass
    obs._DRIFT_CONN.clear()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))
    _close_drift_conns()
    yield
    _close_drift_conns()


def _would_block(rule: str, repo_id: str = REPO_A, file_rel: str = "x.ts") -> None:
    """Emit one would_block metric row for ``rule`` the shadow report will count."""
    emit_hook_metric(
        "posttool-verify",
        elapsed_ms=0,
        repo_id=repo_id,
        advisory_emitted=True,
        would_block=True,
        rule=rule,
        file_rel=file_rel,
    )


def test_empty_when_no_repo():
    audit = build_override_audit(None)
    assert audit["rules"] == {}
    assert audit["flagged"] == []
    assert audit["total_overrides"] == 0


def test_empty_when_no_activity():
    audit = build_override_audit(REPO_B)
    assert audit["rules"] == {}
    assert audit["total_overrides"] == 0


def test_combines_overrides_and_would_blocks():
    # 6 overrides + 4 would-blocks -> rate 0.6, over the 5-event floor and the
    # 0.5 high threshold, so the rule is flagged.
    for i in range(6):
        record_override(REPO_A, "import-preference-violation", rel_path=f"f{i}.ts")
    for _ in range(4):
        _would_block("import-preference-violation")

    audit = build_override_audit(REPO_A)
    rule = audit["rules"]["import-preference-violation"]
    assert rule["overrides"] == 6
    assert rule["would_blocks"] == 4
    assert rule["override_rate"] == 0.6
    assert rule["high_override_rate"] is True
    assert "import-preference-violation" in audit["flagged"]
    assert audit["total_overrides"] == 6


def test_rate_none_below_event_floor():
    # 2 overrides + 1 would-block = 3 events, below the 5-event min: no rate.
    record_override(REPO_A, "jsx-presence-mismatch")
    record_override(REPO_A, "jsx-presence-mismatch")
    _would_block("jsx-presence-mismatch")

    rule = build_override_audit(REPO_A)["rules"]["jsx-presence-mismatch"]
    assert rule["override_rate"] is None
    assert rule["high_override_rate"] is False
    assert "jsx-presence-mismatch" not in build_override_audit(REPO_A)["flagged"]


def test_low_rate_not_flagged():
    # 2 overrides + 8 would-blocks -> rate 0.2, below 0.5.
    for _ in range(2):
        record_override(REPO_A, "naming-convention-violation")
    for _ in range(8):
        _would_block("naming-convention-violation")

    audit = build_override_audit(REPO_A)
    rule = audit["rules"]["naming-convention-violation"]
    assert rule["override_rate"] == 0.2
    assert rule["high_override_rate"] is False
    assert audit["flagged"] == []


def test_blanket_abuse_flagged_independently():
    # All 6 overrides are bare blanket directives -> blanket share 1.0 >= 0.5,
    # flagged for abuse even though the rate alone might be borderline. Pair with
    # enough would-blocks that the rate stays under the high threshold so the
    # flag comes solely from blanket abuse.
    for _ in range(6):
        record_override(REPO_A, "import-preference-violation", blanket=True)
    for _ in range(20):
        _would_block("import-preference-violation")

    audit = build_override_audit(REPO_A)
    rule = audit["rules"]["import-preference-violation"]
    assert rule["blanket"] == 6
    assert rule["high_override_rate"] is False  # 6/26 < 0.5
    assert rule["blanket_abuse"] is True
    assert "import-preference-violation" in audit["flagged"]


def test_override_only_rule_appears():
    # A rule overridden but never would-blocked still shows up (rate 1.0).
    for _ in range(5):
        record_override(REPO_A, "inheritance-convention-violation")

    rule = build_override_audit(REPO_A)["rules"]["inheritance-convention-violation"]
    assert rule["overrides"] == 5
    assert rule["would_blocks"] == 0
    assert rule["override_rate"] == 1.0
    assert rule["high_override_rate"] is True


def test_failopen_on_corrupt_metrics(monkeypatch, tmp_path: Path):
    # A would-block stream read failure degrades to override-only counts, not a
    # crash. Record overrides, then make the shadow reader raise.
    record_override(REPO_A, "import-preference-violation")
    record_override(REPO_A, "import-preference-violation")

    import chameleon_mcp.review_ledger as rl

    def _boom(*a, **k):
        raise RuntimeError("bad log")

    monkeypatch.setattr(rl, "build_shadow_report", _boom, raising=False)
    # _would_block_counts imports build_shadow_report lazily; patch the source.
    monkeypatch.setattr("chameleon_mcp.shadow_report.build_shadow_report", _boom)

    audit = build_override_audit(REPO_A)
    assert audit["rules"]["import-preference-violation"]["overrides"] == 2
    assert audit["rules"]["import-preference-violation"]["would_blocks"] == 0
