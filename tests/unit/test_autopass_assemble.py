from chameleon_mcp.autopass import assemble_facts, classify_change


def test_assemble_facts_composes_signals():
    changed = ["src/a.ts", "src/auth/login.ts", "src/b.ts"]
    importers = {"src/a.ts": 2, "src/auth/login.ts": 0, "src/b.ts": 5}

    facts = assemble_facts(
        changed,
        added_lines=30,
        removed_lines=10,
        new_files=1,
        is_unarchetyped=lambda p: p == "src/b.ts",
        importers_of=lambda p: importers[p],
        block_findings_for=lambda p: 1 if p == "src/a.ts" else 0,
    )

    assert facts["files_changed"] == 3
    assert facts["lines_changed"] == 40
    assert facts["new_files"] == 1
    assert facts["unarchetyped_files"] == 1
    # Blast radius is the worst single-file fan-out, not the sum.
    assert facts["blast_radius"] == 5
    # Every fan-out was readable, so nothing is unknown.
    assert facts["blast_radius_unknown"] == 0
    assert facts["active_block_findings"] == 1
    # An auth path in the changeset trips the security surface.
    assert facts["security_surface"] is True


def test_assemble_counts_unknown_fanout_instead_of_assuming_zero():
    changed = ["src/a.ts", "src/b.ts", "src/c.ts"]
    importers = {"src/a.ts": 2, "src/b.ts": None, "src/c.ts": 4}

    facts = assemble_facts(
        changed,
        added_lines=10,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: importers[p],
        block_findings_for=lambda p: 0,
    )

    assert facts["blast_radius_unknown"] == 1
    # The max is over the KNOWN values only; an unreadable fan-out never reads
    # as 0 (zero is the auto-pass direction, exactly the wrong default).
    assert facts["blast_radius"] == 4


def test_assemble_treats_importers_exception_as_unknown():
    def importers_of(p):
        if p == "src/b.ts":
            raise RuntimeError("index unreadable")
        return 1

    facts = assemble_facts(
        ["src/a.ts", "src/b.ts"],
        added_lines=1,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=importers_of,
        block_findings_for=lambda p: 0,
    )

    assert facts["blast_radius_unknown"] == 1
    assert facts["blast_radius"] == 1


def test_assemble_counts_source_files_excluding_tests():
    changed = [
        "src/a.ts",
        "src/a.test.ts",
        "spec/models/user_spec.rb",
        "app/models/listing.rb",
    ]

    facts = assemble_facts(
        changed,
        added_lines=4,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["source_files_changed"] == 2


def test_assemble_then_classify_routes_security_change_to_human():
    facts = assemble_facts(
        ["app/controllers/sessions_controller.rb"],
        added_lines=5,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert classify_change(facts)["auto_pass_eligible"] is False


def test_assemble_counts_type_error_files_among_changed():
    changed = ["src/a.ts", "src/b.ts", "src/c.ts"]

    facts = assemble_facts(
        changed,
        added_lines=10,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
        type_error_files={"src/b.ts", "src/other.ts"},
    )

    # Only changed files that also have a type error count; src/other.ts is not
    # in the changeset and must not inflate the signal.
    assert facts["type_errors"] == 1


def test_assemble_type_errors_default_zero_when_not_provided():
    facts = assemble_facts(
        ["src/a.ts"],
        added_lines=1,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["type_errors"] == 0


def test_assemble_empty_changeset_has_zeroed_signals():
    facts = assemble_facts(
        [],
        added_lines=0,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["files_changed"] == 0
    assert facts["blast_radius"] == 0
    assert facts["security_surface"] is False
