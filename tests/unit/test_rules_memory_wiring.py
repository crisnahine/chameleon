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


# --------------------------------------------------------------------------
# The lifecycle seam. Every test above calls the writer directly, which is
# precisely why a bootstrap that never called it went unnoticed.
# --------------------------------------------------------------------------


def test_sync_conventions_md_writes_both_halves(repo, monkeypatch):
    """The documented contract: the mirror and its import are written together.

    _sync_conventions_md is the teach/refresh entry point. If it ever writes one
    without the other, a repo ends up with a mirror nothing imports, or an
    import pointing at a file that is gone.
    """
    from chameleon_mcp.tools import _sync_conventions_md

    # A preferred-import row is the cheapest shape that renders a non-empty
    # mirror; an empty render would unlink instead and prove nothing here.
    conv = {
        "generation": 1,
        "conventions": {
            "imports": {
                "svc": {
                    "preferred": [
                        {
                            "module": "~/lib/http",
                            "source": "~/lib/http",
                            "frequency": 9,
                            "total": 10,
                        }
                    ]
                }
            }
        },
    }
    _sync_conventions_md(repo / ".chameleon", conv)

    assert (repo / ".chameleon" / "conventions.md").is_file(), "precondition: mirror rendered"
    assert (repo / RULES_REL).read_text(encoding="utf-8") == _RULES_IMPORT_BODY


def test_sync_conventions_md_removes_both_when_nothing_renders(repo):
    """An empty render unlinks the mirror; the import must go with it."""
    from chameleon_mcp.tools import _sync_conventions_md

    _with_mirror(repo)
    _sync_rules_import(repo / ".chameleon")
    assert (repo / RULES_REL).is_file()  # precondition

    _sync_conventions_md(repo / ".chameleon", {"generation": 1, "conventions": {}})
    assert not (repo / ".chameleon" / "conventions.md").exists()
    assert not (repo / RULES_REL).exists()


def test_a_marked_file_with_extra_user_content_is_still_rewritten(repo):
    """Documented, not accidental: the body is regenerated wholesale, so an
    appended line does not survive. The file says so in its own second comment,
    matching the mirror it imports."""
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.write_text(_RULES_IMPORT_BODY + "@../../extra-notes.md\n", encoding="utf-8")
    _sync_rules_import(_with_mirror(repo))
    assert (repo / RULES_REL).read_text(encoding="utf-8") == _RULES_IMPORT_BODY
    assert "Edits here are lost" in _RULES_IMPORT_BODY


def test_a_symlink_at_the_rules_path_is_replaced_not_followed(repo, tmp_path):
    """Replacing a symlink must not write through it. The outside file has to
    survive byte-identical -- a committed symlink is a supply-chain shape."""
    outside = tmp_path / "outside.md"
    outside.write_text("do not touch\n", encoding="utf-8")
    target = repo / RULES_REL
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    _sync_rules_import(_with_mirror(repo))
    assert outside.read_text(encoding="utf-8") == "do not touch\n"


def test_a_directory_at_the_rules_path_is_left_alone(repo):
    """Fail open rather than try to clear a directory out of the way."""
    target = repo / RULES_REL
    target.mkdir(parents=True)
    _sync_rules_import(_with_mirror(repo))
    assert target.is_dir()
