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


def test_assemble_excludes_non_source_files_from_unarchetyped_count():
    # A version-bump/release diff: manifests, lockfiles, docs, and plugin config
    # alongside two real source files. The engine has no archetype for any of the
    # non-source files, so is_unarchetyped is True for every path -- but only the
    # two source files may count. The rest are reviewed by the secret and
    # dependency passes, not the archetype/logic review, so counting them as
    # "outside profiled archetypes" spuriously elevates risk on a version bump.
    changed = [
        "package.json",
        "package-lock.json",
        "uv.lock",
        "pyproject.toml",
        "CHANGELOG.md",
        ".claude-plugin/marketplace.json",
        "plugin/.claude-plugin/plugin.json",
        "src/a.ts",
        "app/models/listing.rb",
    ]

    facts = assemble_facts(
        changed,
        added_lines=20,
        removed_lines=5,
        new_files=0,
        is_unarchetyped=lambda p: True,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["unarchetyped_files"] == 2


def test_assemble_excludes_non_source_files_from_source_count():
    changed = ["package.json", "uv.lock", "CHANGELOG.md", "src/a.ts"]

    facts = assemble_facts(
        changed,
        added_lines=4,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: True,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    # Only src/a.ts is source; the three non-source files never inflate the count.
    assert facts["source_files_changed"] == 1


def test_assemble_keeps_archetyped_config_as_source():
    # The skip-list is "unless the archetype specifically covers them": a config
    # file a repo HAS taught chameleon to archetype is real governed source, so a
    # false is_unarchetyped keeps it in both counts exactly as before.
    changed = ["config/settings.json"]

    facts = assemble_facts(
        changed,
        added_lines=3,
        removed_lines=0,
        new_files=0,
        is_unarchetyped=lambda p: False,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["unarchetyped_files"] == 0
    assert facts["source_files_changed"] == 1


def test_assemble_test_deletion_with_manifest_only_stays_eligible():
    # weakening_combo routes a change to a human only when a test-weakening signal
    # coincides with a LIVE-SOURCE change. A manifest / version bump is not live
    # source, so excluding it from source_files_changed keeps a "delete a test +
    # bump the version" diff eligible (test cleanup, not gutting a spec while
    # changing the code it covers).
    facts = assemble_facts(
        ["package.json", "CHANGELOG.md"],
        added_lines=3,
        removed_lines=1,
        new_files=0,
        is_unarchetyped=lambda p: True,
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
        deleted_test_files=1,
    )

    assert facts["source_files_changed"] == 0
    assert "test weakening" not in " ".join(classify_change(facts)["reasons"])

    # Contrast: the SAME test deletion alongside a real source file is the
    # dangerous combination and still routes to a human.
    with_source = assemble_facts(
        ["src/a.ts", "CHANGELOG.md"],
        added_lines=3,
        removed_lines=1,
        new_files=0,
        is_unarchetyped=lambda p: p != "src/a.ts",
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
        deleted_test_files=1,
    )

    assert with_source["source_files_changed"] == 1
    assert "test weakening" in " ".join(classify_change(with_source)["reasons"])


def test_assemble_source_only_diff_unchanged():
    # Regression guard: a diff with no non-source files behaves exactly as before
    # the exclusion existed -- every unarchetyped source file still counts.
    changed = ["src/a.ts", "src/b.ts", "app/models/listing.rb"]

    facts = assemble_facts(
        changed,
        added_lines=15,
        removed_lines=2,
        new_files=0,
        is_unarchetyped=lambda p: p != "src/a.ts",
        importers_of=lambda p: 0,
        block_findings_for=lambda p: 0,
    )

    assert facts["unarchetyped_files"] == 2
    assert facts["source_files_changed"] == 3
    assert facts["security_surface"] is False
