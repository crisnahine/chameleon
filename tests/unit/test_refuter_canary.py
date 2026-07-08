"""F1: refuter integrity canaries.

The refuter is the only component allowed to kill a finding and is otherwise
unmeasured. These tests pin the recall/precision scoring (pure, no spawning) and
the run path's fail-open + cli-absent handling with a mocked refuter.
"""

from __future__ import annotations

from chameleon_mcp.refuter_canary import CANARIES, evaluate_canaries, run_refuter_canaries

# A tiny deterministic canary set: 2 real bugs, 1 false alarm.
_CANARIES = [
    {"cls": "off-by-one", "real": True, "claim": "off by one", "excerpt": "x[n-1]"},
    {"cls": "dropped-await", "real": True, "claim": "missing await", "excerpt": "persist(u)"},
    {"cls": "false-positive", "real": False, "claim": "wrong", "excerpt": "sum(xs)"},
]


def test_perfect_refuter_scores_100():
    # Real bugs survive (not refuted), the false alarm is refuted.
    verdicts = {"0": "confirmed", "1": "unverified", "2": "refuted"}
    r = evaluate_canaries(verdicts, _CANARIES)
    assert r["overall"]["recall"] == 1.0  # 2/2 real survived
    assert r["overall"]["precision"] == 1.0  # 1/1 false killed
    assert r["misses"] == []


def test_aggressive_refuter_loses_recall():
    # The refuter wrongly REFUTES a real bug -> recall failure, logged with class.
    verdicts = {"0": "refuted", "1": "confirmed", "2": "refuted"}
    r = evaluate_canaries(verdicts, _CANARIES)
    assert r["overall"]["recall"] == 0.5  # only 1/2 real survived
    assert r["overall"]["precision"] == 1.0
    assert any(m["kind"] == "recall" and m["cls"] == "off-by-one" for m in r["misses"])


def test_lenient_refuter_loses_precision():
    # The refuter fails to refute a false alarm -> precision failure.
    verdicts = {"0": "confirmed", "1": "confirmed", "2": "confirmed"}
    r = evaluate_canaries(verdicts, _CANARIES)
    assert r["overall"]["recall"] == 1.0
    assert r["overall"]["precision"] == 0.0  # 0/1 false killed
    assert any(m["kind"] == "precision" for m in r["misses"])


def test_missing_verdict_scored_as_unverified():
    # No verdict for canary 2 (refuter never ran it) -> unverified -> a false alarm
    # that was NOT killed -> precision miss; real canaries default recall-ok.
    r = evaluate_canaries({}, _CANARIES)
    assert r["overall"]["recall"] == 1.0  # both real default to survived
    assert r["overall"]["precision"] == 0.0  # the false alarm was not killed
    assert any(m["kind"] == "precision" for m in r["misses"])


def test_by_class_breakdown():
    verdicts = {"0": "refuted", "1": "confirmed", "2": "refuted"}
    r = evaluate_canaries(verdicts, _CANARIES)
    assert r["by_class"]["off-by-one"]["recall"] == 0.0  # the one that was killed
    assert r["by_class"]["dropped-await"]["recall"] == 1.0
    assert r["by_class"]["false-positive"]["precision"] == 1.0
    # A class with no real canaries reports precision but null recall.
    assert r["by_class"]["false-positive"]["recall"] is None


def test_builtin_canary_set_is_balanced():
    # The shipped set must carry BOTH real and false canaries or it measures only
    # half of refuter behavior.
    reals = [c for c in CANARIES if c.get("real")]
    falses = [c for c in CANARIES if not c.get("real")]
    assert len(reals) >= 3 and len(falses) >= 2
    for c in CANARIES:
        assert c.get("excerpt") and c.get("claim") and c.get("cls")


def test_run_unavailable_when_cli_absent(monkeypatch):
    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: "claude CLI not found")
    out = run_refuter_canaries("/tmp", canaries=_CANARIES)
    assert out["status"] == "unavailable"
    assert "not found" in out["reason"]


def test_run_with_mocked_refuter(monkeypatch):
    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None)

    def _fake_run_one(repo_root, finding, excerpt, *, model, timeout, retry):
        # A perfect refuter: refute the false alarm (id 2), keep the real ones.
        return {"id": finding["id"], "verdict": "refuted" if finding["id"] == "2" else "confirmed"}

    monkeypatch.setattr(refuter, "run_one", _fake_run_one)
    out = run_refuter_canaries("/tmp", model="sonnet", timeout=5, canaries=_CANARIES)
    assert out["status"] == "ran"
    assert out["overall"]["recall"] == 1.0
    assert out["overall"]["precision"] == 1.0


def test_run_fails_open_when_refuter_raises(monkeypatch):
    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: None)

    def _boom(*a, **k):
        raise RuntimeError("spawn died")

    monkeypatch.setattr(refuter, "run_one", _boom)
    out = run_refuter_canaries("/tmp", model="sonnet", timeout=5, canaries=_CANARIES)
    # Every spawn error -> unverified; real canaries still count as survived, so
    # the harness never crashes and never fabricates a recall failure.
    assert out["status"] == "ran"
    assert out["overall"]["recall"] == 1.0


def test_main_gates_on_recall_not_precision(monkeypatch):
    from chameleon_mcp import refuter_canary as rc

    def _out(recall, precision):
        return lambda root: {"status": "ran", "overall": {"recall": recall, "precision": precision}}

    # A precision miss (stochastic false-alarm survival) must NOT red the gate --
    # this is the exact flaky-CI defect the sweep caught with a real refuter run.
    monkeypatch.setattr(rc, "run_refuter_canaries", _out(1.0, 0.667))
    assert rc.main(["/tmp"]) == 0
    # A real recall drop (the refuter KILLING real findings) does gate.
    monkeypatch.setattr(rc, "run_refuter_canaries", _out(0.5, 1.0))
    assert rc.main(["/tmp"]) == 1
    # At the gate boundary, pass.
    monkeypatch.setattr(rc, "run_refuter_canaries", _out(0.75, 0.2))
    assert rc.main(["/tmp"]) == 0
    # Refuter unavailable -> 2.
    monkeypatch.setattr(
        rc, "run_refuter_canaries", lambda root: {"status": "unavailable", "reason": "no cli"}
    )
    assert rc.main(["/tmp"]) == 2
