"""stop/verify.py: the VERIFY stage over canonical core.finding.Finding.

The refuter spawn itself is neutralized by conftest's autouse guard
(``refuter._spawn_status`` -> ``(None, "spawn_exec_error")``,
``refuter.refuter_cli_absent`` -> None) for every test here; tests that need
a controlled verdict monkeypatch ``refuter.run_batch`` directly, the same
convention the guard's own docstring documents.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import refuter
from chameleon_mcp.core.budget import TurnBudget
from chameleon_mcp.core.finding import Finding
from chameleon_mcp.stop import verify


def _finding(**over) -> Finding:
    base = dict(
        id="f1",
        kind="correctness",
        severity="high",
        confidence=0.9,
        file="src/a.py",
        span=(3, 3),
        claim="retry count is 2 not 3",
        evidence="src/a.py:3 hardcodes 2",
        excerpt_sha="",
        excerpt="",
        source_lens="correctness",
        status="pending",
        created_at="2026-07-15T00:00:00Z",
        intent_tokens=("retries=3",),
    )
    base.update(over)
    return Finding(**base)


def _repo_with_file(tmp_path: Path, rel: str = "src/a.py") -> Path:
    repo = tmp_path / "repo"
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"line {i}" for i in range(1, 20)) + "\n", encoding="utf-8")
    return repo


def _budget(seconds: float = 120.0) -> TurnBudget:
    return TurnBudget.for_hook(total_seconds=seconds, token_ceiling=10_000)


# --- empty input -----------------------------------------------------------


def test_no_findings_returns_empty_no_event():
    events = []
    out = verify.verify_findings(
        [], repo_root=Path("/nonexistent"), budget=_budget(), event_sink=lambda *a: events.append(a)
    )
    assert out == []
    assert events == []


# --- drop / annotate ---------------------------------------------------------


def test_drops_only_refuted_and_annotates_the_rest(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    findings = [
        _finding(id="a", claim="claim a", file="src/a.py", span=(2, 2)),
        _finding(id="b", claim="claim b", file="src/a.py", span=(3, 3)),
        _finding(id="c", claim="claim c", file="src/a.py", span=(4, 4)),
    ]

    def fake_run_batch(repo_root, ref_findings, excerpts, **kw):
        return [
            {"id": "0", "verdict": "refuted"},
            {"id": "1", "verdict": "confirmed"},
            {"id": "2", "verdict": "something-weird"},
        ]

    monkeypatch.setattr(refuter, "run_batch", fake_run_batch)

    out = verify.verify_findings(findings, repo_root=repo, budget=_budget(), event_sink=None)

    claims = {f.claim: f.verified for f in out}
    assert "claim a" not in claims  # refuted -> dropped
    assert claims["claim b"] == "confirmed"
    assert claims["claim c"] == "unverified"  # unrecognized verdict coerces to unverified


def test_missing_verdict_defaults_to_unverified_and_is_kept(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    findings = [_finding(file="src/a.py", span=(2, 2))]
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [])

    out = verify.verify_findings(findings, repo_root=repo, budget=_budget(), event_sink=None)

    assert len(out) == 1
    assert out[0].verified == "unverified"


# --- skipped VERIFY: always disclosed, never silent -------------------------


def test_disabled_emits_skip_event_and_passes_all_through_unverified(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    monkeypatch.setenv("CHAMELEON_STOP_VERIFY", "0")
    findings = [_finding(), _finding(id="f2", claim="second")]
    events = []

    out = verify.verify_findings(
        findings, repo_root=repo, budget=_budget(), event_sink=lambda *a: events.append(a)
    )

    assert len(out) == 2
    assert all(f.verified == "unverified" for f in out)
    assert events == [("skipped", "disabled")]


def test_no_budget_emits_skip_event_and_passes_through(tmp_path):
    repo = _repo_with_file(tmp_path)
    findings = [_finding()]
    events = []

    out = verify.verify_findings(
        findings, repo_root=repo, budget=_budget(0.0), event_sink=lambda *a: events.append(a)
    )

    assert len(out) == 1
    assert out[0].verified == "unverified"
    assert events == [("skipped", "no_budget")]


def test_cli_absent_emits_skip_event_and_passes_through(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    monkeypatch.setattr(refuter, "refuter_cli_absent", lambda: "claude CLI not found")
    findings = [_finding()]
    events = []

    out = verify.verify_findings(
        findings, repo_root=repo, budget=_budget(), event_sink=lambda *a: events.append(a)
    )

    assert len(out) == 1
    assert out[0].verified == "unverified"
    assert events == [("skipped", "claude CLI not found")]


def test_no_verifiable_excerpts_emits_skip_event_and_passes_through(tmp_path):
    # File does not exist -> no excerpt can be fetched for anyone.
    repo = tmp_path / "repo"
    repo.mkdir()
    findings = [_finding(file="src/missing.py", span=(1, 1))]
    events = []

    out = verify.verify_findings(
        findings, repo_root=repo, budget=_budget(), event_sink=lambda *a: events.append(a)
    )

    assert len(out) == 1
    assert out[0].verified == "unverified"
    assert events == [("skipped", "no_verifiable_excerpts")]


def test_run_batch_exception_fails_open(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    findings = [_finding()]
    events = []

    def boom(*a, **k):
        raise RuntimeError("spawn machinery blew up")

    monkeypatch.setattr(refuter, "run_batch", boom)

    out = verify.verify_findings(
        findings, repo_root=repo, budget=_budget(), event_sink=lambda *a: events.append(a)
    )

    assert len(out) == 1
    assert out[0].verified == "unverified"
    assert events == [("skipped", "error")]


# --- the drift-death pin: kind + evidence + intent_tokens -------------------


def test_refuter_dict_carries_kind_evidence_and_intent_tokens(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    finding = _finding(
        kind="duplication",
        claim="widget() re-implements gadget()",
        evidence="src/a.py:3 duplicates src/b.py:9",
        intent_tokens=("widget", "gadget"),
    )
    captured: dict = {}

    def fake_run_batch(repo_root, ref_findings, excerpts, **kw):
        captured["ref_findings"] = ref_findings
        return [{"id": "0", "verdict": "unverified"}]

    monkeypatch.setattr(refuter, "run_batch", fake_run_batch)

    verify.verify_findings([finding], repo_root=repo, budget=_budget(), event_sink=None)

    assert len(captured["ref_findings"]) == 1
    d = captured["ref_findings"][0]
    assert d["kind"] == "duplication"
    assert d["evidence"] == "src/a.py:3 duplicates src/b.py:9"
    assert d["intent_tokens"] == ["widget", "gadget"]
    assert d["claim"] == "widget() re-implements gadget()"


# --- excerpt attachment: happens regardless of VERIFY's own fate -----------


def test_excerpt_attached_even_when_disabled(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    monkeypatch.setenv("CHAMELEON_STOP_VERIFY", "0")
    finding = _finding(file="src/a.py", span=(3, 3), excerpt="")

    out = verify.verify_findings([finding], repo_root=repo, budget=_budget(), event_sink=None)

    assert len(out) == 1
    assert out[0].excerpt  # non-empty: real content read from disk
    assert "line 3" in out[0].excerpt
    assert out[0].excerpt_sha  # digest pinned alongside the text


def test_existing_excerpt_is_not_overwritten(tmp_path):
    repo = _repo_with_file(tmp_path)
    pinned_sha = "deadbeef" * 4
    finding = _finding(
        file="src/a.py", span=(3, 3), excerpt="already pinned text", excerpt_sha=pinned_sha
    )

    out = verify.verify_findings([finding], repo_root=repo, budget=_budget(0.0), event_sink=None)

    assert out[0].excerpt == "already pinned text"
    assert out[0].excerpt_sha == pinned_sha


def test_findings_immutable_originals_untouched(tmp_path, monkeypatch):
    """The frozen-Finding contract this module leans on: verifying never
    mutates the caller's own objects, only ever derives new ones."""
    repo = _repo_with_file(tmp_path)
    finding = _finding(file="src/a.py", span=(3, 3), excerpt="")
    monkeypatch.setattr(refuter, "run_batch", lambda *a, **k: [{"id": "0", "verdict": "confirmed"}])

    out = verify.verify_findings([finding], repo_root=repo, budget=_budget(), event_sink=None)

    assert finding.excerpt == ""  # original untouched
    assert finding.verified == "unverified"  # dataclass default, untouched
    assert out[0] is not finding
    assert out[0].verified == "confirmed"


def test_completed_event_reports_counts(tmp_path, monkeypatch):
    repo = _repo_with_file(tmp_path)
    findings = [
        _finding(id="a", claim="a", span=(2, 2)),
        _finding(id="b", claim="b", span=(3, 3)),
        _finding(id="c", claim="c", span=(4, 4)),
    ]
    monkeypatch.setattr(
        refuter,
        "run_batch",
        lambda *a, **k: [
            {"id": "0", "verdict": "refuted"},
            {"id": "1", "verdict": "confirmed"},
            {"id": "2", "verdict": "unverified"},
        ],
    )
    events = []

    verify.verify_findings(
        findings, repo_root=repo, budget=_budget(), event_sink=lambda *a: events.append(a)
    )

    assert events == [("completed", "refuted=1 confirmed=1 unverified=1")]
