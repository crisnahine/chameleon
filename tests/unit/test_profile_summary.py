"""Unit tests for chameleon_mcp.profile.summary.

Pins the exact Markdown shape of profile.summary.md plus the two pure
helpers it relies on (count_terminal_rules, extract_idioms_section).

This module is pure-logic: it reads no env vars, opens no files, and
holds no connection cache. The autouse fixture below replicates the
suite-wide isolation pattern (CHAMELEON_PLUGIN_DATA pinned to tmp_path)
so this file behaves identically to its siblings even though the
target never touches that env var.
"""

from __future__ import annotations

import pytest

from chameleon_mcp.profile.summary import (
    count_terminal_rules,
    extract_idioms_section,
    render_summary_md,
)


@pytest.fixture(autouse=True)
def _isolate_plugin_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_PLUGIN_DATA", str(tmp_path))


def _lines(text: str) -> list[str]:
    return text.split("\n")


# --------------------------------------------------------------------------
# count_terminal_rules
# --------------------------------------------------------------------------


class TestCountTerminalRules:
    def test_empty_dict_is_zero(self):
        assert count_terminal_rules({}) == 0

    def test_each_scalar_value_counts_one(self):
        assert count_terminal_rules({"a": 1, "b": 2}) == 2

    def test_list_value_counts_its_length(self):
        assert count_terminal_rules({"rules": [1, 2, 3]}) == 3

    def test_empty_list_counts_zero(self):
        assert count_terminal_rules({"rules": []}) == 0

    def test_nested_dict_recurses_and_sums(self):
        # {"b": 1} -> 1, "c": [1,2] -> 2, "d": 5 -> 1  => 4
        assert count_terminal_rules({"a": {"b": 1, "c": [1, 2]}, "d": 5}) == 4

    def test_non_dict_top_level_returns_zero(self):
        assert count_terminal_rules("not-a-dict") == 0
        assert count_terminal_rules([1, 2, 3]) == 0
        assert count_terminal_rules(None) == 0

    @staticmethod
    def _nest(levels: int) -> dict:
        """Build `levels` dict layers with one scalar leaf at the bottom."""
        root: dict = {}
        cur = root
        for _ in range(levels - 1):
            cur["k"] = {}
            cur = cur["k"]
        cur["leaf"] = 1
        return root

    def test_depth_cap_counts_leaf_at_seven_layers(self):
        # innermost dict sits at depth 6 (cap is depth > 6 returns 0), still counted.
        assert count_terminal_rules(self._nest(7)) == 1

    def test_depth_cap_drops_leaf_beyond_seven_layers(self):
        # innermost dict sits at depth 7 -> recursion short-circuits to 0.
        assert count_terminal_rules(self._nest(8)) == 0
        assert count_terminal_rules(self._nest(12)) == 0


# --------------------------------------------------------------------------
# extract_idioms_section
# --------------------------------------------------------------------------


class TestExtractIdiomsSection:
    def test_returns_stripped_section_body(self):
        md = "## active\n- use X\n- use Y\n## deprecated\n- old Z\n"
        assert extract_idioms_section(md, "## active") == "- use X\n- use Y"

    def test_returns_trailing_section_body(self):
        md = "## active\n- use X\n## deprecated\n- old Z\n"
        assert extract_idioms_section(md, "## deprecated") == "- old Z"

    def test_missing_marker_returns_empty(self):
        md = "## active\n- use X\n"
        assert extract_idioms_section(md, "## nope") == ""

    def test_none_placeholder_treated_as_empty(self):
        assert extract_idioms_section("## active\n_(none)_\n", "## active") == ""

    def test_no_idioms_yet_phrase_treated_as_empty(self):
        assert extract_idioms_section("## active\nno idioms yet here\n", "## active") == ""

    def test_blank_section_body_returns_empty(self):
        md = "## active\n\n## deprecated\nx"
        assert extract_idioms_section(md, "## active") == ""

    def test_only_first_subsequent_section_is_dropped(self):
        # split on "\n## " keeps content only up to the next level-2 heading.
        md = "## active\nbody-a\n## deprecated\nbody-b\n## extra\nbody-c"
        assert extract_idioms_section(md, "## active") == "body-a"


# --------------------------------------------------------------------------
# render_summary_md — header + metadata
# --------------------------------------------------------------------------


class TestRenderHeader:
    def test_engine_version_argument_wins_over_meta(self):
        out = render_summary_md(
            archetypes={},
            canonicals={},
            profile_meta={"engine_min_version": "1.4.0"},
            idioms_text="",
            engine_version="9.9.9",
        )
        assert "Engine: chameleon v9.9.9" in _lines(out)

    def test_engine_version_falls_back_to_meta_when_none(self):
        out = render_summary_md(
            archetypes={},
            canonicals={},
            profile_meta={"engine_min_version": "7.7.7"},
            idioms_text="",
            engine_version=None,
        )
        assert "Engine: chameleon v7.7.7" in _lines(out)

    def test_header_metadata_lines_use_meta_values(self):
        meta = {
            "created_at": "2026-05-31T00:00:00Z",
            "engine_min_version": "1.4.0",
            "language": "ruby",
            "source": "merge",
            "generation": 3,
            "schema_version": 2,
            "archetype_count": 0,
        }
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta=meta, idioms_text="")
        lines = _lines(out)
        assert lines[0] == "# chameleon profile summary"
        assert "Generated: 2026-05-31T00:00:00Z" in lines
        assert "Language: ruby" in lines
        assert "Source: merge" in lines
        assert "Generation: 3" in lines
        assert "Schema version: 2" in lines

    def test_source_defaults_to_bootstrap_when_absent(self):
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text="")
        assert "Source: bootstrap" in _lines(out)

    def test_empty_meta_renders_blank_values_not_crash(self):
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text="")
        lines = _lines(out)
        assert "Generated: " in lines
        assert "Engine: chameleon v" in lines
        assert "Language: " in lines


# --------------------------------------------------------------------------
# render_summary_md — secondary-language hint
# --------------------------------------------------------------------------


class TestSecondaryLanguageHint:
    def _meta_with_hint(self, **overrides) -> dict:
        hint = {
            "secondary_detected": "ruby",
            "primary": "typescript",
            "secondary_file_count": 42,
            "secondary_path": "api/",
            "note": "Run init in api/ for a separate Ruby profile.",
        }
        hint.update(overrides)
        return {"archetype_count": 0, "language_hint": hint}

    def test_hint_section_rendered_with_counts_and_paths(self):
        out = render_summary_md(
            archetypes={},
            canonicals={},
            profile_meta=self._meta_with_hint(),
            idioms_text="",
        )
        assert "## Secondary language detected" in _lines(out)
        assert (
            "This bootstrap scanned **typescript** only. A sibling **ruby** codebase "
            "(42 files at `api/`) was deliberately excluded." in _lines(out)
        )
        assert "Run init in api/ for a separate Ruby profile." in _lines(out)

    def test_hint_absent_omits_section(self):
        out = render_summary_md(
            archetypes={},
            canonicals={},
            profile_meta={"archetype_count": 0},
            idioms_text="",
        )
        assert "## Secondary language detected" not in out

    def test_hint_without_secondary_detected_is_ignored(self):
        # A hint dict that exists but has a falsy secondary_detected must not
        # render the section.
        meta = {"archetype_count": 0, "language_hint": {"primary": "ruby"}}
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta=meta, idioms_text="")
        assert "## Secondary language detected" not in out

    def test_non_dict_hint_is_ignored(self):
        meta = {"archetype_count": 0, "language_hint": "ruby"}
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta=meta, idioms_text="")
        assert "## Secondary language detected" not in out


# --------------------------------------------------------------------------
# render_summary_md — archetypes section
# --------------------------------------------------------------------------


class TestArchetypesSection:
    def test_archetype_count_header_from_meta(self):
        out = render_summary_md(
            archetypes={}, canonicals={}, profile_meta={"archetype_count": 7}, idioms_text=""
        )
        assert "## 7 archetypes detected" in _lines(out)

    def test_archetype_count_defaults_to_zero(self):
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text="")
        assert "## 0 archetypes detected" in _lines(out)

    def test_archetypes_rendered_sorted_by_name(self):
        archetypes = {
            "archetypes": {
                "model": {
                    "cluster_size": 12,
                    "paths_pattern": "app/models/**",
                    "paths_pattern_display": "app/models/",
                },
                "component": {"cluster_size": 5, "paths_pattern": "src/components/**"},
            }
        }
        canonicals = {"canonicals": {"model": [{"witness": {"path": "app/models/user.rb"}}]}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals=canonicals,
            profile_meta={"archetype_count": 2},
            idioms_text="",
        )
        lines = _lines(out)
        comp_idx = lines.index(
            "- **component** (cluster_size 5, paths src/components/**) — canonical: `(none)`"
        )
        model_idx = lines.index(
            "- **model** (cluster_size 12, paths app/models/) — canonical: `app/models/user.rb`"
        )
        # sorted() places component before model.
        assert comp_idx < model_idx

    def test_paths_pattern_display_preferred_over_raw(self):
        archetypes = {
            "archetypes": {
                "svc": {
                    "cluster_size": 1,
                    "paths_pattern": "app/services/**",
                    "paths_pattern_display": "app/services/",
                }
            }
        }
        out = render_summary_md(
            archetypes=archetypes,
            canonicals={},
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        line = next(li for li in _lines(out) if li.startswith("- **svc**"))
        assert "paths app/services/)" in line
        assert "app/services/**" not in line

    def test_falls_back_to_raw_paths_pattern_when_no_display(self):
        archetypes = {
            "archetypes": {"svc": {"cluster_size": 1, "paths_pattern": "app/services/**"}}
        }
        out = render_summary_md(
            archetypes=archetypes,
            canonicals={},
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        line = next(li for li in _lines(out) if li.startswith("- **svc**"))
        assert "paths app/services/**)" in line

    def test_missing_cluster_size_and_paths_default(self):
        archetypes = {"archetypes": {"bare": {}}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals={},
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        line = next(li for li in _lines(out) if li.startswith("- **bare**"))
        assert line == "- **bare** (cluster_size 0, paths ) — canonical: `(none)`"

    def test_no_canonical_renders_none(self):
        archetypes = {"archetypes": {"a": {"cluster_size": 1, "paths_pattern": "x/**"}}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals={"canonicals": {}},
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        assert "- **a** (cluster_size 1, paths x/**) — canonical: `(none)`" in _lines(out)

    def test_empty_witness_path_renders_none(self):
        archetypes = {"archetypes": {"a": {"cluster_size": 1, "paths_pattern": "x/**"}}}
        canonicals = {"canonicals": {"a": [{"witness": {"path": ""}}]}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals=canonicals,
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        assert "canonical: `(none)`" in next(li for li in _lines(out) if li.startswith("- **a**"))

    def test_witness_without_path_key_renders_none(self):
        archetypes = {"archetypes": {"c": {"cluster_size": 3, "paths_pattern": ""}}}
        canonicals = {"canonicals": {"c": [{"witness": {"line": 5}}]}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals=canonicals,
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        assert "canonical: `(none)`" in next(li for li in _lines(out) if li.startswith("- **c**"))

    def test_non_dict_first_canonical_entry_renders_none(self):
        archetypes = {"archetypes": {"b": {"cluster_size": 2, "paths_pattern": "x/**"}}}
        canonicals = {"canonicals": {"b": ["not-a-dict-entry"]}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals=canonicals,
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        assert "canonical: `(none)`" in next(li for li in _lines(out) if li.startswith("- **b**"))

    def test_first_canonical_witness_used_when_multiple(self):
        archetypes = {"archetypes": {"m": {"cluster_size": 9, "paths_pattern": "app/models/**"}}}
        canonicals = {
            "canonicals": {
                "m": [
                    {"witness": {"path": "app/models/first.rb"}},
                    {"witness": {"path": "app/models/second.rb"}},
                ]
            }
        }
        out = render_summary_md(
            archetypes=archetypes,
            canonicals=canonicals,
            profile_meta={"archetype_count": 1},
            idioms_text="",
        )
        line = next(li for li in _lines(out) if li.startswith("- **m**"))
        assert "`app/models/first.rb`" in line
        assert "second.rb" not in line


# --------------------------------------------------------------------------
# render_summary_md — rules section
# --------------------------------------------------------------------------


class TestRulesSection:
    def test_no_rules_data_renders_placeholder(self):
        out = render_summary_md(
            archetypes={}, canonicals={}, profile_meta={}, idioms_text="", rules_data=None
        )
        assert "_No tool-config rules detected._" in out
        assert "`eslint`, `tsconfig`, `prettier`, `rubocop`, and `.editorconfig`" in out

    def test_empty_rules_dict_renders_placeholder(self):
        out = render_summary_md(
            archetypes={},
            canonicals={},
            profile_meta={},
            idioms_text="",
            rules_data={"rules": {}},
        )
        assert "_No tool-config rules detected._" in out

    def test_detected_tools_listed_sorted_with_counts(self):
        rules_data = {
            "rules": {
                "tsconfig": {"strict": True},
                "rubocop": {"Style/Foo": True, "Layout/Bar": {"x": 1}},
            }
        }
        out = render_summary_md(
            archetypes={}, canonicals={}, profile_meta={}, idioms_text="", rules_data=rules_data
        )
        lines = _lines(out)
        assert "_Auto-derived from 2 tool config file(s): `rubocop`, `tsconfig`._" in lines
        # rubocop: Style/Foo scalar (1) + Layout/Bar nested {x:1} (1) = 2
        assert "- **rubocop** — 2 rule(s) extracted" in lines
        assert "- **tsconfig** — 1 rule(s) extracted" in lines
        # sorted: rubocop bullet precedes tsconfig bullet
        assert lines.index("- **rubocop** — 2 rule(s) extracted") < lines.index(
            "- **tsconfig** — 1 rule(s) extracted"
        )

    def test_non_dict_tool_block_counted_in_header_but_no_bullet(self):
        # header counts every key (2), but a non-dict value is skipped for the
        # per-tool bullet.
        rules_data = {"rules": {"good": {"a": 1}, "bad": "not-a-dict"}}
        out = render_summary_md(
            archetypes={}, canonicals={}, profile_meta={}, idioms_text="", rules_data=rules_data
        )
        lines = _lines(out)
        assert "_Auto-derived from 2 tool config file(s): `bad`, `good`._" in lines
        assert "- **good** — 1 rule(s) extracted" in lines
        assert not any(li.startswith("- **bad**") for li in lines)


# --------------------------------------------------------------------------
# render_summary_md — idioms section
# --------------------------------------------------------------------------


class TestIdiomsSection:
    def test_no_idioms_renders_teach_prompt(self):
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text="")
        assert (
            "_No idioms captured yet. Run /chameleon-teach to record team conventions._"
            in _lines(out)
        )

    def test_active_idioms_rendered_with_trust_warning(self):
        idioms = "## active\n- always use Foo\n- never inline SQL\n"
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text=idioms)
        assert "- always use Foo\n- never inline SQL" in out
        assert "Review carefully before granting trust." in out
        # teach prompt is suppressed when active idioms exist
        assert "_No idioms captured yet." not in out

    def test_placeholder_active_idioms_shows_teach_prompt(self):
        idioms = "## active\n_(none)_\n"
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text=idioms)
        assert "_No idioms captured yet." in out

    def test_deprecated_idioms_section_rendered(self):
        idioms = "## active\n- use Foo\n## deprecated\n- old Bar\n"
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text=idioms)
        assert "## Deprecated idioms" in _lines(out)
        assert "- old Bar" in _lines(out)
        assert "kept here for audit history and are NOT injected into" in out

    def test_no_deprecated_section_when_absent(self):
        idioms = "## active\n- use Foo\n"
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text=idioms)
        assert "## Deprecated idioms" not in out

    def test_placeholder_deprecated_idioms_omitted(self):
        idioms = "## active\n- use Foo\n## deprecated\n_(none)_\n"
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text=idioms)
        assert "## Deprecated idioms" not in out


# --------------------------------------------------------------------------
# render_summary_md — section ordering / structure
# --------------------------------------------------------------------------


class TestSectionStructure:
    def test_all_top_sections_appear_in_order(self):
        idioms = "## active\n- use Foo\n## deprecated\n- old Bar\n"
        meta = {
            "archetype_count": 1,
            "language_hint": {
                "secondary_detected": "ruby",
                "primary": "typescript",
                "secondary_file_count": 1,
                "secondary_path": "api/",
                "note": "n",
            },
        }
        archetypes = {"archetypes": {"a": {"cluster_size": 1, "paths_pattern": "x/**"}}}
        out = render_summary_md(
            archetypes=archetypes,
            canonicals={},
            profile_meta=meta,
            idioms_text=idioms,
            rules_data={"rules": {"rubocop": {"r": 1}}},
        )
        order = [
            "# chameleon profile summary",
            "## Secondary language detected",
            "## 1 archetypes detected",
            "## Rules",
            "## Idioms",
            "## Deprecated idioms",
        ]
        positions = [out.index(s) for s in order]
        assert positions == sorted(positions)

    def test_output_is_newline_joined_string(self):
        out = render_summary_md(archetypes={}, canonicals={}, profile_meta={}, idioms_text="")
        assert isinstance(out, str)
        # no trailing newline appended by the renderer beyond the joined lines
        assert not out.endswith("\n\n\n")
