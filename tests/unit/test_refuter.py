"""refuter.run_batch must: cap spawns, fail open to 'unverified' (never silently
confirm or drop), and produce one verdict per finding. The refuter is the
independent round-3 step; over-killing or over-confirming both break the
anti-hallucination guarantee, so the fail-open polarity is pinned here."""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp import refuter


def _finding(i):
    return {
        "id": f"f{i}",
        "kind": "inverted-condition",
        "severity": "FIX",
        "claim": "condition inverted",
        "evidence": "line 10",
        "file": "a.ts",
        "line": 10,
    }


def test_cap_marks_remainder_unverified(monkeypatch):
    calls = []

    def fake_run_one(repo_root, finding, excerpt, *, model, timeout):
        calls.append(finding["id"])
        return {"id": finding["id"], "verdict": "confirmed", "reason": "ok"}

    monkeypatch.setattr(refuter, "run_one", fake_run_one)
    findings = [_finding(i) for i in range(5)]
    out = refuter.run_batch(
        Path("/tmp"), findings, ["x"] * 5, model="sonnet", timeout=45, max_spawns=3, concurrency=2
    )
    assert len(out) == 5
    assert len(calls) == 3  # cap honored
    capped = [v for v in out if v["verdict"] == "unverified"]
    assert len(capped) == 2
    assert all("cap reached" in v["reason"] for v in capped)


def test_run_one_fails_open_to_unverified(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(refuter, "_spawn", boom, raising=False)
    out = refuter.run_one(Path("/tmp"), _finding(1), "excerpt", model="sonnet", timeout=45)
    assert out["verdict"] == "unverified"
