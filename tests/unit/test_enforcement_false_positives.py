"""Regression tests for enforcement false-positive holes.

Three confirmed false positives in posttool_verify() + stop_backstop():

1. An inline `// chameleon-ignore <rule>` directive downgraded the inline block
   but NOT the cached blockable_unresolved flag, so the Stop backstop still
   blocked the file at turn end. jsx-presence-mismatch is the clean reproducer:
   its lint layer never honors the directive, so the violation reaches the
   recorder intact.

2. A phantom-import that reached L2 and was later resolved (target created in a
   separate edit, so the importing file was never re-verified) left a stale
   blockable_unresolved flag. The Stop backstop blocked correct code because it
   trusted the cached flag without a live re-check.

3. Stale-trust profiles (granted hash no longer matches the current profile)
   could still block/deny. The spec gates enforcement on "trusted" only, not
   "stale".

These drive the real posttool_verify() to seed on-disk EnforcementState, then
run the real stop_backstop() against the same data dir.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from chameleon_mcp.enforcement import (
    LEVEL_L2,
    EnforcementState,
    FileState,
    load_state,
    save_state,
)
from chameleon_mcp.enforcement_calibration import write_block_rules
from chameleon_mcp.profile.loader import LoadedProfile

REPO_ID = "fp_repo_id"

WITNESS_REL = "src/widget.ts"
WITNESS_SRC = "export default function widget() {}\n"

# JSX in a non-JSX archetype -> jsx-presence-mismatch error. The AST-query lint
# path emits it even with a chameleon-ignore directive present.
JSX_SRC = "const x = <div />\nexport default function widget() { return null }\n"
JSX_IGNORE_SRC = "// chameleon-ignore jsx-presence-mismatch\n" + JSX_SRC

# A relative import whose target is missing -> phantom-import. Matches the
# function-default-export archetype shape, so phantom-import is the sole
# violation.
PHANTOM_SRC = "import { x } from './missing-target'\nexport default function widget() {}\n"


def _build_repo(tmp_path: Path, *, mode: str) -> tuple[Path, LoadedProfile]:
    (tmp_path / REPO_ID).mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": mode, "stop_backstop": True}}),
        encoding="utf-8",
    )
    # profile.json is required for hash_profile() to return a non-empty hash.
    (chameleon / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

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
                        "normative_shape": {"ast_query": {"_seed": True}},
                        "witness": {"path": WITNESS_REL},
                    }
                ]
            }
        },
        rules={},
        conventions={"conventions": {}},
        idioms_text="",
        generation=1,
        profile_dir=chameleon,
    )
    return repo, loaded


def _seed_level(tmp_path: Path, sid: str, file_path: str, *, level: int) -> None:
    state = EnforcementState()
    state.files[file_path] = FileState(
        level=level,
        violation_count=3,
        correction_count=0,
        last_violation_at=time.time() - 1000,
    )
    save_state(state, tmp_path / REPO_ID, sid)


def _trust_rec(*, hash_for_root: str = "H") -> MagicMock:
    """A trust record that grants the root. ``hash_for_root`` controls the
    granted hash; the stale-trust tests pass a value that differs from the live
    hash_profile() output to simulate a profile that changed after the grant."""
    rec = MagicMock()
    rec.grants_root.return_value = True
    rec.hash_for_root.return_value = hash_for_root
    return rec


def _run_verify(
    *,
    repo: Path,
    loaded: LoadedProfile,
    tmp_path: Path,
    file_path: str,
    session_id: str,
    trust_rec: MagicMock,
    env: dict | None = None,
    confidence_band: str = "high",
    match_quality: str = "ast",
) -> dict:
    captured: list[str] = []
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "tool_response": {"success": True},
        "session_id": session_id,
    }
    arch = {
        "data": {
            "archetype": "component",
            "confidence_band": confidence_band,
            "match_quality": match_quality,
        }
    }
    run_env = {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}
    if env:
        run_env.update(env)

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, run_env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[arch, None]),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=loaded),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    out = "".join(captured).strip()
    return json.loads(out) if out else {}


def _run_stop(
    *,
    repo: Path,
    tmp_path: Path,
    session_id: str,
    trust_rec: MagicMock,
    loaded: LoadedProfile,
    env: dict | None = None,
) -> dict:
    captured: list[str] = []
    payload = {"session_id": session_id, "cwd": str(repo), "stop_hook_active": False}
    run_env = {"CHAMELEON_ENFORCE": "1"}
    if env:
        run_env.update(env)

    arch = {
        "data": {
            "archetype": "component",
            "confidence_band": "high",
            "match_quality": "ast",
        }
    }

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, run_env, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[arch, None]),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=loaded),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import stop_backstop

        stop_backstop()

    out = "".join(captured).strip()
    return json.loads(out) if out else {}


# --- HOLE 1: inline-ignore must clear the cached flag, not just the inline block ---


def test_inline_ignore_does_not_arm_stop_backstop(tmp_path: Path):
    repo, loaded = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"jsx-presence-mismatch": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Ignored.tsx"
    cand.write_text(JSX_IGNORE_SRC, encoding="utf-8")
    sid = "s-ignore-stop"
    _seed_level(tmp_path, sid, str(cand), level=LEVEL_L2)

    trust = _trust_rec()
    # The hash gate must pass: make the granted hash match the live profile hash.
    from chameleon_mcp.profile.trust import hash_profile

    trust.hash_for_root.return_value = hash_profile(repo / ".chameleon")

    verify_out = _run_verify(
        repo=repo,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        trust_rec=trust,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert verify_out.get("decision") != "block"  # inline ignore downgrades the inline block

    # The cached flag must NOT be armed, so the Stop backstop stays quiet.
    fs = load_state(tmp_path / REPO_ID, sid).files[str(cand)]
    assert fs.blockable_unresolved is False

    stop_out = _run_stop(
        repo=repo, tmp_path=tmp_path, session_id=sid, trust_rec=trust, loaded=loaded
    )
    assert stop_out.get("decision") != "block"


# --- HOLE 2: a resolved phantom must not block at Stop; the flag must heal ---


def test_resolved_phantom_does_not_block_stop_and_heals(tmp_path: Path):
    repo, loaded = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/a.ts"
    cand.write_text(PHANTOM_SRC, encoding="utf-8")
    sid = "s-phantom-heal"
    _seed_level(tmp_path, sid, str(cand), level=LEVEL_L2)

    trust = _trust_rec()
    from chameleon_mcp.profile.trust import hash_profile

    trust.hash_for_root.return_value = hash_profile(repo / ".chameleon")

    # First verify: the phantom import arms the cached flag at L2.
    _run_verify(
        repo=repo,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        trust_rec=trust,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    fs = load_state(tmp_path / REPO_ID, sid).files[str(cand)]
    assert fs.blockable_unresolved is True

    # The import target is created in a later edit; a.ts itself is NOT re-verified.
    (repo / "src" / "missing-target.ts").write_text("export const x = 1\n", encoding="utf-8")

    stop_out = _run_stop(
        repo=repo, tmp_path=tmp_path, session_id=sid, trust_rec=trust, loaded=loaded
    )
    assert stop_out.get("decision") != "block"

    # The stale flag is healed so the file is not re-checked next turn.
    fs2 = load_state(tmp_path / REPO_ID, sid).files[str(cand)]
    assert fs2.blockable_unresolved is False


def test_live_phantom_still_blocks_stop(tmp_path: Path):
    repo, loaded = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"phantom-import": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/b.ts"
    cand.write_text(PHANTOM_SRC, encoding="utf-8")  # target never created
    sid = "s-phantom-live"
    _seed_level(tmp_path, sid, str(cand), level=LEVEL_L2)

    trust = _trust_rec()
    from chameleon_mcp.profile.trust import hash_profile

    trust.hash_for_root.return_value = hash_profile(repo / ".chameleon")

    _run_verify(
        repo=repo,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        trust_rec=trust,
        env={"CHAMELEON_ENFORCE": "1"},
    )

    stop_out = _run_stop(
        repo=repo, tmp_path=tmp_path, session_id=sid, trust_rec=trust, loaded=loaded
    )
    assert stop_out.get("decision") == "block"


# --- HOLE 3: stale-trust profiles must not block/deny ---


def test_stale_trust_does_not_block_at_stop(tmp_path: Path):
    repo, loaded = _build_repo(tmp_path, mode="enforce")
    cand = repo / "src/c.ts"
    cand.write_text("export const C = 1\n", encoding="utf-8")
    sid = "s-stale-stop"
    st = EnforcementState()
    st.files[str(cand)] = FileState(level=LEVEL_L2, blockable_unresolved=True)
    save_state(st, tmp_path / REPO_ID, sid)

    # Granted hash differs from the live profile hash -> stale.
    trust = _trust_rec(hash_for_root="STALE-DOES-NOT-MATCH")

    stop_out = _run_stop(
        repo=repo, tmp_path=tmp_path, session_id=sid, trust_rec=trust, loaded=loaded
    )
    assert stop_out.get("decision") != "block"


def test_stale_trust_does_not_block_at_posttool(tmp_path: Path):
    repo, loaded = _build_repo(tmp_path, mode="enforce")
    write_block_rules(
        repo / ".chameleon",
        {"jsx-presence-mismatch": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Stale.tsx"
    cand.write_text(JSX_SRC, encoding="utf-8")
    sid = "s-stale-post"
    _seed_level(tmp_path, sid, str(cand), level=LEVEL_L2)

    trust = _trust_rec(hash_for_root="STALE-DOES-NOT-MATCH")

    # Staleness only exists under the kill switch; trust persists by default.
    out = _run_verify(
        repo=repo,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        trust_rec=trust,
        env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_TRUST_REVALIDATE": "1"},
    )
    assert out.get("decision") != "block"


def _run_preflight_pretool(*, repo, tmp_path, cand, trust_state: str) -> dict:
    """Drive preflight_and_advise with a banned import in the proposed content and
    a get_pattern_context result whose repo.trust_state is ``trust_state``."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(cand),
            "new_string": "import _ from 'lodash'\nexport default function widget() {}\n",
        },
        "session_id": "s-pre",
    }
    pattern_ctx = {
        "data": {
            "archetype": {
                "archetype": "component",
                "confidence_band": "high",
                "match_quality": "ast",
            },
            "canonical_excerpt": {"content": ""},
            "repo": {"id": REPO_ID, "trust_state": trust_state},
            "rules": [],
            "idioms": "",
        }
    }
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(
            os.environ,
            {"CHAMELEON_PLUGIN_DATA": str(tmp_path), "CHAMELEON_ENFORCE": "1"},
            clear=False,
        ),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=REPO_ID),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=Exception("no daemon")),
        patch("chameleon_mcp.tools.get_pattern_context", return_value=pattern_ctx),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()
    out = "".join(captured).strip()
    return json.loads(out) if out else {}


def _calibrate_competing_import(repo) -> None:
    chameleon = repo / ".chameleon"
    write_block_rules(
        chameleon,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    (chameleon / "conventions.json").write_text(
        json.dumps(
            {
                "conventions": {
                    "imports": {
                        "component": {"competing": [{"over": "lodash", "preferred": "lodash-es"}]}
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_trusted_does_deny_at_pretool(tmp_path: Path):
    # Baseline: a banned import in proposed content, with the rule active and the
    # archetype AST-confirmed at high confidence, denies under a "trusted" grant.
    repo, _ = _build_repo(tmp_path, mode="enforce")
    _calibrate_competing_import(repo)
    cand = repo / "src/Pre.ts"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("export default function widget() {}\n", encoding="utf-8")

    parsed = _run_preflight_pretool(repo=repo, tmp_path=tmp_path, cand=cand, trust_state="trusted")
    decision = (parsed.get("hookSpecificOutput") or {}).get("permissionDecision")
    assert decision == "deny"


def test_stale_trust_does_not_deny_at_pretool(tmp_path: Path):
    # The PreToolUse deny gate must require "trusted", not "stale": the same banned
    # import that denies under "trusted" must fall through to advisory under "stale".
    repo, _ = _build_repo(tmp_path, mode="enforce")
    _calibrate_competing_import(repo)
    cand = repo / "src/Pre.ts"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("export default function widget() {}\n", encoding="utf-8")

    parsed = _run_preflight_pretool(repo=repo, tmp_path=tmp_path, cand=cand, trust_state="stale")
    decision = (parsed.get("hookSpecificOutput") or {}).get("permissionDecision")
    assert decision != "deny"
