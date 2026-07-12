"""Language scoping in ``_render_stop_idioms``.

The turn-end self-review renderer drops idiom blocks whose ``Language:`` tag is
a recognized concrete language the turn did not edit. Untagged blocks,
``Language: any``, and unrecognized tags always survive (fail open to shown),
and an empty ``edited_languages`` disables the filter entirely (cannot scope).
"""

from __future__ import annotations

from chameleon_mcp.tools import _render_stop_idioms

RUBY_BLOCK = (
    "### ruby-slack\nLanguage: ruby\nStatus: active\nSlack posts go through the service objects.\n"
)
TS_BLOCK = "### ts-imports\nLanguage: typescript\nStatus: active\nUse the api client wrapper.\n"
ANY_BLOCK = "### any-transactions\nLanguage: any\nStatus: active\nWrap writes in a transaction.\n"
UNTAGGED_BLOCK = "### untagged-naming\nStatus: active\nName handlers handle_<event>.\n"
ODD_TAG_BLOCK = "### odd-tag\nLanguage: cobol\nStatus: active\nKeep the ledger idempotent.\n"

CAPS = {"char_cap": 3000, "max_terse": 25, "summary_max_chars": 160}


def test_other_language_block_dropped():
    out = _render_stop_idioms(
        RUBY_BLOCK + "\n" + TS_BLOCK, [], set(), edited_languages=["typescript"], **CAPS
    )
    assert "ts-imports" in out
    assert "ruby-slack" not in out


def test_matching_language_block_kept():
    out = _render_stop_idioms(RUBY_BLOCK, [], set(), edited_languages=["ruby"], **CAPS)
    assert "ruby-slack" in out


def test_untagged_any_and_unrecognized_tags_survive():
    text = "\n".join([RUBY_BLOCK, ANY_BLOCK, UNTAGGED_BLOCK, ODD_TAG_BLOCK])
    out = _render_stop_idioms(text, [], set(), edited_languages=["python"], **CAPS)
    assert "ruby-slack" not in out
    assert "any-transactions" in out
    assert "untagged-naming" in out
    assert "odd-tag" in out


def test_no_edited_languages_disables_filter():
    for langs in (None, []):
        out = _render_stop_idioms(RUBY_BLOCK, [], set(), edited_languages=langs, **CAPS)
        assert "ruby-slack" in out


def test_all_blocks_scoped_out_renders_empty():
    out = _render_stop_idioms(RUBY_BLOCK, [], set(), edited_languages=["typescript"], **CAPS)
    assert out == ""


def test_language_filter_composes_with_archetype_filter():
    # Same language but another archetype -> dropped by the archetype filter;
    # same archetype but another language -> dropped by the language filter.
    text = (
        "### svc-ts\nLanguage: typescript\nArchetype: service\nStatus: active\nA.\n\n"
        "### ctrl-ts\nLanguage: typescript\nArchetype: controller\nStatus: active\nB.\n\n"
        "### svc-rb\nLanguage: ruby\nArchetype: service\nStatus: active\nC.\n"
    )
    out = _render_stop_idioms(text, ["service"], set(), edited_languages=["typescript"], **CAPS)
    assert "svc-ts" in out
    assert "ctrl-ts" not in out
    assert "svc-rb" not in out


def test_language_line_inside_fenced_example_does_not_tag_untagged_block():
    # An untagged idiom whose fenced example contains a column-start
    # `Language: ruby` line must survive: only the metadata region before the
    # first fence is sniffed for the tag.
    text = (
        "### untagged-with-fence\nStatus: active\nFront matter looks like this.\n"
        "Example:\n```\nLanguage: ruby\ntitle: post\n```\n"
    )
    out = _render_stop_idioms(text, [], set(), edited_languages=["typescript"], **CAPS)
    assert "untagged-with-fence" in out


def test_seen_language_scoped_block_still_summarized_when_kept():
    # The language filter runs before seen/unseen: a kept, already-seen idiom
    # renders as its one-line summary, not full text.
    out = _render_stop_idioms(TS_BLOCK, [], {"ts-imports"}, edited_languages=["typescript"], **CAPS)
    assert out.startswith("- ts-imports")
