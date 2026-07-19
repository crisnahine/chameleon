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


def test_titles_kept_after_shaping_excludes_char_cut_tail_block():
    # The block the char cap lands IN can have its header (and metadata)
    # survive with its description sliced away entirely -- such a tail block
    # must be excluded from the "shown" set, or the Stop review would reduce a
    # never-read idiom to a name. Computed directly from the pre-cap block
    # split (no re-parsing of the rendered tail marker), so the boundary is
    # constructed exactly at the end of the second block's metadata line.
    from chameleon_mcp.hook_helper import _IDIOM_CONTEXT_CHAR_CAP, _idiom_titles_kept_after_shaping

    header2 = "### boundary\nStatus: active\n"
    first_head = "### fully_shown\nStatus: active\nA real description sentence.\n"
    pad = _IDIOM_CONTEXT_CHAR_CAP - len(first_head) - 1 - len(header2)
    assert pad > 0
    first = first_head + ("z" * pad) + "\n"
    second = header2 + "The real description, sliced away by the cut.\n"
    text = first + second
    assert len(first) + len(header2) == _IDIOM_CONTEXT_CHAR_CAP

    titles = _idiom_titles_kept_after_shaping(text, "")
    assert titles == {"fully_shown"}


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


# --------------------------------------------------------------------------- #
# Witness-dedup must not reach inside fenced code examples. The per-edit dedup
# drops idiom lines that appear verbatim in the canonical witness (redundant
# prose the model can read off the witness). Applied line-by-line with no fence
# awareness, it deleted INTERIOR lines of a taught Example/Counterexample --
# and a good idiom example resembles the canonical file by construction, so the
# collision is near-certain, not incidental. The model was then handed a
# syntactically broken (or semantically inverted) example to imitate: strictly
# worse than dropping the idiom. Reported independently in C5, C6, C9, C10.
# --------------------------------------------------------------------------- #

from chameleon_mcp.hook_helper import _witness_dedup_idiom_lines


def test_dedup_preserves_fenced_example_lines_shared_with_witness():
    idiom = (
        "### commit-in-service-only\n"
        "Only a service calls commit().\n"
        "\n"
        "Example:\n"
        "```\n"
        "def create(self, payload):\n"
        "    obj = Model(**payload)\n"
        "    self.repository.commit()\n"
        "    return obj\n"
        "```\n"
    )
    # A realistic canonical witness that shares the boilerplate lines.
    witness = (
        "class CarrierService:\n"
        "    def create(self, payload):\n"
        "        obj = Model(**payload)\n"
        "        return obj\n"
    )
    out = _witness_dedup_idiom_lines(idiom, witness)
    # Every line INSIDE the fence survives verbatim -- the example must parse.
    for code_line in (
        "def create(self, payload):",
        "obj = Model(**payload)",
        "self.repository.commit()",
        "return obj",
    ):
        assert code_line in out, f"fenced example lost {code_line!r}"


def test_dedup_still_drops_redundant_prose_outside_fences():
    # The dedup's real job is unchanged: a PROSE line the witness already shows
    # verbatim is still redundant and still dropped.
    idiom = "### x\nStatus: active\nshared prose line\ntail prose\n"
    witness = "shared prose line\n"
    out = _witness_dedup_idiom_lines(idiom, witness)
    assert "shared prose line" not in out
    assert "tail prose" in out


def test_dedup_handles_counterexample_fence_too():
    idiom = "### x\nCounterexample:\n```\nshipment = Shipment.new\nshipment.save\n```\n"
    witness = "shipment = Shipment.new\nother\n"
    out = _witness_dedup_idiom_lines(idiom, witness)
    assert "shipment = Shipment.new" in out
    assert "shipment.save" in out


def test_dedup_survives_unterminated_fence_without_dropping_code():
    # A malformed idiom (fence never closed) must fail safe: keep the code region
    # rather than dedup into it.
    idiom = "### x\nExample:\n```\ndef create(self, payload):\n    return obj\n"
    witness = "    def create(self, payload):\n        return obj\n"
    out = _witness_dedup_idiom_lines(idiom, witness)
    assert "def create(self, payload):" in out
    assert "return obj" in out
