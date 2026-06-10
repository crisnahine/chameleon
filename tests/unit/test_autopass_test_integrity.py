"""Test-integrity facts through build_autopass_verdict.

The combination gate: test weakening (deleted test files, net test deletion,
added skip markers, assertion drop) defeats auto-pass eligibility only when
the same diff also changes live source. Pure test cleanup surfaces the facts
without adding a routing reason.
"""

from __future__ import annotations

from chameleon_mcp.autopass import build_autopass_verdict, count_deleted_test_files

_COMBO_REASON = "test weakening"


def _verdict(numstat, name_status, **kw):
    return build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
        **kw,
    )


def _diff(path, *, removed=(), added=()):
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1,9 +1,9 @@",
    ]
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def test_deleted_spec_alongside_source_change_routes():
    v = _verdict(
        "3\t1\tapp/models/listing.rb\n0\t40\tspec/models/listing_spec.rb\n",
        "M\tapp/models/listing.rb\nD\tspec/models/listing_spec.rb\n",
    )
    assert v["auto_pass_eligible"] is False
    assert v["risk"] == "high"
    assert any(_COMBO_REASON in r for r in v["reasons"])
    assert v["facts"]["deleted_test_files"] == 1


def test_deleted_spec_with_no_source_change_stays_eligible():
    v = _verdict(
        "0\t40\tspec/models/listing_spec.rb\n",
        "D\tspec/models/listing_spec.rb\n",
    )
    assert v["auto_pass_eligible"] is True
    assert v["facts"]["deleted_test_files"] == 1
    assert not any(_COMBO_REASON in r for r in v["reasons"])


def test_net_test_deletion_beyond_threshold_routes():
    v = _verdict(
        "3\t1\tapp/models/listing.rb\n2\t40\tspec/models/listing_spec.rb\n",
        "M\tapp/models/listing.rb\nM\tspec/models/listing_spec.rb\n",
    )
    assert v["facts"]["net_test_line_delta"] == -38
    assert v["auto_pass_eligible"] is False
    assert any(_COMBO_REASON in r for r in v["reasons"])


def test_small_net_test_deletion_stays_eligible():
    v = _verdict(
        "3\t1\tapp/models/listing.rb\n0\t5\tspec/models/listing_spec.rb\n",
        "M\tapp/models/listing.rb\nM\tspec/models/listing_spec.rb\n",
    )
    assert v["facts"]["net_test_line_delta"] == -5
    assert v["auto_pass_eligible"] is True


def test_added_skip_marker_alongside_source_change_routes():
    diff = _diff("src/user.test.ts", added=["it.skip('renders', () => {})"])
    v = _verdict(
        "3\t1\tsrc/user.ts\n1\t0\tsrc/user.test.ts\n",
        "M\tsrc/user.ts\nM\tsrc/user.test.ts\n",
        diff_text=diff,
    )
    assert v["facts"]["added_skip_markers"] == 1
    assert v["auto_pass_eligible"] is False
    assert any(_COMBO_REASON in r for r in v["reasons"])


def test_assertion_drop_at_floor_routes_but_above_floor_does_not():
    dropped_3 = _diff(
        "src/user.test.ts",
        removed=[
            "expect(a).toBe(1)",
            "expect(b).toBe(2)",
            "expect(c).toBe(3)",
        ],
    )
    v = _verdict(
        "3\t1\tsrc/user.ts\n0\t3\tsrc/user.test.ts\n",
        "M\tsrc/user.ts\nM\tsrc/user.test.ts\n",
        diff_text=dropped_3,
    )
    assert v["facts"]["assertion_delta"] == -3
    assert v["auto_pass_eligible"] is False

    dropped_2 = _diff(
        "src/user.test.ts",
        removed=["expect(a).toBe(1)", "expect(b).toBe(2)"],
    )
    v = _verdict(
        "3\t1\tsrc/user.ts\n0\t2\tsrc/user.test.ts\n",
        "M\tsrc/user.ts\nM\tsrc/user.test.ts\n",
        diff_text=dropped_2,
    )
    assert v["facts"]["assertion_delta"] == -2
    assert v["auto_pass_eligible"] is True


def test_no_diff_text_zeroes_content_facts_and_keeps_structural_gates():
    v = _verdict(
        "3\t1\tsrc/user.ts\n",
        "M\tsrc/user.ts\n",
        diff_text=None,
    )
    assert v["facts"]["removed_guard_lines"] == 0
    assert v["facts"]["ignore_directives_added"] == 0
    assert v["facts"]["added_skip_markers"] == 0
    assert v["facts"]["assertion_delta"] == 0
    assert v["facts"]["diff_scan_truncated"] is False
    assert v["auto_pass_eligible"] is True


def test_diff_truncated_flag_is_surfaced():
    v = _verdict(
        "3\t1\tsrc/user.ts\n",
        "M\tsrc/user.ts\n",
        diff_text="",
        diff_truncated=True,
    )
    assert v["facts"]["diff_scan_truncated"] is True


class TestCountDeletedTestFiles:
    def test_deleted_test_paths_count(self):
        text = "D\tspec/models/user_spec.rb\nD\tsrc/user.test.ts\n"
        assert count_deleted_test_files(text) == 2

    def test_deleted_source_paths_do_not_count(self):
        assert count_deleted_test_files("D\tapp/models/user.rb\n") == 0

    def test_renames_are_ignored(self):
        text = "R100\tspec/models/user_spec.rb\tspec/models/person_spec.rb\n"
        assert count_deleted_test_files(text) == 0

    def test_empty_text_is_zero(self):
        assert count_deleted_test_files("") == 0
        assert count_deleted_test_files(None) == 0
