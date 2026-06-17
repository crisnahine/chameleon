"""refute_finding is the skill's round-3 gate. It must: honor the
CHAMELEON_REVIEW_REFUTER=0 kill switch, fail open to refuter='unavailable' (never
crash, never invent verdicts), and return one verdict per input finding."""

from __future__ import annotations

from chameleon_mcp import tools


def test_kill_switch_disables(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_REFUTER", "0")
    out = tools.refute_finding("0" * 64, [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}])
    assert out["data"]["refuter"] == "disabled"
    assert out["data"]["verdicts"] == []


def test_unavailable_fails_open(monkeypatch):
    monkeypatch.delenv("CHAMELEON_REVIEW_REFUTER", raising=False)
    monkeypatch.setattr("chameleon_mcp.refuter.refuter_available", lambda: False)
    out = tools.refute_finding("0" * 64, [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}])
    assert out["data"]["refuter"] == "unavailable"
    # one entry per finding, all unverified (never silently dropped)
    assert [v["verdict"] for v in out["data"]["verdicts"]] == ["unverified"]


def test_empty_findings_returns_empty():
    out = tools.refute_finding("0" * 64, [])
    assert out["data"]["verdicts"] == []
