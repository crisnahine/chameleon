"""#13(b) honest idiom-overflow count + #11(b) corrupt-profile degraded banner.

Both close a silent coverage/state loss on the per-edit surface: the idiom block
now reports HOW MANY idioms the char cap dropped, and a corrupt/unsupported
profile emits a degraded banner instead of the same empty {} a healthy
unarchetyped edit produces (a silent-false-clean).
"""

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from chameleon_mcp.hook_helper import _IDIOM_CONTEXT_CHAR_CAP, _shape_idioms_for_block


def test_overflow_tail_reports_dropped_count():
    blocks = [f"### idiom{i}\n" + ("x" * 100) for i in range(40)]
    out = _shape_idioms_for_block("\n".join(blocks), "")
    tail = out.splitlines()[-1]
    assert "not shown" in tail
    # A real, positive count of dropped idiom headers.
    assert tail.startswith("... +") and "idiom(s) not shown" in tail
    assert ".chameleon/idioms.md" in tail


def test_single_huge_idiom_keeps_generic_tail():
    # One idiom whose body alone exceeds the cap: nothing is DROPPED (there is no
    # second header past the cut), so the honest count is 0 and the tail stays the
    # generic "truncated" form rather than a misleading "+0 not shown".
    out = _shape_idioms_for_block("### only\n" + ("y" * (_IDIOM_CONTEXT_CHAR_CAP + 500)), "")
    tail = out.splitlines()[-1]
    assert "not shown" not in tail
    assert "truncated" in tail


def test_new_overflow_tail_recognized_as_truncation():
    # v2.38.22 guard: _idiom_block_names must treat the "+N idiom(s) not shown"
    # tail as truncation, so the last (char-cut, header-only) block is excluded
    # from idioms_shown_names -- else the Stop review reduces a never-read idiom
    # to a name. Deterministic: the shaped text is constructed directly so the
    # test does not depend on where the char cut happens to land.
    from chameleon_mcp.tools import _idiom_block_names

    shaped = (
        "### fully_shown\nStatus: active\nA real description sentence.\n"
        "### boundary\nStatus: active\n"  # header only, description sliced away
        "... +2 idiom(s) not shown (see .chameleon/idioms.md)"
    )
    names = _idiom_block_names(shaped)
    assert "fully_shown" in names
    assert "boundary" not in names


def test_overflow_count_is_fence_aware():
    # A `### ` inside an example code fence in an idiom body must NOT be counted
    # as a dropped idiom header.
    fenced = (
        "### real1\nStatus: active\ndesc "
        + ("a" * 1600)
        + "\n```\n### not_an_idiom\n```\n### real2\nStatus: active\nd"
    )
    tail = _shape_idioms_for_block(fenced, "").splitlines()[-1]
    # Only real2 is dropped; the fenced pseudo-header is not counted.
    assert "+1 idiom(s) not shown" in tail


def _envelope(profile_status: str) -> dict:
    return {
        "data": {
            "archetype": {"archetype": None, "match_quality": "none", "confidence_band": "low"},
            "canonical_excerpt": {},
            "rules": [],
            "idioms": "",
            "repo": {"id": "r", "trust_state": "n/a", "profile_status": profile_status},
        }
    }


def _run_preflight(tmp_path, profile_status):
    file_path = str(tmp_path / "src" / "user.ts")
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(file_path).write_text("export const x = 1;\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "new_string": "export const x = 2;"},
        "session_id": "sess-degraded",
    }
    captured: list[str] = []
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout") as mock_stdout,
        patch.dict(os.environ, {"CHAMELEON_PLUGIN_DATA": str(tmp_path / "data")}, clear=False),
        patch("chameleon_mcp.daemon_client.call", return_value=_envelope(profile_status)),
        patch("chameleon_mcp.profile.loader.find_repo_root", return_value=tmp_path),
        patch("chameleon_mcp.tools._compute_repo_id", return_value="r"),
        patch("chameleon_mcp.optouts.is_chameleon_suppressed", return_value=None),
        patch(
            "chameleon_mcp.tools.get_pattern_context",
            MagicMock(return_value=_envelope(profile_status)),
        ),
        patch("chameleon_mcp.drift.observations.record_edit_observation"),
        patch("chameleon_mcp.metrics.emit_hook_metric"),
    ):
        mock_stdout.write = lambda s: captured.append(s)
        from chameleon_mcp.hook_helper import preflight_and_advise

        preflight_and_advise()
    return "".join(captured)


def test_corrupt_profile_emits_degraded_banner(tmp_path):
    out = _run_preflight(tmp_path, "profile_corrupted")
    assert "profile degraded" in out.lower()
    assert "search_codebase" in out  # tells the model to fall back to comprehension


def test_unsupported_schema_steers_to_upgrade_not_refresh(tmp_path):
    out = _run_preflight(tmp_path, "profile_unsupported_schema_version")
    assert "profile degraded" in out.lower()
    # A too-new profile must steer to UPGRADE, not a dead-end refresh.
    assert "newer chameleon" in out.lower()
    assert "Upgrade chameleon" in out


def test_profile_too_new_steers_to_upgrade(tmp_path):
    out = _run_preflight(tmp_path, "profile_too_new")
    assert "profile degraded" in out.lower()
    assert "Upgrade chameleon" in out


def test_corrupt_profile_steers_to_refresh(tmp_path):
    out = _run_preflight(tmp_path, "profile_corrupted")
    assert "/chameleon-refresh" in out
    assert "Upgrade chameleon" not in out


def test_healthy_no_archetype_stays_silent(tmp_path):
    # profile_present + no archetype (a genuine unarchetyped edit, e.g. a config
    # file) must NOT emit the degraded banner -- that would be false noise.
    out = _run_preflight(tmp_path, "profile_present")
    assert "profile degraded" not in out.lower()
