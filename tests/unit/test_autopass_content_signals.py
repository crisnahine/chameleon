"""Deterministic diff-content signals for the auto-pass router.

scan_diff_signals reads a unified diff (zero LLM, zero subprocess) and counts
removed guard lines, in-diff chameleon-ignore directives, test skip markers,
and the assertion-count delta. Moved lines (identical text removed and
re-added) net out so a refactor that reorders callbacks stays quiet.
"""

from __future__ import annotations

import re

from chameleon_mcp.autopass import GUARD_LEXICON, scan_diff_signals


def _diff(path, *, removed=(), added=()):
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 0000000..1111111 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1,9 +1,9 @@",
    ]
    lines += [f"-{line}" for line in removed]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


class TestGuardLexicon:
    def test_public_constant_of_compiled_patterns(self):
        # Published interface: the intent-capture lens imports this lexicon.
        assert isinstance(GUARD_LEXICON, tuple)
        assert len(GUARD_LEXICON) > 0
        assert all(isinstance(p, re.Pattern) for p in GUARD_LEXICON)

    def test_removed_before_action_counts(self):
        diff = _diff(
            "app/controllers/orders_controller.rb",
            removed=["  before_action :authenticate_user!"],
        )
        assert scan_diff_signals(diff)["removed_guard_lines"] == 1

    def test_skip_before_action_with_verify_token_counts_via_verify(self):
        # before_action alone must NOT match skip_before_action (underscore is a
        # word char), but verify_authenticity_token still trips the verify_ arm.
        diff = _diff(
            "app/controllers/orders_controller.rb",
            removed=["  skip_before_action :verify_authenticity_token"],
        )
        assert scan_diff_signals(diff)["removed_guard_lines"] == 1
        assert not any(
            p.pattern == r"\bbefore_action\b" and p.search("skip_before_action :x")
            for p in GUARD_LEXICON
        )

    def test_skip_before_action_without_guard_token_does_not_count(self):
        diff = _diff(
            "app/controllers/orders_controller.rb",
            removed=["  skip_before_action :set_locale"],
        )
        assert scan_diff_signals(diff)["removed_guard_lines"] == 0

    def test_moved_guard_nets_to_zero(self):
        diff = _diff(
            "app/controllers/orders_controller.rb",
            removed=["  before_action :authenticate_user!"],
            added=["before_action :authenticate_user!"],
        )
        assert scan_diff_signals(diff)["removed_guard_lines"] == 0

    def test_swapped_guard_counts(self):
        # The removed authorize line does not reappear; an unrelated added
        # before_action must not mask it.
        diff = _diff(
            "app/controllers/orders_controller.rb",
            removed=["  authorize! :update, @listing"],
            added=["  before_action :load_user"],
        )
        assert scan_diff_signals(diff)["removed_guard_lines"] == 1


class TestIgnoreDirectives:
    def test_added_ts_directive_counts(self):
        diff = _diff(
            "src/config.ts",
            added=['const key = "x"; // chameleon-ignore secret-detected-in-content'],
        )
        assert scan_diff_signals(diff)["ignore_directives_added"] == 1

    def test_added_ruby_directive_counts(self):
        diff = _diff(
            "app/services/runner.rb",
            added=["eval(code) # chameleon-ignore eval-call"],
        )
        assert scan_diff_signals(diff)["ignore_directives_added"] == 1

    def test_directive_on_removed_line_only_is_zero(self):
        diff = _diff(
            "src/config.ts",
            removed=["// chameleon-ignore secret-detected-in-content"],
        )
        assert scan_diff_signals(diff)["ignore_directives_added"] == 0

    def test_moved_directive_nets_to_zero(self):
        line = "// chameleon-ignore secret-detected-in-content"
        diff = _diff("src/config.ts", removed=[f"  {line}"], added=[line])
        assert scan_diff_signals(diff)["ignore_directives_added"] == 0


class TestSkipMarkers:
    def test_added_skip_markers_in_test_file_count(self):
        diff = _diff(
            "src/user.test.ts",
            added=[
                "it.skip('renders', () => {})",
                "xdescribe('legacy suite', () => {})",
                "test.todo('rewrite this')",
            ],
        )
        assert scan_diff_signals(diff)["added_skip_markers"] == 3

    def test_same_lines_in_non_test_file_do_not_count(self):
        diff = _diff(
            "src/user.ts",
            added=[
                "it.skip('renders', () => {})",
                "xdescribe('legacy suite', () => {})",
            ],
        )
        assert scan_diff_signals(diff)["added_skip_markers"] == 0

    def test_ruby_pending_and_skip_count_in_spec(self):
        diff = _diff(
            "spec/models/user_spec.rb",
            added=["    pending", '    skip("flaky on CI")'],
        )
        assert scan_diff_signals(diff)["added_skip_markers"] == 2

    def test_skip_before_action_in_source_does_not_count(self):
        diff = _diff(
            "app/controllers/orders_controller.rb",
            added=["  skip_before_action :verify_authenticity_token"],
        )
        assert scan_diff_signals(diff)["added_skip_markers"] == 0


class TestAssertionDelta:
    def test_spec_losing_assertions_reads_negative(self):
        diff = _diff(
            "spec/models/user_spec.rb",
            removed=[
                "expect(user.name).to eq('a')",
                "expect(user.email).to eq('b')",
                "expect(user.role).to eq('c')",
                "expect(user.active).to be(true)",
            ],
            added=["expect(user).to be_valid"],
        )
        assert scan_diff_signals(diff)["assertion_delta"] == -3

    def test_non_test_files_contribute_nothing(self):
        diff = _diff(
            "src/user.ts",
            removed=["expect(user.name).toBe('a')", "assertValid(user)"],
        )
        assert scan_diff_signals(diff)["assertion_delta"] == 0


class TestRobustness:
    def test_empty_and_malformed_input_yield_zeroes(self):
        for text in (None, "", "not a diff at all", "+++ dangling\n@@@ junk\n+x\n-y"):
            out = scan_diff_signals(text)
            assert out["removed_guard_lines"] == 0
            assert out["ignore_directives_added"] == 0
            assert out["added_skip_markers"] == 0
            assert out["assertion_delta"] == 0
