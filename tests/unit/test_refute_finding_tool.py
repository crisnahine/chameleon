"""refute_finding is the skill's round-3 gate. It must: honor the
CHAMELEON_REVIEW_REFUTER=0 kill switch, fail open to refuter='unavailable' (never
crash, never invent verdicts), and return one verdict per input finding."""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import tools


def test_kill_switch_disables(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_REFUTER", "0")
    out = tools.refute_finding(
        "0" * 64, [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}]
    )
    assert out["data"]["refuter"] == "disabled"
    assert out["data"]["verdicts"] == []


def test_unavailable_fails_open(monkeypatch):
    monkeypatch.delenv("CHAMELEON_REVIEW_REFUTER", raising=False)
    monkeypatch.setattr("chameleon_mcp.refuter.refuter_available", lambda: False)
    out = tools.refute_finding(
        "0" * 64, [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}]
    )
    assert out["data"]["refuter"] == "unavailable"
    # one entry per finding, all unverified (never silently dropped)
    assert [v["verdict"] for v in out["data"]["verdicts"]] == ["unverified"]


def test_empty_findings_returns_empty():
    out = tools.refute_finding("0" * 64, [])
    assert out["data"]["verdicts"] == []


def test_anchorless_excerpt_uses_whole_branch_diff(monkeypatch):
    """Anchorless finding (no file) should call _git_branch_diff with only repo_root+base_ref."""
    calls = []

    def fake_branch_diff(repo_root, base_ref, rel_path=None):
        calls.append(
            {"repo_root": repo_root, "base_ref": base_ref, "rel_path": rel_path}
        )
        return "sentinel-diff-output"

    monkeypatch.setattr(tools, "_git_branch_diff", fake_branch_diff)
    result = tools._refuter_excerpt_for(
        Path("/tmp/x"), {"id": "f", "claim": "c", "evidence": "e"}, "main"
    )
    assert result == "sentinel-diff-output"
    assert len(calls) == 1
    assert calls[0]["repo_root"] == Path("/tmp/x")
    assert calls[0]["base_ref"] == "main"
    assert calls[0]["rel_path"] is None


def test_refuter_excerpt_fails_open_when_git_branch_diff_raises(monkeypatch):
    """_refuter_excerpt_for must return '' if _git_branch_diff raises."""

    def exploding_branch_diff(repo_root, base_ref, rel_path=None):
        raise RuntimeError("simulated git error")

    monkeypatch.setattr(tools, "_git_branch_diff", exploding_branch_diff)
    result = tools._refuter_excerpt_for(
        Path("/tmp/x"), {"id": "f", "claim": "c", "evidence": "e"}, "main"
    )
    assert result == ""


def test_kill_switch_checked_before_empty_findings(monkeypatch):
    """CHAMELEON_REVIEW_REFUTER=0 must return disabled even when findings is empty."""
    monkeypatch.setenv("CHAMELEON_REVIEW_REFUTER", "0")
    out = tools.refute_finding("0" * 64, [])
    assert out["data"]["refuter"] == "disabled"
