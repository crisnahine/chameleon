"""Unit tests for the turn-end VERIFY stage (stop_verify.py).

The VERIFY stage wires the independent refuter into the automatic turn-end review
(SCOPE -> EVIDENCE -> ATTACK -> VERIFY -> REPORT). Its contract: it may only ever
DROP a finding the refuter actively refuted ON REAL EVIDENCE; it must never drop a
finding on its own failure, never spawn a refuter with an empty excerpt (the prompt
commands refutation on cannot-tell, so a zero-evidence spawn would kill anchorless
findings), and never invent a confirmed verdict. These tests pin that contract with
a stubbed refuter so no real ``claude -p`` spawn runs.
"""

from __future__ import annotations

import pytest

from chameleon_mcp import stop_verify
from chameleon_mcp.judge import Finding


def _f(msg, conf, file="a.py", line=1):
    return Finding(message=msg, confidence=conf, file=file, line=line)


@pytest.fixture(autouse=True)
def _clear_kill_switch(monkeypatch):
    monkeypatch.delenv("CHAMELEON_STOP_VERIFY", raising=False)


@pytest.fixture
def repo(tmp_path):
    """A minimal repo dir with the default finding target on disk (excerpts are
    read from disk now, so a finding needs a real contained file to be spawnable)."""
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
    return tmp_path


def _stub_refuter(monkeypatch, verdicts_by_id, *, absent=None, raises=False):
    """Stub refuter.run_batch/refuter_cli_absent so no real spawn runs."""
    calls = {"run_batch": 0, "kwargs": None, "findings": None}

    def fake_absent():
        return absent

    def fake_run_batch(repo_root, findings, excerpts, *, model, timeout, max_spawns, **kw):
        calls["run_batch"] += 1
        calls["kwargs"] = dict(model=model, timeout=timeout, max_spawns=max_spawns, **kw)
        calls["findings"] = findings
        if raises:
            raise RuntimeError("boom")
        head = findings[:max_spawns]
        tail = findings[max_spawns:]
        out = []
        for f in head:
            out.append(
                {"id": f.get("id"), "verdict": verdicts_by_id.get(f.get("id"), "unverified")}
            )
        for f in tail:
            out.append({"id": f.get("id"), "verdict": "unverified"})
        return out

    import chameleon_mcp.refuter as refuter

    monkeypatch.setattr(refuter, "refuter_cli_absent", fake_absent)
    monkeypatch.setattr(refuter, "run_batch", fake_run_batch)
    return calls


def test_disabled_passthrough(repo, monkeypatch):
    monkeypatch.setenv("CHAMELEON_STOP_VERIFY", "0")
    calls = _stub_refuter(monkeypatch, {})
    findings = [_f("bug one", 0.9), _f("bug two", 0.5)]
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=100, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is False
    assert res.kept == findings
    assert calls["run_batch"] == 0
    assert res.skip_reason == "disabled"


def test_empty_findings_no_spawn(repo, monkeypatch):
    calls = _stub_refuter(monkeypatch, {})
    res = stop_verify.verify_stop_findings(
        repo, [], budget_seconds=100, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is False
    assert res.kept == []
    assert calls["run_batch"] == 0


def test_refuted_dropped_confirmed_and_unverified_kept(repo, monkeypatch):
    # ids are the ORIGINAL finding indices; verify ranks high-confidence first but
    # ids must map back to the original order.
    findings = [_f("refuted bug", 0.9), _f("confirmed bug", 0.8), _f("unknown bug", 0.6)]
    _stub_refuter(
        monkeypatch,
        {"0": "refuted", "1": "confirmed", "2": "unverified"},
    )
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is True
    assert res.refuted == 1
    assert res.confirmed == 1
    assert res.unverified == 1
    kept_msgs = [f.message for f in res.kept]
    assert "refuted bug" not in kept_msgs
    assert "confirmed bug" in kept_msgs
    assert "unknown bug" in kept_msgs
    # kept_verdicts is aligned with kept order; the confirmed finding leads (ranked).
    vmap = dict(zip(kept_msgs, res.kept_verdicts, strict=True))
    assert vmap["confirmed bug"] == "confirmed"
    assert vmap["unknown bug"] == "unverified"
    assert res.kept[0].message == "confirmed bug"


def test_failopen_on_spawn_error_keeps_all(repo, monkeypatch):
    findings = [_f("bug one", 0.9), _f("bug two", 0.5)]
    calls = _stub_refuter(monkeypatch, {}, raises=True)
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    # A broken refuter must NEVER drop a finding.
    assert calls["run_batch"] == 1  # the batch genuinely ran and raised
    assert res.ran is False
    assert [f.message for f in res.kept] == ["bug one", "bug two"]


def test_cli_absent_passthrough(repo, monkeypatch):
    findings = [_f("bug one", 0.9)]
    calls = _stub_refuter(monkeypatch, {}, absent="claude CLI not found")
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is False
    assert calls["run_batch"] == 0
    assert res.kept == findings


def test_zero_budget_no_spawn(repo, monkeypatch):
    findings = [_f("bug one", 0.9)]
    calls = _stub_refuter(monkeypatch, {"0": "refuted"})
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=0, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is False
    assert calls["run_batch"] == 0
    assert res.kept == findings  # nothing dropped without budget


def test_empty_excerpt_never_spawned_kept_unverified(repo, monkeypatch):
    """The raise-precision guard: a finding with no file, a missing file, or an
    uncontained path has no excerpt; the refuter prompt commands refutation on
    cannot-tell, so such findings must never be spawned -- they pass through
    unverified while anchored findings still verify."""
    findings = [
        _f("anchorless", 0.9, file=None, line=None),
        _f("missing file", 0.9, file="nope.py", line=3),
        _f("anchored refuted", 0.9, file="a.py", line=2),
    ]
    calls = _stub_refuter(monkeypatch, {"2": "refuted"})
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is True
    # Only the anchored finding was sent to the refuter.
    assert [rf["id"] for rf in calls["findings"]] == ["2"]
    kept_msgs = [f.message for f in res.kept]
    assert "anchorless" in kept_msgs
    assert "missing file" in kept_msgs
    assert "anchored refuted" not in kept_msgs
    assert res.refuted == 1
    assert res.unverified == 2


def test_all_empty_excerpts_passthrough_no_spawn(tmp_path, monkeypatch):
    findings = [_f("anchorless", 0.9, file=None, line=None)]
    calls = _stub_refuter(monkeypatch, {"0": "refuted"})
    res = stop_verify.verify_stop_findings(
        tmp_path, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is False
    assert res.skip_reason == "no verifiable excerpts"
    assert calls["run_batch"] == 0
    assert res.kept == findings


def test_spawns_are_retry_free(repo, monkeypatch):
    """The Stop path's budget arithmetic assumes one timeout window per slot, so
    run_batch must be called with retry=False."""
    calls = _stub_refuter(monkeypatch, {})
    stop_verify.verify_stop_findings(
        repo, [_f("bug", 0.9)], budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert calls["kwargs"]["retry"] is False


def test_dict_findings_multilens_shape(repo, monkeypatch):
    """The multi-lens path passes synthesis dicts ({file, line, claim, lenses,
    confidence}), not Finding objects; the stage must handle both."""
    findings = [
        {
            "file": "a.py",
            "line": 2,
            "claim": "refuted claim",
            "lenses": ["correctness"],
            "confidence": 0.9,
        },
        {
            "file": "a.py",
            "line": 5,
            "claim": "kept claim",
            "lenses": ["correctness"],
            "confidence": 0.8,
        },
    ]
    _stub_refuter(monkeypatch, {"0": "refuted", "1": "confirmed"})
    res = stop_verify.verify_stop_findings(
        repo, findings, budget_seconds=1000, model="sonnet", max_spawns=8, timeout=45
    )
    assert res.ran is True
    assert [f["claim"] for f in res.kept] == ["kept claim"]
    assert res.kept_verdicts == ["confirmed"]
    assert res.refuted == 1


def test_affordable_spawns_math():
    # None budget => unbounded, capped at max_spawns.
    assert stop_verify._affordable_spawns(None, 45, 8) == 8
    # 0 or negative => 0.
    assert stop_verify._affordable_spawns(0, 45, 8) == 0
    assert stop_verify._affordable_spawns(-5, 45, 8) == 0
    # One wave of concurrency fits.
    assert stop_verify._affordable_spawns(45, 45, 8, concurrency=4) == 4
    # Not enough for even one timeout window => 0.
    assert stop_verify._affordable_spawns(20, 45, 8, concurrency=4) == 0
    # Two waves, but max_spawns caps it.
    assert stop_verify._affordable_spawns(200, 45, 6, concurrency=4) == 6


def test_severity_across_shapes():
    assert stop_verify._severity_for(_f("m", 0.9)) == "high"
    assert stop_verify._severity_for(_f("m", 0.7)) == "high"
    assert stop_verify._severity_for(_f("m", 0.69)) == "medium"
    assert stop_verify._severity_for({"claim": "c", "confidence": 0.9}) == "high"
    assert stop_verify._severity_for({"claim": "c", "severity": "BLOCK"}) == "BLOCK"
    # Two independently-agreeing lenses read high (mirrors _finding_severity).
    assert (
        stop_verify._severity_for({"claim": "c", "lenses": ["correctness", "duplication"]})
        == "high"
    )
    assert stop_verify._severity_for({"claim": "c"}) == "medium"


def test_excerpt_window_reads_and_failopen(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text("\n".join(f"line{i}" for i in range(1, 121)), encoding="utf-8")
    win = stop_verify._excerpt_window(tmp_path, "mod.py", 60, context=3)
    assert "line60" in win
    assert "line57" in win
    assert "line63" in win
    assert "line10" not in win
    # No line number: head-of-file fallback so an anchorless-but-filed finding
    # still gets real evidence.
    head = stop_verify._excerpt_window(tmp_path, "mod.py", None)
    assert "line1" in head
    assert "line50" in head
    assert "line51" not in head
    # Missing file / no file => "".
    assert stop_verify._excerpt_window(tmp_path, "nope.py", 5) == ""
    assert stop_verify._excerpt_window(tmp_path, None, 5) == ""


def test_excerpt_window_containment(tmp_path):
    """The finding's file field is model output: reads outside repo_root (absolute
    or ..-traversal) must yield "" -- the excerpt lands in a model prompt, so an
    escape would exfiltrate arbitrary local files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inside.py").write_text("secret_inside = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET_OUTSIDE = 1\n", encoding="utf-8")

    # Absolute path INSIDE the repo is fine.
    assert "secret_inside" in stop_verify._excerpt_window(repo, str(repo / "inside.py"), 1)
    # Absolute path OUTSIDE the repo is refused.
    assert stop_verify._excerpt_window(repo, str(outside), 1) == ""
    # ..-traversal out of the repo is refused.
    assert stop_verify._excerpt_window(repo, "../outside.py", 1) == ""
    # Symlink escape is refused (safe_read_text rejects symlinks).
    link = repo / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert stop_verify._excerpt_window(repo, "link.py", 1) == ""


def test_rank_kept_confirmed_high_first():
    findings = [
        _f("low unverified", 0.4, line=1),
        _f("high confirmed", 0.9, line=2),
        _f("high unverified", 0.85, line=3),
    ]
    verify_by_id = {"0": "unverified", "1": "confirmed", "2": "unverified"}
    ranked = stop_verify.rank_kept(findings, verify_by_id)
    # confirmed-high leads, then remaining by confidence desc.
    assert ranked[0].message == "high confirmed"
    assert ranked[1].message == "high unverified"
    assert ranked[2].message == "low unverified"
