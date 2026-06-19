from chameleon_mcp.autopass import build_autopass_verdict, count_added_files


def test_count_added_files():
    text = "A\tsrc/new.ts\nM\tsrc/old.ts\nD\tsrc/gone.ts\nR100\tsrc/a.ts\tsrc/b.ts\n"
    assert count_added_files(text) == 1


def test_count_added_files_empty():
    assert count_added_files("") == 0


def test_build_autopass_verdict_clean_change_eligible():
    numstat = "20\t5\tsrc/a.ts\n10\t0\tsrc/b.ts\n"
    name_status = "M\tsrc/a.ts\nA\tsrc/b.ts\n"

    verdict = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 1,
        block_findings_for=lambda p: 0,
    )

    assert verdict["auto_pass_eligible"] is True
    assert verdict["changed_files"] == ["src/a.ts", "src/b.ts"]
    assert verdict["facts"]["lines_changed"] == 35
    assert verdict["facts"]["new_files"] == 1


def test_build_autopass_verdict_failing_tests_route_to_human():
    # An otherwise-clean small change that fails the opt-in test run routes to a
    # human, with a recorded reason, like a type error does.
    numstat = "20\t5\tsrc/a.ts\n"
    name_status = "M\tsrc/a.ts\n"

    clean = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 1,
        block_findings_for=lambda p: 0,
    )
    assert clean["auto_pass_eligible"] is True
    assert clean["facts"]["tests_failed"] == 0

    failing = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 1,
        block_findings_for=lambda p: 0,
        tests_failed=True,
    )
    assert failing["auto_pass_eligible"] is False
    assert failing["facts"]["tests_failed"] == 1
    assert any("test suite failing" in r for r in failing["reasons"])


def test_build_autopass_verdict_security_change_routes_to_human():
    numstat = "3\t1\tapp/controllers/sessions_controller.rb\n"
    name_status = "M\tapp/controllers/sessions_controller.rb\n"

    verdict = build_autopass_verdict(
        numstat,
        name_status,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert verdict["auto_pass_eligible"] is False
    assert any("security-sensitive" in r for r in verdict["reasons"])


def test_build_autopass_verdict_empty_diff_is_eligible_and_zeroed():
    verdict = build_autopass_verdict(
        "",
        "",
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert verdict["changed_files"] == []
    assert verdict["facts"]["files_changed"] == 0
    assert verdict["auto_pass_eligible"] is True
