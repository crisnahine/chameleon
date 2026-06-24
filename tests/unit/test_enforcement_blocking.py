"""Enforcement-decision tests for posttool_verify() at L2.

Task 10 inserts a hard-class blocking decision between computing violations and
emitting the advisory. A hard-class violation (a rule in the repo's active block
set), on a file already at L2, with the archetype gates satisfied
(confidence_band == "high" and match_quality == "ast"), blocks the edit when the
repo's enforcement mode is "enforce". In "shadow" mode the same situation logs a
would_block metric but never blocks. A soft-class violation (a learned heuristic
not in the active block set) never blocks regardless of level or mode.

Isolation follows the sibling deviation test (no conftest): each run pins
CHAMELEON_PLUGIN_DATA at tmp_path, mocks repo/trust resolution, and forces the
in-process lint path (daemon get_archetype mocked, lint_file -> None) so the real
lint engine produces the violations. The archetype mock carries the
confidence_band/match_quality the enforcement gate reads.
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
    save_state,
)
from chameleon_mcp.enforcement_calibration import write_block_rules
from chameleon_mcp.profile.loader import LoadedProfile

# A witness fixing the archetype shape to a function default export, so a file
# that ALSO default-exports a function is clean against the shape and the only
# violation is the competing-import one we want to test in isolation.
WITNESS_REL = "src/widget.ts"
WITNESS_SRC = "export default function widget() {}\n"

# Violates the competing-import convention (imports lodash, never references the
# preferred lodash-es) while matching the archetype shape (function default
# export), so import-preference-violation is the sole violation.
LODASH_SRC = "import _ from 'lodash'\nexport default function widget() {}\n"

# No competing import; matches the archetype shape. Used for the soft-class case
# where we instead expect a naming/shape violation that is NOT block-eligible.
CLEAN_SHAPE_SRC = "export default function widget() {}\n"

# A class default export violates the function-default-export archetype shape,
# producing a default-export-kind violation (a learned, archetype-dependent
# heuristic) that is NOT in any active block set.
SHAPE_VIOLATION_SRC = "export default class Widget {}\n"

# JSX in a non-JSX archetype produces a jsx-presence-mismatch error. Unlike the
# competing-import rule, the AST-query lint path emits this even with a
# chameleon-ignore directive present, so it reaches the block site untouched and
# exercises the block-site ignore check directly.
JSX_SRC = "const x = <div />\nexport default function widget() { return null }\n"
JSX_IGNORE_SRC = "// chameleon-ignore jsx-presence-mismatch\n" + JSX_SRC


def _build_repo(
    tmp_path: Path,
    *,
    mode: str,
    with_competing_import: bool,
) -> tuple[Path, str, LoadedProfile]:
    """Create a synthetic repo + in-memory profile.

    ``mode`` is written into ``.chameleon/config.json`` (enforcement.mode).
    ``with_competing_import`` adds a lodash -> lodash-es competing-import rule to
    the per-archetype conventions so lint_conventions emits
    import-preference-violation.
    """
    repo_id = "enforce_repo_id"
    (tmp_path / repo_id).mkdir(exist_ok=True)

    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": mode}}), encoding="utf-8"
    )
    # profile.json gives hash_profile() a non-empty hash; the trust record in
    # _run_verify mirrors it so the PostToolUse block's not-stale gate reads
    # "trusted" rather than "stale".
    (chameleon / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    witness = repo / WITNESS_REL
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.write_text(WITNESS_SRC, encoding="utf-8")

    conventions: dict = {"conventions": {}}
    if with_competing_import:
        conventions = {
            "conventions": {
                "imports": {
                    "component": {"competing": [{"over": "lodash", "preferred": "lodash-es"}]}
                }
            }
        }

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
        conventions=conventions,
        idioms_text="",
        generation=1,
        profile_dir=chameleon,
    )
    return repo, repo_id, loaded


def _seed_level(
    tmp_path: Path,
    repo_id: str,
    session_id: str,
    file_path: str,
    *,
    level: int,
) -> None:
    state = EnforcementState()
    state.files[file_path] = FileState(
        level=level,
        violation_count=3,
        correction_count=0,
        last_violation_at=time.time() - 1000,
    )
    save_state(state, tmp_path / repo_id, session_id)


def _run_verify(
    *,
    repo: Path,
    repo_id: str,
    loaded: LoadedProfile,
    tmp_path: Path,
    file_path: str,
    session_id: str,
    env: dict | None = None,
    confidence_band: str = "high",
    match_quality: str = "ast",
    stale: bool = False,
) -> dict:
    """Drive posttool_verify() through the in-process lint fallback.

    The first daemon_client.call (get_archetype) returns the archetype plus the
    confidence_band/match_quality the enforcement gate reads; the second
    (lint_file) returns None, forcing the real in-process lint engine to run.

    ``stale=True`` makes the trust record's granted hash diverge from the live
    profile hash, so the not-stale block gate must fall through to advisory.
    """
    from chameleon_mcp.profile.trust import hash_profile

    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True
    trust_rec.hash_for_root.return_value = (
        "STALE-DOES-NOT-MATCH" if stale else hash_profile(repo / ".chameleon")
    )

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
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[arch, None]),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=loaded),
    ):
        mock_stdout.write = captured.append
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    output = "".join(captured).strip()
    return json.loads(output) if output else {}


def test_hard_class_l2_blocks_in_enforce_mode(tmp_path: Path):
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Bad.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-enforce"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") == "block"


def test_stale_trust_does_not_block_at_l2(tmp_path: Path):
    # The same hard-class violation that blocks under a trusted grant must fall
    # through to advisory when the grant is stale (granted hash drifted).
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Stale.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-stale"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    # Staleness only exists under the kill switch; trust persists by default.
    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1", "CHAMELEON_TRUST_REVALIDATE": "1"},
        stale=True,
    )
    assert out.get("decision") != "block"


def test_shadow_mode_does_not_block(tmp_path: Path):
    repo, repo_id, loaded = _build_repo(tmp_path, mode="shadow", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Shadow.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-shadow"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"  # advisory only


def test_soft_class_never_blocks(tmp_path: Path):
    # A default-export-kind violation is archetype-dependent and not in any
    # active block set, so it stays advisory even at L2 in enforce mode.
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=False)
    cand = repo / "src/Soft.ts"
    cand.write_text(SHAPE_VIOLATION_SRC, encoding="utf-8")
    sid = "s-soft"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"


def test_enforce_off_env_does_not_block(tmp_path: Path):
    # CHAMELEON_ENFORCE=0 forces advisory even when everything else would block.
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Off.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-off"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "0"},
    )
    assert out.get("decision") != "block"


def test_gate_fails_when_match_quality_not_ast(tmp_path: Path):
    # A hard-class violation at L2 in enforce mode still falls through to advisory
    # if the archetype match was not AST-confirmed.
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Heur.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-heur"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
        match_quality="heuristic",
    )
    assert out.get("decision") != "block"


def test_jsx_mismatch_blocks_without_ignore(tmp_path: Path):
    # Baseline: a jsx-presence-mismatch error, with the rule in the active block
    # set, at L2 in enforce mode, blocks. The AST-query lint path emits this even
    # though no chameleon-ignore directive is present.
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=False)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"jsx-presence-mismatch": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Jsx.tsx"
    cand.write_text(JSX_SRC, encoding="utf-8")
    sid = "s-jsx"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") == "block"


def test_inline_ignore_downgrades_block(tmp_path: Path):
    # A `// chameleon-ignore jsx-presence-mismatch` directive in the file
    # downgrades the same hard-class block to advisory, even at L2 in enforce
    # mode. The AST-query lint path still emits the violation, so this exercises
    # the block-site ignore check rather than upstream lint suppression.
    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=False)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"jsx-presence-mismatch": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Ignored.tsx"
    cand.write_text(JSX_IGNORE_SRC, encoding="utf-8")
    sid = "s-ignore"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L2)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"


def test_hard_class_below_l2_does_not_block(tmp_path: Path):
    # The same hard-class violation in enforce mode does not block while the file
    # is still below L2 after recording. Seeded at L0, a single violation
    # escalates to L1, one step short of the blocking threshold.
    from chameleon_mcp.enforcement import LEVEL_L0

    repo, repo_id, loaded = _build_repo(tmp_path, mode="enforce", with_competing_import=True)
    profile_dir = repo / ".chameleon"
    write_block_rules(
        profile_dir,
        {"import-preference-violation": {"active": True, "fp_rate": 0.0, "sampled": 3}},
    )
    cand = repo / "src/Early.ts"
    cand.write_text(LODASH_SRC, encoding="utf-8")
    sid = "s-early"
    _seed_level(tmp_path, repo_id, sid, str(cand), level=LEVEL_L0)

    out = _run_verify(
        repo=repo,
        repo_id=repo_id,
        loaded=loaded,
        tmp_path=tmp_path,
        file_path=str(cand),
        session_id=sid,
        env={"CHAMELEON_ENFORCE": "1"},
    )
    assert out.get("decision") != "block"
