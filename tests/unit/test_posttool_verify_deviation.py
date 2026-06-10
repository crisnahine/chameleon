"""End-to-end deviation-feedback tests for posttool_verify().

The sibling test_posttool_verify.py hand-feeds a `violations` list through a
mocked daemon, so it never proves the hook surfaces feedback derived from the
REAL lint engine. This file closes that gap: it builds a synthetic profile +
archetype with a concrete witness, forces the in-process fallback (daemon
unreachable), and lets `chameleon_mcp.lint_engine` actually compare the edited
file against the archetype's recalibrated ast_query.

Behaviors pinned here:
  - a file that VIOLATES the archetype shape produces the exact deviation
    messages the real engine emits, wrapped in the PostToolUse
    additionalContext channel
  - a CLEAN file (matching the witness) emits nothing
  - per-file escalation tone changes across repeated violations
    (L0 -> L1 -> L2 STOP), driven by enforcement.tone_for_level
  - the L2 escalation also surfaces the repeated-violation user note

Isolation follows the project pattern (no conftest): every test pins
CHAMELEON_PLUGIN_DATA at tmp_path via _plugin_data_dir, mocks repo/trust
resolution, and forces the in-process lint path so no daemon/socket is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from chameleon_mcp.enforcement import (
    LEVEL_L0,
    LEVEL_L1,
    EnforcementState,
    FileState,
    save_state,
)
from chameleon_mcp.profile.loader import LoadedProfile

# A witness that fixes the archetype shape to a class default export with a
# single top-level ClassDeclaration. recalibrate_ast_query turns this into the
# ast_query the engine enforces.
WITNESS_REL = "src/Component.tsx"
WITNESS_SRC = "export default class Widget {}\n"

# Violates the archetype: a function default export instead of a class.
VIOLATING_SRC = "export default function widget() {}\n"

# Matches the archetype exactly.
CLEAN_SRC = "export default class OtherWidget {}\n"


def _build_repo(tmp_path: Path) -> tuple[Path, str, LoadedProfile]:
    """Create a synthetic repo with a witness on disk + an in-memory profile.

    Returns (repo_root, repo_id, loaded_profile). The repo_id directory under
    tmp_path is created so enforcement state has somewhere to live.
    """
    repo_id = "deviation_repo_id"
    (tmp_path / repo_id).mkdir(exist_ok=True)

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)

    witness = repo / WITNESS_REL
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.write_text(WITNESS_SRC, encoding="utf-8")

    loaded = LoadedProfile(
        profile={},
        archetypes={},
        canonicals={
            "canonicals": {
                "component": [
                    {
                        # ast_query value here is a placeholder; the in-process
                        # path recalibrates it from the witness's own regex
                        # snapshot, so only "is it truthy" matters.
                        "normative_shape": {"ast_query": {"_seed": True}},
                        "witness": {"path": WITNESS_REL},
                    }
                ]
            }
        },
        rules={},
        conventions={},
        idioms_text="",
        generation=1,
        profile_dir=repo / ".chameleon",
    )
    return repo, repo_id, loaded


def _write_source(repo: Path, rel: str, src: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src, encoding="utf-8")
    return path


def _verify_seen_marker(
    tmp_path: Path, repo_id: str, file_path: str, session_id: str = "sess-deviation"
) -> Path:
    from chameleon_mcp.optouts import _safe_session_marker

    file_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:16]
    return tmp_path / repo_id / f".verify_seen.{_safe_session_marker(session_id)}.{file_hash}"


def _run_verify(
    *,
    repo: Path,
    repo_id: str,
    loaded: LoadedProfile,
    tmp_path: Path,
    file_path: str,
    session_id: str,
    grants_root: bool = True,
) -> dict:
    """Drive posttool_verify() through the in-process lint fallback.

    The first daemon_client.call (get_archetype) returns the archetype; the
    second (lint_file) returns None, which forces the in-process branch to run
    the REAL lint engine against the edited file.
    """
    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = grants_root

    captured: list[str] = []
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch(
            "chameleon_mcp.daemon_client.call",
            side_effect=[{"data": {"archetype": "component"}}, None],
        ),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=loaded),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def _ctx(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


# ---------------------------------------------------------------------------
# Core gap: a real violation produces real deviation feedback.
# ---------------------------------------------------------------------------


def test_violating_file_emits_real_deviation_feedback(tmp_path: Path):
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Bad.tsx", VIOLATING_SRC)

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id="s-violation",
    )

    out = result.get("hookSpecificOutput", {})
    # Emitted via the documented PostToolUse additionalContext channel.
    assert out.get("hookEventName") == "PostToolUse"
    assert "additionalContext" in out
    assert "updatedToolOutput" not in out

    ctx = out["additionalContext"]
    # Exactly the two violations the real engine produces for a function
    # default export against a class archetype.
    assert "[🦎 chameleon: 2 violations]" in ctx
    assert (
        "archetype expects default export of kind 'ClassDeclaration'; "
        "file has 'FunctionDeclaration'" in ctx
    )
    assert "file is missing top-level constructs the archetype expects" in ctx
    # Numbered list shape.
    assert "1. archetype expects default export of kind 'ClassDeclaration'" in ctx
    assert ctx.startswith("<chameleon-context>")
    assert ctx.rstrip().endswith("</chameleon-context>")


def test_clean_file_emits_no_feedback(tmp_path: Path):
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Good.tsx", CLEAN_SRC)

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id="s-clean",
    )

    # A first-time clean file with no prior violation history emits {}.
    assert result == {}


def test_first_violation_uses_l0_tone(tmp_path: Path):
    """A fresh violation (no prior file state) escalates NONE -> L0 and shows
    the gentle L0 tone, with no user-surfacing note."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Fresh.tsx", VIOLATING_SRC)

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id="s-fresh",
    )

    ctx = _ctx(result)
    assert "Fix these." in ctx
    # L0 must not carry the L1 "flagged before" suffix nor the L2 STOP/surface.
    assert "This file was flagged before." not in ctx
    assert "STOP. Fix these violations" not in ctx
    assert "chameleon is flagging repeated violations" not in ctx


# ---------------------------------------------------------------------------
# Per-file escalation tone changes across repeated violations.
# ---------------------------------------------------------------------------


def _seed_file_state(
    tmp_path: Path,
    repo_id: str,
    session_id: str,
    file_path: str,
    *,
    level: int,
) -> None:
    """Persist enforcement state so the next verify sees `level` for this file.

    last_violation_at is set well in the past so the hook's record_violation
    does NOT treat the new violation as a self-correction (which would suppress
    escalation) and the 60s correction-count reset window has elapsed.
    """
    state = EnforcementState()
    state.files[file_path] = FileState(
        level=level,
        violation_count=3,
        correction_count=0,
        last_violation_at=time.time() - 1000,
    )
    save_state(state, tmp_path / repo_id, session_id)


def test_escalation_l0_to_l1_tone(tmp_path: Path):
    """A file already at L0 escalates to L1 on the next violation; the tone
    gains the 'This file was flagged before.' suffix."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Esc.tsx", VIOLATING_SRC)
    sid = "s-l0-l1"

    _seed_file_state(tmp_path, repo_id, sid, str(cand), level=LEVEL_L0)
    # Pre-seed leaves no .verify_seen marker, so the cooldown gate stays open.

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
    )

    ctx = _ctx(result)
    assert "Fix these. This file was flagged before." in ctx
    # Not yet the L2 STOP escalation.
    assert "STOP. Fix these violations" not in ctx
    assert "chameleon is flagging repeated violations" not in ctx


def test_escalation_l1_to_l2_stop_tone_and_user_surface(tmp_path: Path):
    """A file at L1 escalates to L2; tone becomes the hard STOP, and because
    the file hit L2 with no clean pass yet, the user-surfacing note appears."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/EscStop.tsx", VIOLATING_SRC)
    sid = "s-l1-l2"

    _seed_file_state(tmp_path, repo_id, sid, str(cand), level=LEVEL_L1)

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
    )

    ctx = _ctx(result)
    assert "STOP. Fix these violations before any other edit." in ctx
    # L2-with-no-clean surfaces a teach pointer to the human partner.
    assert "chameleon is flagging repeated violations" in ctx
    assert "/chameleon-teach" in ctx
    # The path in the surface note is the edited file.
    assert "EscStop.tsx" in ctx
    # The gentle L0-only phrasing must NOT be the trailing tone now.
    assert "This file was flagged before." not in ctx


def test_tone_strictly_escalates_across_levels(tmp_path: Path):
    """End-to-end: the same violating shape yields progressively harsher tone
    as the seeded level rises NONE -> L0 -> L1. Distinct sessions keep each
    seeded level isolated from the prior run's persisted escalation."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Ladder.tsx", VIOLATING_SRC)
    fp = str(cand)
    # Each run uses a distinct session, and the cooldown marker is session-
    # scoped, so no run's .verify_seen stamp can short-circuit the next into
    # the "already verified" note instead of re-linting.

    # Fresh (no prior state) -> escalates to L0 tone.
    fresh = _ctx(
        _run_verify(
            repo=repo,
            repo_id=repo_id,
            loaded=loaded,
            tmp_path=tmp_path,
            file_path=fp,
            session_id="ladder-fresh",
        )
    )

    _seed_file_state(tmp_path, repo_id, "ladder-l0", fp, level=LEVEL_L0)
    at_l0 = _ctx(
        _run_verify(
            repo=repo,
            repo_id=repo_id,
            loaded=loaded,
            tmp_path=tmp_path,
            file_path=fp,
            session_id="ladder-l0",
        )
    )

    _seed_file_state(tmp_path, repo_id, "ladder-l1", fp, level=LEVEL_L1)
    at_l1 = _ctx(
        _run_verify(
            repo=repo,
            repo_id=repo_id,
            loaded=loaded,
            tmp_path=tmp_path,
            file_path=fp,
            session_id="ladder-l1",
        )
    )

    # Tier 0: gentle, no escalation markers.
    assert "Fix these." in fresh
    assert "This file was flagged before." not in fresh
    assert "STOP. Fix these violations" not in fresh

    # Tier 1: flagged-before suffix, still no STOP.
    assert "This file was flagged before." in at_l0
    assert "STOP. Fix these violations" not in at_l0

    # Tier 2: hard STOP.
    assert "STOP. Fix these violations before any other edit." in at_l1

    # All three are genuinely different feedback bodies.
    assert fresh != at_l0 != at_l1
    assert fresh != at_l1


# ---------------------------------------------------------------------------
# Cooldown and trust gates around the deviation path.
# ---------------------------------------------------------------------------


def _content_digest(src: str) -> str:
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def test_cooldown_suppresses_repeat_then_emits_dedup_note(tmp_path: Path):
    """A fresh .verify_seen marker recording the SAME content digest
    short-circuits the deviation lint and emits the 'already verified' note."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Cool.tsx", VIOLATING_SRC)
    fp = str(cand)

    marker = _verify_seen_marker(tmp_path, repo_id, fp, session_id="s-cooldown")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_content_digest(VIOLATING_SRC), encoding="utf-8")

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=fp,
        session_id="s-cooldown",
    )

    ctx = _ctx(result)
    assert "already verified this file" in ctx
    # The real violation list must NOT be recomputed/emitted on a cooldown hit.
    assert "2 violations" not in ctx


def test_cooldown_reverifies_when_content_changed_within_window(tmp_path: Path):
    """An edit landing inside the cooldown window must re-verify: the marker
    records a different content digest, so the dedup cannot suppress analysis.
    Regression for the iterate-then-break flow where a defect introduced
    mid-cooldown slipped through silently."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Cool2.tsx", VIOLATING_SRC)
    fp = str(cand)

    marker = _verify_seen_marker(tmp_path, repo_id, fp, session_id="s-cooldown-changed")
    marker.parent.mkdir(parents=True, exist_ok=True)
    # The previously verified content differs from what is on disk now.
    marker.write_text(_content_digest("export const fine = 1;\n"), encoding="utf-8")

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=fp,
        session_id="s-cooldown-changed",
    )

    ctx = _ctx(result)
    assert "already verified this file" not in ctx
    assert "2 violations" in ctx


def test_legacy_empty_marker_forces_reverification(tmp_path: Path):
    """A pre-digest (empty) marker never matches, so one fresh verification
    runs and rewrites the marker in the new digest format."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Cool3.tsx", VIOLATING_SRC)
    fp = str(cand)

    marker = _verify_seen_marker(tmp_path, repo_id, fp, session_id="s-cooldown-legacy")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=fp,
        session_id="s-cooldown-legacy",
    )

    ctx = _ctx(result)
    assert "already verified this file" not in ctx
    assert "2 violations" in ctx
    assert marker.read_text(encoding="utf-8") == _content_digest(VIOLATING_SRC)


def test_violation_writes_verify_seen_marker(tmp_path: Path):
    """After emitting deviation feedback, the hook drops a .verify_seen marker
    so an immediate re-edit hits the cooldown."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Mark.tsx", VIOLATING_SRC)
    fp = str(cand)
    marker = _verify_seen_marker(tmp_path, repo_id, fp, session_id="s-mark")

    assert not marker.exists()

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=fp,
        session_id="s-mark",
    )

    assert "[🦎 chameleon: 2 violations]" in _ctx(result)
    assert marker.exists()


def test_untrusted_profile_skips_deviation_feedback(tmp_path: Path):
    """An ungranted workspace under a shared repo_id is untrusted; PostToolUse
    must NOT feed violations derived from an untrusted profile back."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Untrusted.tsx", VIOLATING_SRC)

    result = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id="s-untrusted",
        grants_root=False,
    )

    assert result == {}


def test_verify_disabled_env_skips_deviation(tmp_path: Path):
    """CHAMELEON_VERIFY=0 short-circuits before any lint runs."""
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Off.tsx", VIOLATING_SRC)

    captured: list[str] = []
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(cand)},
        "session_id": "s-off",
    }
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_VERIFY": "0"}, clear=False),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    output = "".join(captured).strip()
    assert (json.loads(output) if output else {}) == {}


def test_deviation_message_sanitized_against_tag_injection(tmp_path: Path):
    """If a witness's recalibrated query produced a message containing a tag
    boundary, the emitted block must neutralize it. We exercise the real
    sanitizer on the real engine output: the engine messages here are static,
    so we assert the wrapper integrity holds (single chameleon-context block).
    """
    repo, repo_id, loaded = _build_repo(tmp_path)
    cand = _write_source(repo, "src/Sanitize.tsx", VIOLATING_SRC)

    ctx = _ctx(
        _run_verify(
            repo=repo,
            repo_id=repo_id,
            loaded=loaded,
            tmp_path=tmp_path,
            file_path=str(cand),
            session_id="s-sanitize",
        )
    )
    # Exactly one opening and one closing wrapper tag — no nested/duplicate
    # boundary that would let injected content escape the block.
    assert ctx.count("<chameleon-context>") == 1
    assert ctx.count("</chameleon-context>") == 1
