"""The corrections-exhausted breaker must not arm the Stop backstop on a
non-code file's credential.

posttool_verify has a circuit breaker: once a file has been corrected
``MAX_CORRECTIONS_PER_FILE`` times, advisory feedback is suppressed, but a
deterministic-hard secret still arms the Stop backstop so a credential cannot
slip in unblocked. Every OTHER arming site (and both Stop re-lint branches)
runs that secret through ``block_eligible_on_file(..., language=detect_language)``
so a credential-shaped token in markdown / config PROSE (no recognized
language) stays advisory and never arms the backstop -- such a file has no
inline ``chameleon-ignore`` escape, and the re-lint would drop it anyway.

This breaker site did not apply that gate, so a non-code file could be armed
(``blockable_unresolved=True``) inconsistently with the re-lint that then has
to clear it. Arm only when the file is a recognized code language, matching the
sibling sites.
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
    MAX_CORRECTIONS_PER_FILE,
    EnforcementState,
    FileState,
    load_state,
    save_state,
)
from chameleon_mcp.profile.loader import LoadedProfile

WITNESS_REL = "src/widget.ts"
WITNESS_SRC = "export default function widget() {}\n"
SECRET_CONTENT = "key = AKIAIOSFODNN7EXAMPLE\nsecret = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"


def _build_loaded(chameleon: Path) -> LoadedProfile:
    return LoadedProfile(
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


def _run_breaker(tmp_path: Path, filename: str) -> FileState | None:
    """Drive posttool_verify on a file already at the corrections cap with a
    hard secret in its content. Return the file's FileState after the run."""
    repo_id = "breaker_repo_id"
    state_dir = tmp_path / repo_id
    state_dir.mkdir(parents=True, exist_ok=True)

    repo = tmp_path / "repo"
    chameleon = repo / ".chameleon"
    chameleon.mkdir(parents=True, exist_ok=True)
    (chameleon / "config.json").write_text(
        json.dumps({"enforcement": {"mode": "enforce"}}), encoding="utf-8"
    )
    (chameleon / "profile.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / WITNESS_REL).write_text(WITNESS_SRC, encoding="utf-8")

    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SECRET_CONTENT, encoding="utf-8")
    file_path = str(target)

    # Pre-seed the file at the corrections cap with a recent timestamp so the
    # 60s reset window does not zero it before the breaker check.
    now = time.time()
    seed = EnforcementState()
    seed.files[file_path] = FileState(
        level=LEVEL_L2,
        correction_count=MAX_CORRECTIONS_PER_FILE,
        last_violation_at=now,
        last_verified_at=now,
    )
    save_state(seed, state_dir, "breaker-session")

    loaded = _build_loaded(chameleon)
    trust_rec = MagicMock()
    trust_rec.grants_root.return_value = True
    from chameleon_mcp.profile.trust import hash_profile

    trust_rec.hash_for_root.return_value = hash_profile(chameleon)

    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "tool_response": {"success": True},
        "session_id": "breaker-session",
    }
    arch = {"data": {"archetype": "component", "confidence_band": "high", "match_quality": "ast"}}

    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path)}, clear=False),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=repo),
        patch("chameleon_mcp.profile.trust.trust_state_for", return_value=trust_rec),
        patch("chameleon_mcp.tools._compute_repo_id", return_value=repo_id),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch("chameleon_mcp.hook_helper._plugin_data_dir", return_value=tmp_path),
        patch("chameleon_mcp.daemon_client.call", side_effect=[arch, None]),
        patch("chameleon_mcp.profile.loader.load_profile_dir", return_value=loaded),
    ):
        mock_stdout.write = lambda s: None
        from chameleon_mcp.hook_helper import posttool_verify

        posttool_verify()

    after = load_state(state_dir, "breaker-session")
    return after.files.get(file_path)


def test_breaker_does_not_arm_noncode_file(tmp_path: Path) -> None:
    fs = _run_breaker(tmp_path, "docs/NOTES.md")
    assert fs is not None
    assert fs.blockable_unresolved is False, (
        "a non-code file's credential must stay advisory at the corrections "
        "breaker -- it has no inline chameleon-ignore escape and the Stop "
        "re-lint drops it, so arming the backstop only over-arms"
    )


def test_breaker_still_arms_code_file(tmp_path: Path) -> None:
    # The fix must be targeted: a real CODE file with a credential still arms
    # the backstop at the breaker (the breaker's whole purpose).
    fs = _run_breaker(tmp_path, "src/leak.ts")
    assert fs is not None
    assert fs.blockable_unresolved is True, (
        "a code file's hardcoded credential must still arm the Stop backstop "
        "when advisory feedback is suppressed by the corrections breaker"
    )
