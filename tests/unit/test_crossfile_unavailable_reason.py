"""Reason-routing pins for the cross-file "could not look" contract.

query_symbol_importers / get_crossfile_context report WHY no cross-file index
loaded via a small reason vocabulary the using-chameleon skill routes on:
``unsupported-language`` (absence by design -- fall back to grep, never suggest
a repair) vs ``index-unavailable`` (the artifact should exist, so its absence
is damage /chameleon-refresh repairs). These tests pin that routing for
_crossfile_unavailable_reason and the Ruby constant-graph analogue so the
strings cannot silently drift from the skill's documented contract.
"""

from __future__ import annotations

import json


def _write_profile(repo, language):
    profile_dir = repo / ".chameleon"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.joinpath("profile.json").write_text(
        json.dumps({"schema_version": 1, "language": language}), encoding="utf-8"
    )
    return profile_dir


def test_damaged_index_on_reverse_indexed_language_reads_as_damage(tmp_path):
    # Bootstrap writes the reverse index for Python too, so its absence there
    # is repairable damage, never by-design (the old "typescript-only" answer
    # suppressed the repair suggestion on Python profiles).
    from chameleon_mcp.tools import _crossfile_unavailable_reason

    repo = tmp_path / "repo"
    _write_profile(repo, "python")
    assert _crossfile_unavailable_reason(repo) == "index-unavailable"


def test_ruby_profile_is_unsupported_for_the_reverse_index(tmp_path):
    # Ruby has no named-export surface, so no reverse index exists by design.
    from chameleon_mcp.tools import _crossfile_unavailable_reason

    repo = tmp_path / "repo"
    _write_profile(repo, "ruby")
    assert _crossfile_unavailable_reason(repo) == "unsupported-language"


def test_ruby_profile_is_damage_for_the_full_crossfile_surface(tmp_path):
    # get_crossfile_context passes the ruby-inclusive surface set: a Ruby
    # profile DOES write constant_index.json, so reaching the unavailable path
    # there means the backing artifact is missing/corrupt -- repairable damage.
    from chameleon_mcp.tools import _CROSSFILE_SURFACE_LANGUAGES, _crossfile_unavailable_reason

    repo = tmp_path / "repo"
    _write_profile(repo, "ruby")
    assert (
        _crossfile_unavailable_reason(repo, surfaces=_CROSSFILE_SURFACE_LANGUAGES)
        == "index-unavailable"
    )


def test_no_stored_language_falls_back_to_damage(tmp_path):
    # No stored language (unprofiled or corrupt manifest): never claim
    # by-design absence without evidence.
    from chameleon_mcp.tools import _crossfile_unavailable_reason

    repo = tmp_path / "repo"
    (repo / ".chameleon").mkdir(parents=True, exist_ok=True)
    assert _crossfile_unavailable_reason(repo) == "index-unavailable"


def test_stray_rb_file_in_ts_profile_reads_unsupported(tmp_path):
    # A .rb file dispatches to the constant graph by extension, but only a
    # Ruby profile ever writes constant_index.json -- suggesting a refresh in
    # a TS-profiled repo is a dead-end loop, so the absence is by design.
    from chameleon_mcp.tools import _ruby_constant_importers

    repo = tmp_path / "repo"
    _write_profile(repo, "typescript")
    rb = repo / "script.rb"
    rb.write_text("class Foo; end\n", encoding="utf-8")

    out = _ruby_constant_importers(repo, rb)
    assert out["found"] is False
    assert out["reason"] == "unsupported-language"


def test_ruby_profile_missing_constant_index_reads_as_damage(tmp_path):
    from chameleon_mcp.tools import _ruby_constant_importers

    repo = tmp_path / "repo"
    _write_profile(repo, "ruby")
    rb = repo / "app" / "models" / "foo.rb"
    rb.parent.mkdir(parents=True, exist_ok=True)
    rb.write_text("class Foo; end\n", encoding="utf-8")

    out = _ruby_constant_importers(repo, rb)
    assert out["found"] is False
    assert out["reason"].startswith("index-unavailable")
