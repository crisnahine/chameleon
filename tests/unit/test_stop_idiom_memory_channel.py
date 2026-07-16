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
from chameleon_mcp.hook_helper import (
    _record_mirror_idiom_slugs,
    _wired_mirror_text,
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

    def test_indented_fence_mention_is_not_wired(self, tmp_path):
        # The init skill's own docs quote the import inside a 3-space-indented
        # list fence; CommonMark treats that as a fence, so must the scan.
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "1. Wire it:\n\n   ```\n   @.chameleon/conventions.md\n   ```\n",
            encoding="utf-8",
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_tilde_fence_mention_is_not_wired(self, tmp_path):
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text("~~~\n@.chameleon/conventions.md\n~~~\n", encoding="utf-8")
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        assert _wired_mirror_text(repo) == ""

    def test_unclosed_fence_blanks_to_eof(self, tmp_path):
        # Markdown treats an unclosed fence as code to EOF; a quoted import
        # after one must not read as wiring.
        repo, profile = self._repo(tmp_path)
        (repo / "CLAUDE.md").write_text(
            "Example:\n\n```\nsome code\n\n@.chameleon/conventions.md\n",
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


class TestRecordMirrorIdiomSlugs:
    """SessionStart-side recording onto SessionDoc.idioms_shown_slugs."""

    def _wired_repo(self, tmp_path, monkeypatch) -> tuple[Path, Path]:
        from chameleon_mcp.core.idiom_store import IdiomRecord, upsert_idiom

        monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path / "data"))
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (repo / "CLAUDE.local.md").write_text("@.chameleon/conventions.md\n", encoding="utf-8")
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        # The recording resolves each delivered gist NAME to its store slug, so
        # a title with no matching record is dropped; seed the one the mirror
        # actually carries (_MIRROR_MD's only real gist line, "wrap-fetches").
        upsert_idiom(
            profile,
            IdiomRecord(
                slug="wrap-fetches",
                title="wrap-fetches",
                rationale="Always wrap fetches in the apiClient helper.",
                status="active",
                added_date="2026-07-15",
                rank=1,
            ),
        )
        _clear_cache()
        return repo, tmp_path / "data"

    def _shown(self, repo: Path, sid: str) -> set[str]:
        from chameleon_mcp.core.session_state import read_session_doc
        from chameleon_mcp.tools import _compute_repo_id

        return read_session_doc(_compute_repo_id(repo), sid).idioms_shown_slugs

    def test_wired_repo_records_delivered_slugs(self, tmp_path, monkeypatch):
        repo, _data_dir = self._wired_repo(tmp_path, monkeypatch)
        _record_mirror_idiom_slugs(repo, "sess-1")
        assert self._shown(repo, "sess-1") == {"wrap-fetches"}

    def test_unwired_repo_records_nothing(self, tmp_path, monkeypatch):
        repo, data_dir = self._wired_repo(tmp_path, monkeypatch)
        (repo / "CLAUDE.local.md").unlink()
        _clear_cache()
        _record_mirror_idiom_slugs(repo, "sess-2")
        assert self._shown(repo, "sess-2") == set()
        assert not list(data_dir.rglob(".session_doc.*")) if data_dir.exists() else True

    def test_null_session_records_nothing(self, tmp_path, monkeypatch):
        repo, data_dir = self._wired_repo(tmp_path, monkeypatch)
        _record_mirror_idiom_slugs(repo, None)
        assert not list(data_dir.rglob(".session_doc.*")) if data_dir.exists() else True

    def test_mid_session_teach_is_not_recorded(self, tmp_path, monkeypatch):
        # The live mirror gains an idiom after session start; the recorded
        # shown set stays pinned to what was delivered at session start (the
        # @import resolved once), so the new idiom is absent from it.
        repo, _data_dir = self._wired_repo(tmp_path, monkeypatch)
        _record_mirror_idiom_slugs(repo, "sess-3")
        (repo / ".chameleon" / "conventions.md").write_text(
            _MIRROR_MD.replace(
                "- wrap-fetches: Always wrap fetches in the apiClient helper.",
                "- wrap-fetches: Always wrap fetches in the apiClient helper.\n"
                "- taught-later: A rule taught mid-session.",
            ),
            encoding="utf-8",
        )
        assert self._shown(repo, "sess-3") == {"wrap-fetches"}


def _is_wired(tmp_path, claude_md: str) -> bool:
    """Build a repo whose CLAUDE.md is claude_md and ask if the mirror wires."""
    repo = tmp_path / "repo"
    profile = repo / ".chameleon"
    profile.mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
    _clear_cache()
    return "TEAM IDIOMS" in _wired_mirror_text(repo)


_IMP = "@.chameleon/conventions.md"


class TestBlankCodeRegionsFenceRules:
    def test_mixed_fence_nesting_stays_blanked(self, tmp_path):
        # CommonMark: a ``` inside an open ~~~ block is literal content, not a
        # closer — the quoted import after it must stay blanked.
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (repo / "CLAUDE.md").write_text(
            "~~~\n```\n@.chameleon/conventions.md\n~~~\n", encoding="utf-8"
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        hh._WIRED_MIRROR_CACHE.clear()
        assert _wired_mirror_text(repo) == ""

    def test_info_string_line_is_not_a_closer(self, tmp_path):
        # ```python inside an open ``` block has an info string and cannot
        # close it; the import stays code to EOF.
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (repo / "CLAUDE.md").write_text(
            "```\n```python\n@.chameleon/conventions.md\n", encoding="utf-8"
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        hh._WIRED_MIRROR_CACHE.clear()
        assert _wired_mirror_text(repo) == ""

    def test_import_after_properly_closed_fence_is_live(self, tmp_path):
        repo = tmp_path / "repo"
        profile = repo / ".chameleon"
        profile.mkdir(parents=True)
        (repo / "CLAUDE.md").write_text(
            "```\nexample\n```\n\n@.chameleon/conventions.md\n", encoding="utf-8"
        )
        (profile / "conventions.md").write_text(_MIRROR_MD, encoding="utf-8")
        hh._WIRED_MIRROR_CACHE.clear()
        assert "TEAM IDIOMS" in _wired_mirror_text(repo)


class TestFenceCloserRule:
    """A closing fence is ONE run of the opener's char followed only by
    whitespace; a content line carrying extra fence chars never closes."""

    def test_fence_char_pair_content_line_is_not_a_closer(self, tmp_path):
        assert not _is_wired(tmp_path, f"```\n``` ```\n{_IMP}\n```\nprose\n")

    def test_single_fence_char_after_run_is_not_a_closer(self, tmp_path):
        assert not _is_wired(tmp_path, f"```\n``` `\n{_IMP}\n```\nprose\n")

    def test_tilde_pair_content_line_is_not_a_closer(self, tmp_path):
        assert not _is_wired(tmp_path, f"~~~\n~~~ ~~~\n{_IMP}\n~~~\nprose\n")

    def test_closer_with_trailing_whitespace_still_closes(self, tmp_path):
        assert _is_wired(tmp_path, f"```\ncode\n```   \n\n{_IMP}\n")

    def test_longer_same_char_run_still_closes(self, tmp_path):
        assert _is_wired(tmp_path, f"```\ncode\n`````\n{_IMP}\n")

    def test_shorter_run_does_not_close_longer_opener(self, tmp_path):
        assert not _is_wired(tmp_path, f"````\n```\n{_IMP}\n````\nprose\n")

    def test_tilde_info_string_line_is_not_a_closer(self, tmp_path):
        assert not _is_wired(tmp_path, f"~~~\n~~~ruby\n{_IMP}\n~~~\nprose\n")

    def test_indented_opener_unindented_closer_still_closes(self, tmp_path):
        assert _is_wired(tmp_path, f"   ```\n   code\n```\n\n{_IMP}\n")

    def test_unicode_line_separator_does_not_fabricate_closer(self, tmp_path):
        # U+2028 is not a markdown line ending; the run after it is fence
        # content, not a closer, so the fence stays open to EOF.
        assert not _is_wired(tmp_path, f"```\ncode\u2028```\n{_IMP}\n")

    def test_form_feed_does_not_fabricate_closer(self, tmp_path):
        assert not _is_wired(tmp_path, f"```\ncode\x0c```\n{_IMP}\n")

    def test_lone_carriage_return_is_a_line_ending(self, tmp_path):
        assert _is_wired(tmp_path, f"```\rcode\r```\r\r{_IMP}\r")


class TestIndentedCodeBlocks:
    """A line indented >=4 columns (tab = 4) is indented code, never wiring;
    0-3 columns of indent is plain prose Claude Code delivers."""

    def test_four_space_indented_import_after_blank_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"Example wiring:\n\n    {_IMP}\n\nMore prose.\n")

    def test_tab_indented_import_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"\t{_IMP}\n")

    def test_two_space_indented_import_stays_wired(self, tmp_path):
        assert _is_wired(tmp_path, f"  {_IMP}\n")

    def test_three_space_indented_import_stays_wired(self, tmp_path):
        assert _is_wired(tmp_path, f"   {_IMP}\n")

    def test_import_after_indented_block_ends_stays_wired(self, tmp_path):
        assert _is_wired(tmp_path, f"    code sample\n\n{_IMP}\n")

    def test_blockquoted_indented_import_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"> Add this to CLAUDE.md:\n>\n>     {_IMP}\n")

    def test_nested_blockquote_indented_import_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"> > note\n> >\n> >     {_IMP}\n")

    def test_list_item_indented_code_import_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"-      {_IMP}\n")


class TestContainerFences:
    """Fence markers behind blockquote / list-item prefixes open real fences;
    the prefixed contents are code, not wiring."""

    def test_blockquoted_fence_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"> ```\n> {_IMP}\n> ```\n")

    def test_list_item_fence_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"- ```\n  {_IMP}\n  ```\n")

    def test_ordered_list_fence_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"1. ```\n   {_IMP}\n   ```\n")

    def test_unclosed_blockquoted_fence_blanks_its_lines(self, tmp_path):
        assert not _is_wired(tmp_path, f"> ```\n> {_IMP}\n")

    def test_plain_blockquoted_import_stays_wired(self, tmp_path):
        # A bare blockquoted import with no fence counts as wired; only
        # fenced/indented/span regions are blanked.
        assert _is_wired(tmp_path, f"> {_IMP}\n")

    def test_import_after_closed_list_fence_stays_wired(self, tmp_path):
        assert _is_wired(tmp_path, f"- ```\n  code\n  ```\n\n{_IMP}\n")

    def test_container_drop_mid_fence_returns_to_prose(self, tmp_path):
        # A non-blank line that drops the blockquote prefix ends the container
        # and its fence; the line itself is prose again.
        assert _is_wired(tmp_path, f"> ```\n{_IMP}\n")

    def test_import_after_closed_blockquote_fence_stays_wired(self, tmp_path):
        assert _is_wired(tmp_path, f"> ```\n> code\n> ```\n\n{_IMP}\n")

    def test_nested_list_blockquote_fence_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"- > ```\n  > {_IMP}\n  > ```\n")


class TestInlineSpanPairing:
    """Backtick strings pair with the next run of EQUAL length; spans may
    cross a soft line break but never a blank line, and unpaired runs are
    literal text."""

    def test_double_backtick_span_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"Wire it with ``{_IMP}`` in CLAUDE.md.\n")

    def test_triple_backtick_inline_span_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"Use ```{_IMP}``` here.\n")

    def test_multiline_span_is_not_wired(self, tmp_path):
        assert not _is_wired(tmp_path, f"see `quoted\n{_IMP} still quoted` end\n")

    def test_span_does_not_cross_blank_line(self, tmp_path):
        assert _is_wired(tmp_path, f"a `stray opener\n\n{_IMP} tail`\n")

    def test_unpaired_backtick_leaves_import_live(self, tmp_path):
        assert _is_wired(tmp_path, f"a lone ` here and {_IMP}\n")

    def test_unequal_runs_do_not_pair_across_import(self, tmp_path):
        assert not _is_wired(tmp_path, f"mix `a`` {_IMP} ``b` end\n")

    def test_span_pairs_across_lazy_indented_line(self, tmp_path):
        # An indented line inside a paragraph is a lazy continuation, not a
        # code block; the span around it still pairs and blanks the import.
        assert not _is_wired(tmp_path, f"see `code\n    filler\n{_IMP}` here\n")

    def test_lazy_line_backtick_runs_still_pair(self, tmp_path):
        # A lazy line's own backtick runs take part in pairing; dropping them
        # would shift later pairs and leave the import live.
        assert not _is_wired(tmp_path, f"a ` b\n    ` c\n`{_IMP}` end\n")

    def test_span_closer_on_lazy_line_blanks_import(self, tmp_path):
        assert not _is_wired(tmp_path, f"`{_IMP}\n    x`\nend\n")

    def test_span_opener_on_lazy_line_blanks_import(self, tmp_path):
        assert not _is_wired(tmp_path, f"prose\n    `x\n{_IMP}` end\n")

    def test_blockquoted_lazy_closer_blanks_import(self, tmp_path):
        assert not _is_wired(tmp_path, f"> `{_IMP}\n>     x`\n> end\n")

    def test_ordered_non_one_fence_is_lazy_text(self, tmp_path):
        # "2)" cannot interrupt a paragraph, so its fence marker is paragraph
        # text and the surrounding span still pairs.
        assert not _is_wired(tmp_path, f"text `span\n2) ```\n{_IMP}` end\n")

    def test_import_outside_spans_after_lazy_line_stays_wired(self, tmp_path):
        # Pairing consumes the dangling opener across the lazy line, leaving
        # the later import outside any span - plain prose, so wired.
        assert _is_wired(tmp_path, f"dangling ` here\n    x\nalso `{_IMP}` tail\n")
