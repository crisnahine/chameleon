"""refute_finding is the skill's round-3 gate. It must: honor the
CHAMELEON_REVIEW_REFUTER=0 kill switch, fail open to refuter='unavailable' (never
crash, never invent verdicts), and return one verdict per input finding."""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import tools


def test_kill_switch_disables(monkeypatch):
    monkeypatch.setenv("CHAMELEON_REVIEW_REFUTER", "0")
    out = tools.refute_finding("0" * 64, [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}])
    assert out["data"]["refuter"] == "disabled"
    assert out["data"]["verdicts"] == []


def test_unavailable_fails_open(monkeypatch, tmp_path):
    # Reach the refuter_unavailable_reason() gate -- refute_finding now
    # consults that, not refuter_available(). A real trusted tmp repo gets
    # past the earlier repo-unresolved / untrusted returns so the unavailable
    # path has genuine coverage (and never spawns a real `claude -p`).
    monkeypatch.delenv("CHAMELEON_REVIEW_REFUTER", raising=False)
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    repo = tmp_path / "repo"
    cham = repo / ".chameleon"
    cham.mkdir(parents=True)
    (cham / "profile.json").write_text('{"generation": 1, "language": "typescript"}')
    from chameleon_mcp.profile.trust import grant_trust

    grant_trust(tools._compute_repo_id(repo), cham)
    monkeypatch.setattr(
        "chameleon_mcp.refuter.refuter_unavailable_reason", lambda: "test: cli unavailable"
    )
    out = tools.refute_finding(
        str(repo), [{"id": "f1", "kind": "x", "claim": "c", "evidence": "e"}]
    )
    assert out["data"]["refuter"] == "unavailable"
    # one entry per finding, all unverified (never silently dropped), carrying
    # the precise reason from refuter_unavailable_reason().
    assert [v["verdict"] for v in out["data"]["verdicts"]] == ["unverified"]
    assert out["data"]["verdicts"][0]["reason"] == "test: cli unavailable"


def test_empty_findings_returns_empty():
    out = tools.refute_finding("0" * 64, [])
    assert out["data"]["verdicts"] == []


def test_anchorless_excerpt_uses_whole_branch_diff(monkeypatch):
    """Anchorless finding (no file) should call _git_branch_diff with only repo_root+base_ref."""
    calls = []

    def fake_branch_diff(repo_root, base_ref, rel_path=None):
        calls.append({"repo_root": repo_root, "base_ref": base_ref, "rel_path": rel_path})
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


def test_traversal_path_returns_empty(tmp_path, monkeypatch):
    """A finding with a path-traversal file must fail open to '' (never read outside repo)."""
    # Ensure _git_branch_diff doesn't fire for the traversal file path.
    monkeypatch.setattr(tools, "_git_branch_diff", lambda *a, **kw: "")
    result = tools._refuter_excerpt_for(
        tmp_path, {"file": "../../../etc/passwd", "line": 1}, "main"
    )
    assert result == "", "traversal path must fail open to ''"


def test_dotdot_path_no_escape(tmp_path, monkeypatch):
    """Any '../'-escaping path yields '' regardless of whether the target exists."""
    monkeypatch.setattr(tools, "_git_branch_diff", lambda *a, **kw: "")
    result = tools._refuter_excerpt_for(tmp_path, {"file": "../../.ssh/id_rsa"}, "main")
    assert result == "", "repo-escaping path must fail open to ''"
