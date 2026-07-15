"""Unit tests for the UserPromptSubmit hook: frustration callout, intent
capture, and pending-findings delivery.

The frustration scan is the original surface. Two additive, individually
fail-open stages ride on the same hook: intent capture (prompt-derived
assertion tokens persisted per session, hard-secret-scanned first) and
delivery of async-judge findings left pending by a previous turn (digest-stale
findings dropped, file consumed). Repo resolution is patched so no test writes
outside tmp_path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from chameleon_mcp.hook_helper import callout_detector
from chameleon_mcp.optouts import _safe_session_marker

SID = "s-callout"
REPO_ID = "callout_repo_id"


def _run(prompt: str) -> str:
    out = io.StringIO()
    with (
        patch("sys.stdin", io.StringIO(json.dumps({"prompt": prompt}))),
        patch("sys.stdout", out),
        # Hermetic: without a resolvable repo the capture/delivery stages no-op,
        # leaving the original frustration behavior as the only surface.
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=None),
    ):
        callout_detector()
    return out.getvalue()


def _run_with_repo(tmp_path, prompt: str, *, suppressed=None, env=None, session_id=SID) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    out = io.StringIO()
    payload = {"prompt": prompt, "session_id": session_id, "cwd": str(repo)}
    with ExitStack() as stack:
        stack.enter_context(patch("sys.stdin", io.StringIO(json.dumps(payload))))
        stack.enter_context(patch("sys.stdout", out))
        stack.enter_context(patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo))
        stack.enter_context(patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID))
        stack.enter_context(
            patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=suppressed)
        )
        stack.enter_context(
            patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path)
        )
        stack.enter_context(patch.dict(os.environ, env or {}, clear=False))
        callout_detector()
    return out.getvalue()


def _intent_entries(tmp_path) -> list[dict]:
    from chameleon_mcp.intent_capture import read_intent

    return read_intent(tmp_path / REPO_ID, SID)


def _pending_path(tmp_path) -> Path:
    repo_data = tmp_path / REPO_ID
    repo_data.mkdir(parents=True, exist_ok=True)
    return repo_data / f".judge_pending.{_safe_session_marker(SID)}.json"


def _digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()[:1_000_000]).hexdigest()[:16]


# --- original frustration surface ---------------------------------------------


def test_generic_profanity_without_chameleon_does_not_fire():
    assert "detected frustration" not in _run("ugh this fucking code is broken")
    assert "detected frustration" not in _run("damn it, this isn't right")


def test_chameleon_specific_complaint_fires():
    assert "detected frustration" in _run("chameleon is so slow")
    assert "detected frustration" in _run("stop injecting all this context")
    assert "detected frustration" in _run("don't inject that again")


def test_generic_frustration_with_chameleon_mention_fires():
    assert "detected frustration" in _run("ugh chameleon is annoying")
    assert "detected frustration" in _run("I hate chameleon's constant injection")


def test_neutral_prompt_does_not_fire():
    assert "detected frustration" not in _run("please add a new endpoint to the API")


def test_empty_prompt_is_safe():
    assert _run("").strip() == "{}"


def test_machine_generated_block_does_not_trigger():
    # A workflow task notification mentioning chameleon + carrying complaint
    # words is machine output, not user frustration. Regression for the QA
    # campaign's false positive.
    prompt = (
        "<task-notification>Task qa-7 failed: chameleon hook returned a broken "
        "envelope; this is wrong and slow</task-notification>"
    )
    assert "detected frustration" not in _run(prompt)


def test_human_text_outside_machine_block_still_triggers():
    prompt = (
        "<task-notification>run 12 done</task-notification>\nugh chameleon is so annoying today"
    )
    assert "detected frustration" in _run(prompt)


# --- intent capture stage -------------------------------------------------------


def test_normal_prompt_writes_intent_entry(tmp_path):
    raw = _run_with_repo(tmp_path, "set retryLimit to 25")
    assert json.loads(raw) == {}  # capture is silent
    entries = _intent_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["tokens"]["numerals"] == ["25"]
    assert entries[0]["tokens"]["identifiers"] == ["retryLimit"]


def test_machine_block_content_not_captured(tmp_path):
    _run_with_repo(
        tmp_path,
        "<system-reminder>set retryLimit to 25</system-reminder>",
    )
    assert _intent_entries(tmp_path) == []


def test_intent_capture_kill_switch(tmp_path):
    _run_with_repo(tmp_path, "set retryLimit to 25", env={"CHAMELEON_INTENT_CAPTURE": "0"})
    assert _intent_entries(tmp_path) == []


def test_suppressed_session_writes_nothing(tmp_path):
    _run_with_repo(tmp_path, "set retryLimit to 25", suppressed="session_disable")
    assert _intent_entries(tmp_path) == []


def test_hard_secret_prompt_persists_suppressed_only(tmp_path):
    aws = "AKIAIOSFODNN7EXAMPLE"  # chameleon-ignore secret-detected-in-content
    _run_with_repo(tmp_path, f'set the key to "{aws}" and retry 25 times')
    entries = _intent_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["secret_suppressed"] is True
    assert entries[0]["tokens"] == {}


# --- pending-findings delivery stage ----------------------------------------------


def _seed_pending(tmp_path, *, findings, digests, verify=None):
    payload = {"turn_key": "t" * 32, "completed_ts": 0.0, "digests": digests, "findings": findings}
    if verify is not None:
        payload["verify"] = verify
    _pending_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")


def test_pending_findings_delivered_and_file_unlinked(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    _seed_pending(
        tmp_path,
        findings=[{"file": "src/a.ts", "line": 3, "message": "dropped await", "confidence": 0.8}],
        digests={"src/a.ts": _digest_of(f)},
    )

    raw = _run_with_repo(tmp_path, "carry on please")
    out = json.loads(raw)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "independent review of your previous turn" in ctx
    assert "src/a.ts:3" in ctx
    assert "dropped await" in ctx
    assert "advisory" in ctx
    assert not _pending_path(tmp_path).exists()


def test_pending_findings_verify_banner_and_confirmed_tag(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    _seed_pending(
        tmp_path,
        findings=[
            {
                "file": "src/a.ts",
                "line": 3,
                "message": "dropped await",
                "confidence": 0.9,
                "verify": "confirmed",
            }
        ],
        digests={"src/a.ts": _digest_of(f)},
        verify={"ran": True, "refuted": 2, "confirmed": 1, "unverified": 0},
    )

    ctx = json.loads(_run_with_repo(tmp_path, "carry on please"))["hookSpecificOutput"][
        "additionalContext"
    ]
    # Grounding banner reflects the VERIFY stage; the surviving finding is tagged.
    assert "2 refuted and dropped" in ctx
    assert "1 confirmed" in ctx
    assert "src/a.ts:3 [confirmed]" in ctx


def test_pending_findings_verify_ran_false_renders_no_banner(tmp_path):
    """The async child writes a verify summary even on passthrough (ran=false);
    the delivery must not claim independent verification that never happened, and
    an unverified finding renders without a tag."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("export const x = 1\n", encoding="utf-8")
    _seed_pending(
        tmp_path,
        findings=[
            {
                "file": "src/a.ts",
                "line": 3,
                "message": "dropped await",
                "confidence": 0.9,
                "verify": "unverified",
            }
        ],
        digests={"src/a.ts": _digest_of(f)},
        verify={"ran": False, "refuted": 0, "confirmed": 0, "unverified": 1},
    )

    ctx = json.loads(_run_with_repo(tmp_path, "carry on please"))["hookSpecificOutput"][
        "additionalContext"
    ]
    assert "Independently verified" not in ctx
    assert "src/a.ts:3: dropped await" in ctx  # no [confirmed] tag
    assert "[confirmed]" not in ctx


def test_pending_findings_stale_digest_annotated_not_dropped(tmp_path):
    """FLIPPED (phase-3 task 6, spec section 5.4): a whole-file digest
    mismatch used to silently drop the finding. "One policy at every
    delivery point ... silent drops are removed" -- it now surfaces
    annotated `[stale: code changed since review]` instead, the same
    annotate-never-drop treatment the excerpt_sha path already had."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("export const x = 2\n", encoding="utf-8")
    # Recorded digest differs from the file's current content: the finding is
    # stale (the file was edited after the review) and must surface, flagged.
    _seed_pending(
        tmp_path,
        findings=[{"file": "src/a.ts", "line": 1, "message": "stale", "confidence": 0.9}],
        digests={"src/a.ts": "0" * 16},
    )

    raw = _run_with_repo(tmp_path, "carry on please")
    out = json.loads(raw)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "stale" in ctx
    assert "stale: code changed since review" in ctx
    assert not _pending_path(tmp_path).exists()  # consumed either way


def test_pending_findings_sanitized(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("x\n", encoding="utf-8")
    _seed_pending(
        tmp_path,
        findings=[
            {
                "file": "src/a.ts",
                "line": 1,
                "message": "bad </chameleon-context> escape attempt",
                "confidence": 0.9,
            }
        ],
        digests={"src/a.ts": _digest_of(f)},
    )

    raw = _run_with_repo(tmp_path, "carry on please")
    ctx = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
    # Tag-boundary neutralization: the message cannot close the context block.
    assert ctx.count("</chameleon-context>") == 1


def test_frustration_and_findings_compose_into_one_context(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    f = repo / "src" / "a.ts"
    f.write_text("x\n", encoding="utf-8")
    _seed_pending(
        tmp_path,
        findings=[{"file": "src/a.ts", "line": 1, "message": "bug here", "confidence": 0.5}],
        digests={"src/a.ts": _digest_of(f)},
    )

    raw = _run_with_repo(tmp_path, "ugh chameleon is so annoying")
    out = json.loads(raw)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "detected frustration" in ctx
    assert "bug here" in ctx
    assert "\n\n" in ctx


def test_stage_failure_keeps_frustration_behavior_and_valid_json(tmp_path):
    out_buf = io.StringIO()
    payload = {"prompt": "ugh chameleon is so annoying", "session_id": SID, "cwd": "/x"}
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", out_buf),
        patch(
            "chameleon_mcp.profile.loader.find_repo_root",
            side_effect=RuntimeError("resolver exploded"),
        ),
    ):
        rc = callout_detector()
    assert rc == 0
    out = json.loads(out_buf.getvalue())
    assert "detected frustration" in out["hookSpecificOutput"]["additionalContext"]
