"""The chameleon-pr-review skill must assemble a PR-level test-coverage-delta view.

The file-by-file convention loop (Step 2) reviews each changed file in isolation
and cannot make the whole-diff judgment a human makes at a glance: "this PR
changed several source files and one test — are the untested ones intentional?"
Step 3g composes that aggregate view from the per-file archetype data the skill
already gathered, and emits it as a heads-up.

The view is advisory by construction. Chameleon has no source-to-test path map,
so it cannot name a specific missing test file, and ``git diff --name-only``
lists only changed files, so a pre-existing untouched test is invisible. The
grounded signals are (a) the file's archetype name (``test``/``test-*`` is a test
cluster, else source) and (b) the test-vs-source archetype path mirror in
``archetypes.json`` (a ``spec/services`` test archetype mirrors an ``app/services``
source archetype). The step must stay capped below NIT, never invent an
assertion-count delta, and never change the verdict. If those caps or the
grounding are lost in an edit the step regresses into the ungrounded coverage
finding the integrity rule forbids. The skill is an LLM-driven procedure, so the
test asserts on the procedure text the same way the migration-safety and
security-pass tests do.
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


def test_coverage_delta_step_present_and_advisory():
    text = _skill_text()
    assert "Step 3g: PR-level test-coverage-delta view (always, advisory only)" in text
    assert "It is ADVISORY only" in text
    assert "It never produces a BLOCK or a FIX, and it never forces a verdict" in text


def test_step_states_the_no_path_map_limit():
    """The advisory cap rests on chameleon having no source-to-test path map."""
    text = _skill_text()
    assert "chameleon has no source-to-test path map" in text
    # The diff lists only changed files, so an untouched pre-existing test is invisible.
    assert "the diff also lists only changed files" in text.lower()


def test_partition_uses_archetype_name():
    """Test vs source is grounded in the archetype name, the signal principles use."""
    text = _skill_text()
    assert "#### 3g-i. Partition the changed set into source vs test" in text
    assert "the archetype name is `test` or starts with `test-`" in text
    # A file with no reliable archetype is left out of both partitions.
    assert "`none` or `fallback`" in text
    # The headline is the source/test count pair.
    assert '"N source files changed, M test files changed."' in text


def test_test_paired_archetype_grounded_in_path_mirror():
    text = _skill_text()
    assert "#### 3g-ii. Decide which source archetypes are test-paired in this repo" in text
    assert ".chameleon/archetypes.json" in text
    # The pairing is the spec/source path mirror, not an invented file map.
    assert "a test archetype's `paths_pattern` mirrors it" in text
    assert "source `app/services` is paired with test `spec/services`" in text


def test_no_test_archetypes_skips_the_step():
    text = _skill_text()
    assert "If the repo has no test archetypes at all, skip this step entirely" in text


def test_untested_list_only_covers_test_paired_layers():
    text = _skill_text()
    assert "#### 3g-iii. Build the untested-source list" in text
    # Only source files in a test-paired layer are eligible; others are omitted.
    assert "keep only files whose archetype is test-paired" in text
    # A changed test in the mirroring archetype removes a source file from the list.
    assert "Drop a source file from the list when a changed test file in this same diff" in text


def test_coverage_delta_has_its_own_advisory_output_section():
    text = _skill_text()
    assert "### Coverage-delta (advisory)" in text
    assert "Never a BLOCK or FIX; it does not affect the verdict." in text
    # The output line must label itself a heads-up, not a verified gap.
    assert "heads-up, not a verified gap" in text


def test_verdict_rule_makes_coverage_delta_non_blocking():
    text = _skill_text()
    assert "The coverage-delta view (Step 3g) is advisory and carries no severity." in text
    assert "never changes the verdict" in text
    # An untested heads-up alone leaves a clean PR at APPROVE.
    assert "leaves an otherwise clean PR at APPROVE" in text


def test_integrity_rule_bans_named_missing_test_and_assertion_delta():
    text = _skill_text()
    assert "The coverage-delta view is advisory and grounded only in archetypes" in text
    # No claim of a specific missing test file.
    assert "must not claim a specific missing test file" in text
    # No assertion-count delta, which would be an ungrounded eyeball count.
    assert "never count an assertions delta" in text
