"""The chameleon-pr-review skill must review manifest and lockfile diffs.

Chameleon only models intra-repo conventions, so a supply-chain change (a new
dependency, a tampered lockfile entry resolved from a non-registry host, a
malicious install script, a git+ssh:/file: source) is a class the convention
review is structurally blind to. The skill's file-skip rules drop
``*.lock``/``.json``/``.yml`` wholesale, which would silently exclude
``package.json``/``package-lock.json``/``Gemfile``/``Gemfile.lock`` from any
review at all. The dependency-change step (Step 2.5) carves those files out of
the skip and runs four independent, no-network diff-parse checks against them.
If any of these instructions is lost in an edit the skill regresses to passing
typosquats and tampered hashes with zero signal, so these tests pin the
load-bearing instructions in place. The skill is an LLM-driven procedure, so the
test asserts on the procedure text the same way the hunk-aware tests do.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL = REPO_ROOT / "skills" / "chameleon-pr-review" / "SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_manifests_and_lockfiles_are_not_skipped():
    """The manifest/lockfile carve-out must override the generic skip rules."""
    text = _skill_text()
    assert "Do NOT skip the package manifests and lockfiles" in text
    # The exact files that must reach the dependency review despite matching the
    # `.json`/`*.lock`/`.yml` skip globs.
    for needle in (
        "package.json",
        "package-lock.json",
        "Gemfile",
        "Gemfile.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
    ):
        assert needle in text, f"dependency review omits {needle!r}"


def test_closing_skip_rule_no_longer_drops_lockfiles_outright():
    """The 'Important' skip line must scope the lockfile skip to archetype review."""
    text = _skill_text()
    skip_line = next(
        line for line in text.splitlines() if line.startswith("- Skip auto-generated files:")
    )
    # The auto-generated list itself must not carry a bare *.lock entry before
    # the qualifying clause: lockfiles are skipped for archetype review only.
    before_clause = skip_line.split("Lockfiles")[0]
    assert "`*.lock`" not in before_clause
    assert "NOT for the dependency-change review" in skip_line


def test_dependency_change_step_present_and_no_network():
    text = _skill_text()
    assert "Step 2.5: Dependency-change review" in text
    # The whole pass is a diff parse: no network, no install, no audit shell-out.
    assert "NO network calls" in text
    assert "Do not run a security audit, hit a network, or install packages" in text


def test_new_direct_dependency_is_block_until_acknowledged():
    text = _skill_text()
    assert "New direct dependency" in text
    assert "verify provenance" in text
    # It is a deliberate human gate, cleared only by a human acknowledgement.
    assert "BLOCK until acknowledged" in text
    assert "human acknowledges" in text
    # A bump of an existing dependency is explicitly not this finding.
    assert "A bump of an already-present dependency is NOT this finding" in text
    # Typosquatting is the concrete risk the provenance gate guards against.
    assert "typosquat" in text


def test_non_registry_resolved_host_check():
    text = _skill_text()
    assert "registry.npmjs.org" in text
    assert "rubygems.org" in text
    # The repo's own consistent private registry must not be flagged.
    assert "do not flag added entries that use it" in text


def test_install_lifecycle_script_check():
    text = _skill_text()
    for needle in (
        "scripts.preinstall",
        "scripts.install",
        "scripts.postinstall",
    ):
        assert needle in text, f"install-script check omits {needle!r}"
    assert "runs automatically on `npm install`" in text


def test_non_registry_source_check():
    text = _skill_text()
    # The non-registry source shapes that bypass the registry publish path.
    for needle in ("git+ssh:", "file:", "github:"):
        assert needle in text, f"non-registry source check omits {needle!r}"


def test_dependency_findings_have_their_own_output_section():
    text = _skill_text()
    assert "Dependency / supply-chain findings" in text
    # Findings cite the exact lockfile line / manifest key, not the profile.
    assert "cites the exact lockfile line or manifest key" in text


def test_integrity_rule_exempts_dependency_findings_from_chameleon_data():
    """Dependency findings are backed by the diff, not the profile."""
    text = _skill_text()
    assert "Dependency findings (Step 2.5) are the one exception" in text
