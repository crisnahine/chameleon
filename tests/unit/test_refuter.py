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

    def fake_run_one(repo_root, finding, excerpt, *, model, timeout, retry=True):
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


def test_run_one_retry_false_spawns_exactly_once(monkeypatch):
    """The Stop-path VERIFY budget assumes one timeout window per slot, so
    retry=False must suppress the transient-failure re-spawn."""
    calls = {"n": 0}

    def fake_spawn(prompt, cwd, *, model=None, timeout_s=None):
        calls["n"] += 1
        return (None, "spawn_nonzero_exit")

    monkeypatch.setattr(refuter, "_spawn_status", fake_spawn)
    out = refuter.run_one(
        Path("/tmp"), _finding(1), "excerpt", model="sonnet", timeout=45, retry=False
    )
    assert calls["n"] == 1
    assert out["verdict"] == "unverified"

    # Default (retry=True) keeps the one re-spawn for the interactive skills.
    calls["n"] = 0
    refuter.run_one(Path("/tmp"), _finding(1), "excerpt", model="sonnet", timeout=45)
    assert calls["n"] == 2


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


# --- build_refuter_prompt: intent_tokens rendering (phase-3 task-6 item A) ---
#
# An intent-forced correctness finding ("code says 2, user wanted 3") used to
# reach the refuter with only its excerpt: the excerpt alone can look
# internally consistent, so the refuter's "cannot tell -> refute" rule kills a
# finding that is only wrong relative to what the user actually asked for.
# verify.py's _to_refuter_dict already carried intent_tokens on the dict; this
# pins that build_refuter_prompt actually renders them into the prompt.


def test_prompt_includes_intent_line_when_finding_carries_intent_tokens():
    finding = {**_finding(1), "intent_tokens": ["retryLimit", "3"]}
    prompt = refuter.build_refuter_prompt(finding, "some excerpt")
    assert "intent: retryLimit, 3" in prompt
    # Still inside the untrusted <finding> data block, not floated loose.
    before_close = prompt.split("</finding>")[0]
    assert "intent: retryLimit, 3" in before_close


def test_prompt_omits_intent_line_when_no_intent_tokens():
    prompt = refuter.build_refuter_prompt(_finding(1), "some excerpt")
    assert "intent:" not in prompt


def test_prompt_omits_intent_line_for_empty_intent_tokens_list():
    finding = {**_finding(1), "intent_tokens": []}
    prompt = refuter.build_refuter_prompt(finding, "some excerpt")
    assert "intent:" not in prompt


def test_prompt_still_renders_kind_claim_evidence_unchanged():
    """Additive only: the existing rendered fields keep their exact shape for
    every caller that supplies no intent_tokens at all (e.g. pr-review's
    round-3 refuter, which builds its finding dicts without that key)."""
    prompt = refuter.build_refuter_prompt(_finding(1), "some excerpt")
    assert "kind: inverted-condition" in prompt
    assert "claim: condition inverted" in prompt
    assert "evidence: line 10" in prompt
