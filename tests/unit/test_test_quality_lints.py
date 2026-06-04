"""Unit tests for the test-quality lints in lint_engine.lint_conventions.

These rules fire only when archetype_name marks the file as a test (starts with
test/spec) and stay advisory (severity info, never block-eligible).
"""

from __future__ import annotations

from chameleon_mcp.lint_engine import lint_conventions

# A non-empty conventions dict so lint_conventions does not early-return; the
# test-quality pass does not consume these keys, it gates on archetype_name.
_CONV: dict = {"imports": {"competing": []}}


def _rules(violations) -> set[str]:
    return {v.rule for v in violations}


class TestArchetypeGate:
    def test_inert_without_archetype_name(self):
        # Default archetype_name=None must leave the pass off entirely.
        content = "it.skip('x', () => { expect(true).toBe(true); });\n"
        violations = lint_conventions(content, _CONV, language="typescript")
        assert _rules(violations) == set()

    def test_inert_for_non_test_archetype(self):
        content = "it.skip('x', () => {});\n"
        violations = lint_conventions(
            content, _CONV, language="typescript", archetype_name="service"
        )
        assert "skipped-test" not in _rules(violations)

    def test_fires_for_test_prefix(self):
        content = "it.skip('x', () => {});\n"
        violations = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" in _rules(violations)

    def test_fires_for_spec_prefix(self):
        content = "xit 'does a thing' do\nend\n"
        violations = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "skipped-test" in _rules(violations)

    def test_all_advisory(self):
        content = (
            "it.skip('a', () => {});\n"
            "it('b', () => { expect(true).toBe(true); });\n"
            "it('c', () => { Math.random(); });\n"
        )
        violations = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert violations
        assert all(v.severity == "info" for v in violations)


class TestSkippedTest:
    def test_ts_it_skip(self):
        content = "describe('x', () => { it.skip('y', () => {}); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" in _rules(v)

    def test_ts_xit(self):
        content = "xit('y', () => {});\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" in _rules(v)

    def test_ts_describe_skip(self):
        content = "describe.skip('y', () => {});\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" in _rules(v)

    def test_ruby_pending(self):
        content = "it 'y' do\n  pending\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "skipped-test" in _rules(v)

    def test_ruby_skip(self):
        content = "it 'y' do\n  skip 'flaky'\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "skipped-test" in _rules(v)

    def test_skip_in_string_does_not_fire(self):
        # `pending` inside a description string is stripped before the scan.
        content = "it('handles a pending order', () => { expect(o).toBe(p); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" not in _rules(v)


class TestTautology:
    def test_expect_true_to_be_true(self):
        content = "it('x', () => { expect(true).toBe(true); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "tautological-assertion" in _rules(v)

    def test_expect_one_to_equal_one(self):
        content = "it('x', () => { expect(1).toEqual(1); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "tautological-assertion" in _rules(v)

    def test_real_assertion_does_not_fire(self):
        content = "it('x', () => { expect(result).toBe(true); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "tautological-assertion" not in _rules(v)


class TestRealSleep:
    def test_ts_settimeout_wait(self):
        content = "it('x', async () => { await new Promise(r => setTimeout(r, 500)); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "real-sleep-in-test" in _rules(v)

    def test_ruby_sleep(self):
        content = "it 'x' do\n  sleep 2\n  expect(x).to eq(1)\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "real-sleep-in-test" in _rules(v)

    def test_no_sleep_does_not_fire(self):
        content = "it('x', () => { expect(x).toBe(1); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "real-sleep-in-test" not in _rules(v)


class TestRandom:
    def test_ts_math_random(self):
        content = "it('x', () => { const n = Math.random(); expect(n).toBeLessThan(1); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "random-in-test" in _rules(v)

    def test_ruby_rand(self):
        content = "it 'x' do\n  n = rand(10)\n  expect(n).to be < 10\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "random-in-test" in _rules(v)

    def test_ruby_securerandom(self):
        content = "it 'x' do\n  t = SecureRandom.hex\n  expect(t).to be_present\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "random-in-test" in _rules(v)

    def test_no_random_does_not_fire(self):
        content = "it('x', () => { expect(x).toBe(1); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "random-in-test" not in _rules(v)


class TestAssertionFree:
    def test_block_with_no_assertion_flags(self):
        content = "it('x', () => { const u = makeUser(); save(u); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "assertion-free-test" in _rules(v)

    def test_block_with_expect_does_not_flag(self):
        content = "it('x', () => { const u = makeUser(); expect(u.id).toBe(1); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "assertion-free-test" not in _rules(v)

    def test_helper_wrapped_assert_not_flagged_with_witness(self):
        # The witness wraps asserts in assertUser(); a candidate that calls the
        # same helper must not be flagged as assertion-free.
        witness = "it('w', () => { const u = makeUser(); assertUser(u); });\n"
        candidate = "it('x', () => { const u = makeUser(); assertUser(u); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "assertion-free-test" not in _rules(v)

    def test_helper_wrapped_assert_flagged_without_witness(self):
        # No witness -> no derived helper vocabulary -> the helper-wrapped block
        # has no recognized assertion token, so it flags. Acceptable advisory FP
        # the witness is meant to suppress.
        candidate = "it('x', () => { const u = makeUser(); assertUser(u); });\n"
        v = lint_conventions(candidate, _CONV, language="typescript", archetype_name="test")
        assert "assertion-free-test" in _rules(v)

    def test_ruby_block_with_no_assertion_flags(self):
        content = "it 'creates a user' do\n  user = create(:user)\n  user.save\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "assertion-free-test" in _rules(v)

    def test_ruby_block_with_should_does_not_flag(self):
        content = (
            "it 'creates a user' do\n  user = create(:user)\n  expect(user).to be_valid\nend\n"
        )
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "assertion-free-test" not in _rules(v)


class TestUnstubbedNetwork:
    def test_flags_when_witness_stubs_and_candidate_does_not(self):
        witness = "it('w', () => { nock('http://x'); fetch('http://x'); });\n"
        candidate = "it('x', () => { const r = fetch('http://api'); expect(r).toBeDefined(); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "unstubbed-network" in _rules(v)

    def test_silent_without_witness(self):
        candidate = "it('x', () => { const r = fetch('http://api'); expect(r).toBeDefined(); });\n"
        v = lint_conventions(candidate, _CONV, language="typescript", archetype_name="test")
        assert "unstubbed-network" not in _rules(v)

    def test_silent_when_witness_does_not_stub(self):
        witness = "it('w', () => { const r = fetch('http://x'); expect(r).toBeDefined(); });\n"
        candidate = "it('x', () => { const r = fetch('http://api'); expect(r).toBeDefined(); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "unstubbed-network" not in _rules(v)

    def test_silent_when_candidate_also_stubs(self):
        witness = "it('w', () => { nock('http://x'); fetch('http://x'); });\n"
        candidate = "it('x', () => { nock('http://api'); fetch('http://api'); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "unstubbed-network" not in _rules(v)

    def test_ruby_webmock(self):
        witness = "it 'w' do\n  stub_request(:get, 'http://x')\n  Net::HTTP.get(uri)\nend\n"
        candidate = "it 'x' do\n  body = Net::HTTP.get(uri)\n  expect(body).to be_present\nend\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="ruby",
            archetype_name="spec",
            witness_content=witness,
        )
        assert "unstubbed-network" in _rules(v)


class TestUnfrozenClock:
    def test_flags_when_witness_freezes_and_candidate_does_not(self):
        witness = "it('w', () => { jest.useFakeTimers(); const n = Date.now(); });\n"
        candidate = "it('x', () => { const n = Date.now(); expect(n).toBeGreaterThan(0); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "unfrozen-clock" in _rules(v)

    def test_ruby_freeze_time(self):
        witness = "it 'w' do\n  freeze_time\n  t = Time.now\nend\n"
        candidate = "it 'x' do\n  t = Time.now\n  expect(t).to be_a(Time)\nend\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="ruby",
            archetype_name="spec",
            witness_content=witness,
        )
        assert "unfrozen-clock" in _rules(v)

    def test_silent_when_candidate_does_not_read_clock(self):
        witness = "it('w', () => { jest.useFakeTimers(); const n = Date.now(); });\n"
        candidate = "it('x', () => { expect(1).toBe(1); });\n"
        v = lint_conventions(
            candidate,
            _CONV,
            language="typescript",
            archetype_name="test",
            witness_content=witness,
        )
        assert "unfrozen-clock" not in _rules(v)


class TestIgnoreDirective:
    def test_chameleon_ignore_suppresses_test_quality(self):
        content = "// chameleon-ignore test-quality\nit.skip('x', () => {});\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert "skipped-test" not in _rules(v)

    def test_ruby_ignore_suppresses_test_quality(self):
        content = "# chameleon-ignore test-quality\nit 'x' do\n  skip\nend\n"
        v = lint_conventions(content, _CONV, language="ruby", archetype_name="spec")
        assert "skipped-test" not in _rules(v)


class TestRobustness:
    def test_empty_content(self):
        v = lint_conventions("", _CONV, language="typescript", archetype_name="test")
        assert isinstance(v, list)

    def test_unbalanced_braces_does_not_crash(self):
        content = "it('x', () => { const u = makeUser();\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert isinstance(v, list)

    def test_unicode_content(self):
        content = "it('日本語', () => { expect(café).toBe('résumé'); });\n"
        v = lint_conventions(content, _CONV, language="typescript", archetype_name="test")
        assert isinstance(v, list)
