from chameleon_mcp.autopass import classify_change


def _facts(**over):
    # A small, in-pattern change with no grounded findings and no risky surface:
    # the routine slice the router exists to auto-pass.
    base = {
        "files_changed": 2,
        "lines_changed": 40,
        "new_files": 0,
        "unarchetyped_files": 0,
        "blast_radius": 1,
        "active_block_findings": 0,
        "security_surface": False,
    }
    base.update(over)
    return base


def test_small_in_pattern_clean_change_is_auto_pass_eligible():
    verdict = classify_change(_facts())

    assert verdict["auto_pass_eligible"] is True
    assert verdict["risk"] == "low"
    assert verdict["reasons"] == []


def test_grounded_blocking_finding_routes_to_human():
    verdict = classify_change(_facts(active_block_findings=1))

    assert verdict["auto_pass_eligible"] is False
    assert verdict["risk"] == "high"
    assert any("blocking finding" in r for r in verdict["reasons"])


def test_security_surface_routes_to_human_even_when_otherwise_clean():
    # The defining rule: a security-sensitive change is never auto-passable,
    # however small and in-pattern it looks.
    verdict = classify_change(_facts(security_surface=True))

    assert verdict["auto_pass_eligible"] is False
    assert verdict["risk"] == "high"
    assert any("security-sensitive" in r for r in verdict["reasons"])


def test_large_change_routes_to_human():
    verdict = classify_change(_facts(files_changed=40, lines_changed=900))

    assert verdict["auto_pass_eligible"] is False
    assert verdict["risk"] == "elevated"
    assert any("too large" in r for r in verdict["reasons"])


def test_high_blast_radius_routes_to_human():
    verdict = classify_change(_facts(blast_radius=50))

    assert verdict["auto_pass_eligible"] is False
    assert any("blast radius" in r for r in verdict["reasons"])


def test_unarchetyped_file_routes_to_human():
    # A file the engine has no canonical for cannot be vouched for.
    verdict = classify_change(_facts(unarchetyped_files=1))

    assert verdict["auto_pass_eligible"] is False
    assert any("outside profiled archetypes" in r for r in verdict["reasons"])


def test_multiple_failing_gates_all_reported():
    verdict = classify_change(
        _facts(active_block_findings=2, security_surface=True, blast_radius=99)
    )

    assert verdict["auto_pass_eligible"] is False
    assert len(verdict["reasons"]) == 3


def test_missing_facts_default_safe_and_do_not_block_a_clean_minimal_change():
    # Absent keys default to 0/False: a minimal fact set with nothing risky is
    # eligible, but absence is never read as a risky positive that wrongly blocks.
    verdict = classify_change({})

    assert verdict["auto_pass_eligible"] is True
    assert verdict["reasons"] == []


def test_type_errors_route_to_human():
    # Execution-grounded: a change that does not typecheck is never auto-passable,
    # and it is a high-confidence (grounded) reason, like a block finding.
    verdict = classify_change(_facts(type_errors=2))

    assert verdict["auto_pass_eligible"] is False
    assert verdict["risk"] == "high"
    assert any("type error" in r for r in verdict["reasons"])
