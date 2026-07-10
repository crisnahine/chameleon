"""The chameleon-pr-review skill must run a migration-safety pass on Rails migrations.

A file under ``db/migrate/`` clusters as just another archetype matched on
top-level shape, so a dangerous migration looks structurally identical to its
safe siblings and passes the convention review clean. The migration-safety pass
(Step 2.7) reads the migration DSL inside the change directly and surfaces three
things at three confidence levels:

- 2.7a flags an irreversible operation inside a ``def change`` block as BLOCK.
  An irreversible op with no ``up``/``down`` pair is a witnessed structural fact
  in the diff (the rollback will raise), so it is the one BLOCK this pass earns.
- 2.7b is an advisory ``null: false``-without-``default`` reminder capped at FIX.
- 2.7c is an advisory ``add_index``-without-``concurrently`` reminder capped at
  FIX.

The two advisories are NOT findings about the migration being wrong: the
dangerous predicate (a populated or large table) is a row count this static read
cannot see, and the repo's own clean migrations share the same shapes. They must
stay capped at FIX and labeled "verify table size", never escalated to a
confident defect. If any of these instructions or the severity caps are lost in
an edit the skill regresses to either no migration signal or an over-confident
one. The skill is an LLM-driven procedure, so the test asserts on the procedure
text the same way the dependency-review and security-pass tests do.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "plugin" / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    """Body plus lazily-loaded references — the skill's full procedure text."""
    refs = sorted(SKILL.parent.glob("references/*.md"))
    parts = [SKILL.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in refs]
    return "\n".join(parts)


def test_migration_pass_step_present_and_scoped_to_db_migrate():
    text = _skill_text()
    assert "Step 2.7: Migration-safety pass" in text
    # Scoped to Rails migrations, not every Ruby file.
    assert "every changed file whose path is under `db/migrate/`" in text


def test_migration_files_are_carved_out_of_the_generic_skip():
    """db/migrate files must reach the migration pass; only schema.rb stays skipped."""
    text = _skill_text()
    assert "Rails migrations (`db/migrate/*.rb`) get one extra pass" in text
    # schema.rb is generated and stays fully skipped.
    assert "Only `schema.rb` stays fully skipped" in text


def test_pass_reads_dsl_kwargs_at_deeper_indent_no_network():
    """The DSL sits inside change/up/down deeper than the archetype shape, kwargs matter."""
    text = _skill_text()
    assert "makes NO network calls" in text
    for kwarg in ("`null:`", "`default:`", "`algorithm:`"):
        assert kwarg in text, f"migration pass omits the {kwarg} keyword"
    # The call can wrap onto a second line.
    assert "including a call that wraps onto a second line" in text


def test_irreversible_change_block_is_block():
    text = _skill_text()
    assert "Irreversible `change` block (BLOCK)" in text
    assert (
        "Raise a **BLOCK** when a `change` method contains an operation Rails cannot auto-reverse"
        in text
    )
    # A def up / def down pair (or reversible) is the correct fix, not a flag.
    assert "does NOT instead define a `def up` / `def down` pair" in text
    assert "`reversible do |dir|`" in text


def test_irreversible_is_the_only_witnessed_block_in_the_pass():
    text = _skill_text()
    # It is the witnessed structural fact, distinguished from the table-size guesses.
    assert "the one clean static win in this pass" in text
    assert "a witnessed structural fact in the diff, not a guess about table size" in text


def test_null_false_is_advisory_fix_verify_table_size():
    text = _skill_text()
    assert "`null:false` added without a default (advisory FIX — verify table size)" in text
    assert "advisory, verify table size" in text
    # change_column_null with no backfill is the same shape.
    assert "`change_column_null ..., false`" in text
    # The static read cannot see the row count, and safe siblings share the shape.
    assert "this static read cannot see the row count" in text


def test_add_index_concurrently_is_advisory_fix_verify_table_size():
    text = _skill_text()
    assert (
        "`add_index` without `algorithm: :concurrently` (advisory FIX — verify table size)" in text
    )
    assert "`algorithm: :concurrently`" in text
    # disable_ddl_transaction! is part of the suggested fix for the concurrent build.
    assert "`disable_ddl_transaction!`" in text


def test_advisories_never_reach_block():
    text = _skill_text()
    # Both 2.7b and 2.7c are explicitly capped below BLOCK.
    assert "Never let 2.7b or 2.7c reach BLOCK" in text
    assert "the only BLOCK this pass can raise" in text


def test_migration_findings_have_their_own_output_section():
    text = _skill_text()
    assert "### Migration-safety findings" in text


def test_severity_table_caps_migration_advisories_at_fix():
    text = _skill_text()
    assert "The migration null:false and add_index advisories are capped at FIX" in text
    assert "only the irreversible-`change` check blocks from the migration-safety pass" in text


def test_verdict_rule_makes_irreversible_drive_block():
    text = _skill_text()
    assert (
        "an irreversible op in a `change` block (Step 2.7a) is a BLOCK and drives a BLOCK verdict"
        in text
    )
    # The migration table-size advisories never force a BLOCK verdict on their own.
    assert "the migration table-size advisories (Step 2.7b/2.7c) are capped below BLOCK" in text
    assert "never force a BLOCK verdict on their own" in text


def test_integrity_rule_keeps_migration_tiers_honest():
    text = _skill_text()
    assert "Migration findings carry their own honesty bar" in text
    # The advisories are reminders, not confirmed defects.
    assert "not confirmed defects" in text
    assert "Do not present either reminder as if it were a confirmed migration bug" in text
