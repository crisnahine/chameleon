"""Finding->fix loop (#9): persist surfaced findings, re-check the anchor next
Stop, re-surface an unaddressed high-severity finding ONCE.

Drives the real cross-turn flow against a real file on disk: persist (turn N),
re-check with the file UNCHANGED (turn N+1 -> re-surface once), re-check again
(no second re-surface), then CHANGE the file (-> addressed).
"""

from __future__ import annotations

from chameleon_mcp import hook_helper
from chameleon_mcp.drift import observations as obs


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CHAMELEON_FINDING_LEDGER", raising=False)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    f = repo / "src" / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    return repo, "a" * 64


# --- helpers -----------------------------------------------------------------


def test_severity_normalization():
    assert hook_helper._finding_severity({"severity": "BLOCK"}) == "BLOCK"
    assert hook_helper._finding_severity({"confidence": 0.9}) == "high"
    assert hook_helper._finding_severity({"confidence": 0.5}) == "medium"
    assert hook_helper._finding_severity({"lenses": ["correctness", "duplication"]}) == "high"
    assert hook_helper._finding_severity({"lenses": ["correctness"]}) is None
    assert hook_helper._finding_is_high("high") is True
    assert hook_helper._finding_is_high("medium") is False


def test_message_across_shapes():
    assert hook_helper._finding_message({"message": "m"}) == "m"
    assert hook_helper._finding_message({"claim": "c"}) == "c"  # multi-lens shape
    assert hook_helper._finding_message({}) is None


def test_fingerprint_stable_and_distinct():
    a = hook_helper._finding_fingerprint("correctness", "src/a.ts", 10, "bug here")
    b = hook_helper._finding_fingerprint("correctness", "src/a.ts", 10, "bug here")
    c = hook_helper._finding_fingerprint("correctness", "src/a.ts", 11, "bug here")
    assert a == b and a != c


# --- the cross-turn loop -----------------------------------------------------


def test_high_severity_unchanged_resurfaces_once_then_addressed_on_change(tmp_path, monkeypatch):
    repo, rid = _setup(tmp_path, monkeypatch)
    finding = {"file": "src/a.ts", "line": 1, "message": "logic bug", "confidence": 0.9}

    # Turn N: gate surfaces + persists.
    hook_helper._ledger_persist(rid, "s1", repo, "correctness", [finding])
    assert len(obs.open_judge_findings(rid, ws_root=str(repo))) == 1

    # Turn N+1: file UNCHANGED -> re-surface ONCE.
    lines = hook_helper._ledger_recheck_and_resurface(rid, "s1", repo)
    assert lines and any("unaddressed high-severity" in ln for ln in lines)
    assert any("src/a.ts:1" in ln for ln in lines)

    # Turn N+2: still unchanged, ALREADY resurfaced -> no second re-surface (no nag).
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", repo) == []

    # Turn N+3: file CHANGED -> addressed, drops out of the open set.
    (repo / "src" / "a.ts").write_text("export const x = 2  // fixed\n", encoding="utf-8")
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", repo) == []
    assert obs.open_judge_findings(rid, ws_root=str(repo)) == []


def test_medium_severity_never_resurfaces(tmp_path, monkeypatch):
    repo, rid = _setup(tmp_path, monkeypatch)
    hook_helper._ledger_persist(
        rid, "s1", repo, "correctness", [{"file": "src/a.ts", "line": 1, "confidence": 0.4}]
    )
    # Unchanged, but medium severity -> no re-surface (stays open, not nagged).
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", repo) == []


def test_addressed_when_file_deleted(tmp_path, monkeypatch):
    repo, rid = _setup(tmp_path, monkeypatch)
    hook_helper._ledger_persist(
        rid, "s1", repo, "correctness", [{"file": "src/a.ts", "line": 1, "confidence": 0.9}]
    )
    (repo / "src" / "a.ts").unlink()
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", repo) == []
    assert obs.open_judge_findings(rid, ws_root=str(repo)) == []


def test_multi_lens_two_lens_finding_is_high_and_resurfaces(tmp_path, monkeypatch):
    repo, rid = _setup(tmp_path, monkeypatch)
    ml = {"file": "src/a.ts", "line": 1, "claim": "dup + bug", "lenses": ["correctness", "dup"]}
    hook_helper._ledger_persist(rid, "s1", repo, "multi_lens", [ml])
    lines = hook_helper._ledger_recheck_and_resurface(rid, "s1", repo)
    assert lines and any("multi_lens" in ln for ln in lines)


def test_shared_repo_id_monorepo_does_not_cross_resolve(tmp_path, monkeypatch):
    # Two workspaces share ONE repo_id (one drift.db). A finding persisted by
    # workspace A must NOT be mis-resolved by workspace B's re-check just because
    # A's rel_path does not exist under B's root. Regression from adversarial
    # review: unscoped re-check marked every sibling workspace's finding addressed.
    rid = "b" * 64
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CHAMELEON_FINDING_LEDGER", raising=False)
    ws_a = tmp_path / "mono" / "packages" / "api"
    ws_b = tmp_path / "mono" / "packages" / "web"
    (ws_a / "src").mkdir(parents=True)
    (ws_b / "src").mkdir(parents=True)
    (ws_a / "src" / "a.ts").write_text("api code\n", encoding="utf-8")

    # A persists a high-severity finding on api/src/a.ts.
    hook_helper._ledger_persist(
        rid, "s1", ws_a, "correctness", [{"file": "src/a.ts", "line": 1, "confidence": 0.9}]
    )

    # B's Stop re-check must NOT touch A's finding (its rel_path 'src/a.ts' does
    # not exist under B -> would have been wrongly marked addressed).
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", ws_b) == []
    # A's finding is still open (not cross-resolved).
    assert len(obs.open_judge_findings(rid, ws_root=str(ws_a))) == 1

    # A's own Stop re-check now re-surfaces it (file unchanged in A).
    lines = hook_helper._ledger_recheck_and_resurface(rid, "s1", ws_a)
    assert lines and any("src/a.ts:1" in ln for ln in lines)


def test_kill_switch_disables(tmp_path, monkeypatch):
    repo, rid = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("CHAMELEON_FINDING_LEDGER", "0")
    hook_helper._ledger_persist(
        rid, "s1", repo, "correctness", [{"file": "src/a.ts", "line": 1, "confidence": 0.9}]
    )
    assert obs.open_judge_findings(rid, ws_root=str(repo)) == []
    assert hook_helper._ledger_recheck_and_resurface(rid, "s1", repo) == []
