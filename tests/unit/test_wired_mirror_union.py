"""A multi-import rules file delivers every mirror, so detection must read all.

``_wired_mirror_text`` returned the FIRST resolved import target and stopped.
That was correct while chameleon only ever wrote one ``@`` line, and becomes a
silent under-report the moment a monorepo's rules file names several mirrors:
the memory channel delivers all of them, detection sees one, and the two
consumers draw the wrong conclusion in opposite directions.

``_dedupe_conventions_block`` drops a SessionStart line only when the mirror
already carries it, so under-reporting there is merely lost savings -- the
block re-injects, which is safe. ``_record_mirror_idiom_slugs`` is the harmful
one: it feeds ``SessionDoc.idioms_shown_slugs`` off the same text, so an idiom
the mirror really did deliver would be re-shown at Stop as though the model had
never seen it.

Union the resolved targets instead of stopping at the first.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.hook_helper import _WIRED_MIRROR_CACHE, _wired_mirror_text


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    _WIRED_MIRROR_CACHE.clear()
    yield tmp_path
    _WIRED_MIRROR_CACHE.clear()


def _mirror(root, rel, body):
    profile = root / rel / ".chameleon" if rel else root / ".chameleon"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "conventions.md").write_text(body, encoding="utf-8")


def _rules(root, targets):
    path = root / ".claude" / "rules" / "chameleon-conventions.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"@{t}" for t in targets) + "\n", encoding="utf-8")


def test_every_imported_mirror_contributes_its_text(repo):
    _mirror(repo, "", "ROOT CONVENTION LINE\n")
    _mirror(repo, "packages/api", "API CONVENTION LINE\n")
    _mirror(repo, "packages/web", "WEB CONVENTION LINE\n")
    _rules(
        repo,
        [
            "../../.chameleon/conventions.md",
            "../../packages/api/.chameleon/conventions.md",
            "../../packages/web/.chameleon/conventions.md",
        ],
    )
    delivered = _wired_mirror_text(repo)
    assert "ROOT CONVENTION LINE" in delivered
    assert "API CONVENTION LINE" in delivered
    assert "WEB CONVENTION LINE" in delivered


def test_a_single_import_still_returns_that_mirror(repo):
    _mirror(repo, "", "ONLY CONVENTION LINE\n")
    _rules(repo, ["../../.chameleon/conventions.md"])
    assert "ONLY CONVENTION LINE" in _wired_mirror_text(repo)


def test_an_unresolvable_target_does_not_drop_its_siblings(repo):
    # A linked worktree can hold the rules file without materializing every
    # mirror it names; the ones that DO resolve still count as delivered.
    _mirror(repo, "packages/api", "API CONVENTION LINE\n")
    _rules(
        repo,
        [
            "../../packages/gone/.chameleon/conventions.md",
            "../../packages/api/.chameleon/conventions.md",
        ],
    )
    assert "API CONVENTION LINE" in _wired_mirror_text(repo)


def test_no_wiring_still_reads_as_undelivered(repo):
    _mirror(repo, "", "ROOT CONVENTION LINE\n")
    assert _wired_mirror_text(repo) == ""


def test_the_union_spans_candidate_files_not_just_one(repo):
    """Regression: the scan stopped at the first FILE that resolved anything.

    A monorepo root commonly carries both the CLAUDE.md import /chameleon-init
    offers and the rules file naming every workspace mirror. Candidates are
    scanned CLAUDE.md first, so breaking there returned only the root mirror --
    under-reporting exactly what the union was added to fix.
    """
    _mirror(repo, "", "ROOT CONVENTION LINE\n")
    _mirror(repo, "packages/api", "API CONVENTION LINE\n")
    (repo / "CLAUDE.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
    _rules(repo, ["../../packages/api/.chameleon/conventions.md"])
    delivered = _wired_mirror_text(repo)
    assert "ROOT CONVENTION LINE" in delivered
    assert "API CONVENTION LINE" in delivered


def test_the_same_mirror_reached_twice_is_not_duplicated(repo):
    # CLAUDE.md and the rules file can name the SAME mirror; dedupe is by
    # resolved target across every candidate, not per file.
    _mirror(repo, "", "ROOT CONVENTION LINE\n")
    (repo / "CLAUDE.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
    _rules(repo, ["../../.chameleon/conventions.md"])
    assert _wired_mirror_text(repo).count("ROOT CONVENTION LINE") == 1
