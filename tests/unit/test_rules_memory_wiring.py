"""The memory-channel import is wired in code, not by prose.

The conventions mirror only does anything if something imports it, and content
delivered through the memory channel measurably outranks the same content
injected by a hook. Both the mirror's own header and the init skill already name
`.claude/rules/chameleon-conventions.md` as the way to wire it -- but creating it
depended on the model offering the step and the user accepting, so the
highest-authority channel chameleon has was wired by prose and luck.

The property that keeps this safe is narrow and load-bearing: chameleon writes
only a file it authored. An existing file without its marker is left exactly
alone, mirroring how the statusline wiring declines to touch a statusLine the
user already configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chameleon_mcp.tools import (
    _RULES_IMPORT_BODY,
    _RULES_IMPORT_MARKER,
    _sync_rules_import,
)

RULES_REL = Path(".claude") / "rules" / "chameleon-conventions.md"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ALLOW_TMP_REPO", "1")
    monkeypatch.delenv("CHAMELEON_CONVENTIONS_MD", raising=False)
    (tmp_path / ".chameleon").mkdir()
    return tmp_path


def _with_mirror(repo: Path) -> Path:
    (repo / ".chameleon" / "conventions.md").write_text("PROJECT CONVENTIONS\n", encoding="utf-8")
    return repo / ".chameleon"


def test_creates_the_import_when_a_mirror_exists(repo):
    _sync_rules_import(_with_mirror(repo))
    assert (repo / RULES_REL).read_text(encoding="utf-8") == _RULES_IMPORT_BODY


def test_the_import_is_unconditioned(repo):
    """No `paths` frontmatter, deliberately.

    A path-scoped rule loads when Claude READS a matching file, not on every
    tool use, so it cannot carry guidance that has to arrive before a write. An
    unconditioned rule loads at launch at CLAUDE.md priority, which is the only
    reason this file is worth writing at all.
    """
    _sync_rules_import(_with_mirror(repo))
    body = (repo / RULES_REL).read_text(encoding="utf-8")
    assert "paths:" not in body
    assert not body.lstrip().startswith("---")


def test_rewrite_is_skipped_when_already_current(repo):
    profile = _with_mirror(repo)
    _sync_rules_import(profile)
    before = (repo / RULES_REL).stat().st_mtime_ns
    _sync_rules_import(profile)
    assert (repo / RULES_REL).stat().st_mtime_ns == before


def test_a_file_chameleon_did_not_author_is_never_touched(repo):
    """The whole safety property. Without the marker check this silently
    replaces a rule the team wrote by hand."""
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.write_text("# our own team rule\n", encoding="utf-8")
    _sync_rules_import(_with_mirror(repo))
    assert target.read_text(encoding="utf-8") == "# our own team rule\n"


def test_its_own_file_is_repaired_when_it_drifts(repo):
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.write_text(f"{_RULES_IMPORT_MARKER}\n@../../STALE.md\n", encoding="utf-8")
    _sync_rules_import(_with_mirror(repo))
    assert target.read_text(encoding="utf-8") == _RULES_IMPORT_BODY


def test_its_own_file_is_removed_when_the_mirror_goes(repo):
    """An empty render unlinks the mirror; an import pointing at a file that no
    longer exists is worse than no import."""
    profile = _with_mirror(repo)
    _sync_rules_import(profile)
    (profile / "conventions.md").unlink()
    _sync_rules_import(profile)
    assert not (repo / RULES_REL).exists()


def test_a_user_file_survives_the_mirror_going_away(repo):
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.write_text("# our own team rule\n", encoding="utf-8")
    _sync_rules_import(repo / ".chameleon")
    assert target.read_text(encoding="utf-8") == "# our own team rule\n"


def test_kill_switch_suppresses_the_write(repo, monkeypatch):
    """Same switch as the mirror it imports: one feature, one switch."""
    monkeypatch.setenv("CHAMELEON_CONVENTIONS_MD", "0")
    _sync_rules_import(_with_mirror(repo))
    assert not (repo / RULES_REL).exists()


def test_an_unreadable_existing_file_is_left_alone(repo):
    """Undecodable bytes are ambiguous, not obviously chameleon's. Replacing a
    file it cannot even read is the one move guaranteed to be wrong."""
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe\x00binary")
    _sync_rules_import(_with_mirror(repo))
    assert target.read_bytes() == b"\xff\xfe\x00binary"
