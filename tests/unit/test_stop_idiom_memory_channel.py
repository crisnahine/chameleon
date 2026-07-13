"""Memory-channel-aware Stop idiom review (v3.1.0).

When a repo imports `.chameleon/conventions.md` into the Claude memory channel
(CLAUDE.md / CLAUDE.local.md / .claude/rules) and the DELIVERED mirror carries
an idiom's gist, the Stop self-review renders that idiom as a one-line gist
instead of a full-text dump: its directive is already ambient in every session
through the higher-authority channel (migration A/B 2026-07-11). Delivery is
verified the way Claude Code resolves imports — code fences and inline code
spans are ignored, the path resolves relative to the containing file, and the
target must exist — so a prose mention or a worktree with an unmaterialized
target never counts. Idioms with no channel keep the v3.0.3 full-text
escalation, and CHAMELEON_STOP_IDIOM_GIST=0 restores it wholesale.
"""

from __future__ import annotations

from pathlib import Path

import chameleon_mcp.hook_helper as hh
from chameleon_mcp.hook_helper import _mirror_idiom_names, _wired_mirror_text
from chameleon_mcp.tools import _render_stop_idioms, parse_idiom_gist_names

_IDIOMS_MD = (
    "# idioms\n\n## active\n\n"
    "### wrap-fetches\n"
    "Always wrap fetches in the apiClient helper.\n\n"
    "Example:\n```\napiClient.get('/x')\n```\n\n"
    "### atomic-writes\n"
    "Write profile artifacts inside atomic_profile_commit only.\n"
)

_MIRROR_MD = (
    "PROJECT CONVENTIONS — authoritative.\n\n"
    "SHAPE (advisory):\n"
    "- unit-py: nesting median 0, p90 1\n\n"
    "TEAM IDIOMS (taught; follow on every edit — full text with examples in "
    ".chameleon/idioms.md):\n"
    "- wrap-fetches: Always wrap fetches in the apiClient helper.\n"
    "- (+2 more; see .chameleon/idioms.md)\n\n"
    "PRINCIPLES:\n"
    "- keep-it: small functions.\n"
)


def _clear_cache():
    hh._WIRED_MIRROR_CACHE.clear()


def _render(mirror_names=None, seen=None):
    return _render_stop_idioms(
        _IDIOMS_MD,
        [],
        seen or [],
        char_cap=3000,
        max_terse=25,
        summary_max_chars=160,
        edited_languages=None,
        mirror_idiom_names=mirror_names,
    )


class TestRenderWithMirrorNames:
    def test_mirrored_idiom_renders_as_gist_with_pointer(self):
        out = _render(mirror_names={"wrap-fetches"})
        assert "- wrap-fetches: Always wrap fetches in the apiClient helper." in out
        assert "apiClient.get" not in out  # full block did not re-dump
        assert "Full text for any you have not applied: .chameleon/idioms.md" in out
        # the unmirrored idiom keeps full-text escalation
        assert "### atomic-writes" in out

    def test_no_mirror_names_keeps_v303_escalation(self):
        out = _render(mirror_names=None)
        assert "### wrap-fetches" in out
        assert "apiClient.get" in out
        assert "Full text for any you have not applied" not in out

    def test_session_seen_idiom_needs_no_pointer(self):
        out = _render(mirror_names=None, seen=["wrap-fetches", "atomic-writes"])
        assert "- wrap-fetches:" in out
        assert "Full text for any you have not applied" not in out

    def test_mirror_name_not_in_idioms_md_changes_nothing(self):
        out = _render(mirror_names={"no-such-idiom"})
        assert "### wrap-fetches" in out
        assert "### atomic-writes" in out


class TestParseIdiomGistNames:
    def test_names_parsed_from_team_idioms_section_only(self):
        names = parse_idiom_gist_names(_MIRROR_MD)
        assert names == {"wrap-fetches"}
        # SHAPE / PRINCIPLES colon lines and the "+N more" tail never leak in
        assert "unit-py" not in names
        assert "keep-it" not in names

    def test_no_section_returns_empty(self):
        assert parse_idiom_gist_names("IMPORTS:\n- Prefer pathlib\n") == set()

    def test_round_trips_through_the_real_renderer(self):
        # Producer/consumer grammar coupling: what render_conventions_md emits,
        # parse_idiom_gist_names must read back — this test is the contract.
        from chameleon_mcp.conventions import render_conventions_md

        mirror = render_conventions_md({"conventions": {}}, None, _IDIOMS_MD)
        assert parse_idiom_gist_names(mirror) == {"wrap-fetches", "atomic-writes"}


class TestWiredMirrorText:
    def _repo(self, tmp_path) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        _clear_cache()
        return repo, profile

    def test_claude_local_md_import_delivers_mirror(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert "TEAM IDIOMS" in _wired_mirror_text(repo)

    def test_rules_dir_relative_import_resolves_against_containing_file(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        rules = repo / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "chameleon-conventions.md").write_text(
            "@../../.chameleon/conventions.md\n", encoding="utf-8"
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert "TEAM IDIOMS" in _wired_mirror_text(repo)

    def test_import_with_missing_target_is_not_wired(self, tmp_path):
        # The linked-worktree case: the import line exists but the target file
        # is not materialized where the import resolves -> nothing delivered.
        repo, _profile = self._repo(tmp_path)
        (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_inline_code_span_mention_is_not_wired(self, tmp_path):
        # Claude Code does not evaluate imports inside code spans; docs that
        # QUOTE the sigil (this repo's own rules file does) must not count.
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "Add an `@.chameleon/conventions.md` line to wire the mirror.\n",
            encoding="utf-8",
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_fenced_block_mention_is_not_wired(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "Example wiring:\n\n```markdown\n@.chameleon/conventions.md\n```\n",
            encoding="utf-8",
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_prose_path_mention_without_sigil_is_not_wired(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "See .chameleon/conventions.md for the rendered conventions.\n",
            encoding="utf-8",
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_absent_memory_files_not_wired(self, tmp_path):
        repo, _profile = self._repo(tmp_path)
        assert _wired_mirror_text(repo) == ""

    def test_nonexistent_root_fails_closed(self, tmp_path):
        _clear_cache()
        assert _wired_mirror_text(tmp_path / "missing") == ""

    def test_result_is_memoized_per_process(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        first = _wired_mirror_text(repo)
        # a post-read mutation is not observed within the same hook process
        (profile / "conventions.md").write_text("changed", encoding="utf-8")
        assert _wired_mirror_text(repo) == first


class TestMirrorIdiomNames:
    def _wired(self, tmp_path) -> Path:
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        _clear_cache()
        return repo

    def test_names_from_delivered_mirror(self, tmp_path):
        repo = self._wired(tmp_path)
        assert _mirror_idiom_names(repo) == {"wrap-fetches"}

    def test_unwired_repo_returns_empty(self, tmp_path):
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        _clear_cache()
        assert _mirror_idiom_names(repo) == set()

    def test_pre_idioms_format_mirror_returns_empty(self, tmp_path):
        repo = self._wired(tmp_path)
        (repo / ".chameleon" / "conventions.md").write_text(
            "PROJECT CONVENTIONS — authoritative.\n\nIMPORTS:\n- Prefer pathlib\n",
            encoding="utf-8",
        )
        _clear_cache()
        assert _mirror_idiom_names(repo) == set()

    def test_kill_switch_returns_empty(self, tmp_path, monkeypatch):
        repo = self._wired(tmp_path)
        monkeypatch.setenv("CHAMELEON_STOP_IDIOM_GIST", "0")
        assert _mirror_idiom_names(repo) == set()
