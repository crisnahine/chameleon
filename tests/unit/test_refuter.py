"""refuter.run_batch must: cap spawns, fail open to 'unverified' (never silently
confirm or drop), and produce one verdict per finding. The refuter is the
independent round-3 step; over-killing or over-confirming both break the
anti-hallucination guarantee, so the fail-open polarity is pinned here."""

from __future__ import annotations

import json
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


def _stream_json(verdict_body: str) -> str:
    """A realistic `claude -p --output-format stream-json` stdout carrying the
    refuter's verdict inside an assistant result block, preceded by the
    system-init `tools` array that a raw JSON scan would wrongly lock onto."""
    system = json.dumps({"type": "system", "subtype": "init", "tools": ["Task", "Bash"]})
    result = json.dumps({"type": "result", "result": verdict_body})
    return system + "\n" + result


def test_run_one_fails_open_to_unverified(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(refuter, "_spawn_status", boom, raising=False)
    out = refuter.run_one(Path("/tmp"), _finding(1), "excerpt", model="sonnet", timeout=45)
    assert out["verdict"] == "unverified"


def test_run_one_forwards_model_and_timeout(monkeypatch):
    """model and timeout must reach _spawn_status as keyword args."""
    captured = {}

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        captured["model"] = model
        captured["timeout_s"] = timeout_s
        return _stream_json('[{"confirmed": false, "reason": "refuted by test"}]'), None

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(Path("/tmp"), _finding(2), "excerpt", model="opus", timeout=99)
    assert captured["model"] == "opus"
    assert captured["timeout_s"] == 99
    assert out["verdict"] == "refuted"


def test_run_one_parses_verdict_from_stream_json_not_envelope(monkeypatch):
    """The verdict lives inside the assistant/result text block; a raw scan of the
    stream-json would grab the system-init `tools` array and read every verdict
    unverified. This pins the two-step extraction so that regression cannot return."""

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        return _stream_json('[{"confirmed": true, "reason": "the guard is gone"}]'), None

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(Path("/tmp"), _finding(3), "excerpt", model="sonnet", timeout=45)
    assert out["verdict"] == "confirmed"
    assert out["reason"] == "the guard is gone"


def test_run_one_accepts_bare_object_verdict(monkeypatch):
    """The model sometimes drops the array wrapper and returns a bare object."""

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        return _stream_json('{"confirmed": false, "reason": "no bug shown"}'), None

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(Path("/tmp"), _finding(4), "excerpt", model="sonnet", timeout=45)
    assert out["verdict"] == "refuted"


def test_run_one_retries_once_on_transient_failure(monkeypatch):
    """A non-timeout spawn failure (the plain-fallback flakiness) is retried once;
    the retry's real verdict must win over the first attempt's empty result."""
    attempts = []

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        attempts.append(1)
        if len(attempts) == 1:
            return None, "spawn_nonzero_exit"
        return _stream_json('[{"confirmed": true, "reason": "confirmed on retry"}]'), None

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(Path("/tmp"), _finding(5), "excerpt", model="sonnet", timeout=45)
    assert len(attempts) == 2
    assert out["verdict"] == "confirmed"


def test_run_one_does_not_retry_on_timeout(monkeypatch):
    """A genuine timeout is NOT retried -- a second spawn would blow the budget."""
    attempts = []

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        attempts.append(1)
        return None, "spawn_timeout"

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(Path("/tmp"), _finding(6), "excerpt", model="sonnet", timeout=45)
    assert len(attempts) == 1
    assert out["verdict"] == "unverified"
