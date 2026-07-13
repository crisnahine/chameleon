"""Unit tests for chameleon_mcp.conventions — schema, serialization, extraction."""

from __future__ import annotations

import json
from pathlib import Path

from chameleon_mcp.conventions import (
    CONVENTIONS_SCHEMA_VERSION,
    empty_conventions,
    extract_all_conventions,
    extract_import_conventions,
    extract_inheritance_conventions,
    extract_key_exports,
    extract_method_call_conventions,
    extract_naming_conventions,
    format_conventions_for_session,
    serialize_conventions,
)
from chameleon_mcp.extractors._base import ParsedFile


def _make_parsed_file(
    path: str, imports: list[tuple[str, str]], *, top_level_kinds: tuple[str, ...] = ()
) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=top_level_kinds,
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=tuple(imports),
        has_jsx=False,
    )


class TestConventionsSchema:
    def test_empty_conventions_has_schema_version(self):
        c = empty_conventions(generation=42)
        assert c["schema_version"] == CONVENTIONS_SCHEMA_VERSION
        assert c["generation"] == 42
        assert c["conventions"]["imports"] == {}
        assert c["conventions"]["naming"] == {}

    def test_serialize_round_trip(self):
        c = empty_conventions(generation=1)
        c["conventions"]["imports"]["model"] = {
            "preferred": [
                {"module": "useCustomQuery", "source": "@/hooks", "frequency": 47, "total": 52}
            ],
            "competing": [
                {
                    "preferred": "useCustomQuery",
                    "over": "useQuery",
                    "preferred_count": 47,
                    "over_count": 0,
                }
            ],
        }
        c["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = serialize_conventions(c)
        parsed = json.loads(text)
        assert (
            parsed["conventions"]["imports"]["model"]["preferred"][0]["module"] == "useCustomQuery"
        )
        assert (
            parsed["conventions"]["naming"]["component"]["interface_prefix"]["consistency"] == 0.999
        )


class TestImportFrequencyExtractor:
    def test_detects_preferred_import(self):
        files = [
            _make_parsed_file(f"src/hooks/use{i}.ts", [("@/lib/api", "named")]) for i in range(15)
        ]
        result = extract_import_conventions(files)
        preferred = [p["module"] for p in result.get("preferred", [])]
        assert "@/lib/api" in preferred

    def test_skips_below_min_sample_size(self):
        files = [_make_parsed_file(f"src/f{i}.ts", [("react", "named")]) for i in range(5)]
        result = extract_import_conventions(files)
        assert result == {"preferred": [], "competing": []}

    def test_detects_competing_imports(self):
        files = []
        for i in range(20):
            if i < 15:
                files.append(_make_parsed_file(f"src/h{i}.ts", [("useCustomQuery", "named")]))
            else:
                files.append(_make_parsed_file(f"src/u{i}.ts", [("somethingElse", "named")]))
        result = extract_import_conventions(files, competing_pairs=[("useCustomQuery", "useQuery")])
        competing = result.get("competing", [])
        assert len(competing) == 1
        assert competing[0]["preferred"] == "useCustomQuery"
        assert competing[0]["over"] == "useQuery"

    def test_excludes_framework_mandatory(self):
        files = [
            _make_parsed_file(f"src/f{i}.ts", [("react", "namespace"), ("@/lib/api", "named")])
            for i in range(20)
        ]
        result = extract_import_conventions(files)
        preferred_modules = [p["module"] for p in result.get("preferred", [])]
        assert "react" not in preferred_modules
        assert "@/lib/api" in preferred_modules


class TestNamingExtractor:
    def test_detects_interface_i_prefix(self):
        declarations = [
            "IUserProps",
            "IChartData",
            "IListingData",
            "IApiResponse",
            "ITableRow",
            "IFormValues",
            "IModalProps",
            "ISearchParams",
            "IFilterState",
            "IConfig",
        ]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result["interface_prefix"]["pattern"] == "I"
        assert result["interface_prefix"]["consistency"] >= 0.95

    def test_no_prefix_when_inconsistent(self):
        declarations = ["IFoo", "Bar", "IBaz", "Qux", "Hello"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert (
            "interface_prefix" not in result
            or result.get("interface_prefix", {}).get("consistency", 0) < 0.6
        )

    def test_detects_type_t_prefix(self):
        declarations = ["TTheme", "TRoute", "TConfig", "TState", "TProps", "TData"]
        result = extract_naming_conventions(declarations={"type": declarations})
        assert result["type_prefix"]["pattern"] == "T"

    def test_skips_below_min_sample(self):
        declarations = ["IFoo", "IBar"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result == {}

    def test_no_prefix_convention_for_bulletproof_style(self):
        declarations = [
            "UserProps",
            "ChartData",
            "ListingData",
            "ApiResponse",
            "TableRow",
            "FormValues",
        ]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert "interface_prefix" not in result


RUBY_FINDER_SRC = """\
# frozen_string_literal: true

class UsersFinder
  MAX_RESULTS = 100
  DEFAULT_SCOPE = :active

  def execute
    filter_users(base_scope)
  end

  private

  def filter_users(scope)
    scope.where(active: true)
  end

  def self.cache_key
    'users_finder'
  end
end
"""


class TestRubyDeclarationExtraction:
    def test_extracts_methods_classes_constants(self):
        from chameleon_mcp.conventions import extract_declarations_from_content

        decls = extract_declarations_from_content(RUBY_FINDER_SRC, language="ruby")
        assert decls["method"] == ["execute", "filter_users", "cache_key"]
        assert decls["class"] == ["UsersFinder"]
        assert decls["constant"] == ["MAX_RESULTS", "DEFAULT_SCOPE"]

    def test_namespaced_class_and_module_captured(self):
        from chameleon_mcp.conventions import extract_declarations_from_content

        src = "module Ci\n  class BuildsFinder\n  end\nend\n"
        decls = extract_declarations_from_content(src, language="ruby")
        assert decls["class"] == ["Ci", "BuildsFinder"]

    def test_heredoc_and_comment_noise_ignored(self):
        from chameleon_mcp.conventions import extract_declarations_from_content

        src = (
            "class Mailer\n"
            "  # def commentedOut\n"
            "  BODY = <<~TEXT\n"
            "    def fakeMethod\n"
            "    class fake_class\n"
            "  TEXT\n"
            "end\n"
        )
        decls = extract_declarations_from_content(src, language="ruby")
        assert decls.get("method") is None
        assert decls["class"] == ["Mailer"]

    def test_operator_and_setter_defs(self):
        from chameleon_mcp.conventions import extract_declarations_from_content

        src = "class C\n  def ==(other)\n  end\n\n  def name=(value)\n  end\nend\n"
        decls = extract_declarations_from_content(src, language="ruby")
        # Operator defs carry no casing signal; setters do.
        assert decls.get("method") == ["name="]


class TestRubyNamingConventions:
    def test_detects_snake_case_methods(self):
        names = ["execute", "filter_users", "cache_key", "find_by_id", "valid?", "save!"]
        result = extract_naming_conventions(declarations={"method": names})
        assert result["method_casing"]["pattern"] == "snake_case"
        assert result["method_casing"]["consistency"] >= 0.95

    def test_detects_pascal_case_classes(self):
        names = ["UsersFinder", "Ci", "BuildsFinder", "ApplicationRecord", "ProjectPolicy"]
        result = extract_naming_conventions(declarations={"class": names})
        assert result["class_casing"]["pattern"] == "PascalCase"

    def test_detects_screaming_snake_constants(self):
        names = ["MAX_RESULTS", "DEFAULT_SCOPE", "API_VERSION", "TTL", "RETRY_LIMIT"]
        result = extract_naming_conventions(declarations={"constant": names})
        assert result["constant_casing"]["pattern"] == "SCREAMING_SNAKE_CASE"

    def test_mixed_methods_below_threshold_silent(self):
        names = ["execute", "fetchData", "getUser", "callApi", "runJob"]
        result = extract_naming_conventions(declarations={"method": names})
        assert "method_casing" not in result

    def test_below_min_sample_silent(self):
        result = extract_naming_conventions(declarations={"method": ["a_b", "c_d"]})
        assert result == {}

    def test_unicode_snake_case_methods_not_misclassified(self):
        # A legal PEP 3131 identifier that mixes ASCII with non-ASCII letters
        # but carries no uppercase letter is genuinely snake_case in its own
        # script; an ASCII-only casing check would wrongly count it as
        # non-conforming and tank the derived consistency.
        names = ["calc_café", "fetch_naïve_result", "procesar_dados", "cache_key", "save_state"]
        result = extract_naming_conventions(declarations={"method": names})
        assert result["method_casing"]["pattern"] == "snake_case"
        assert result["method_casing"]["consistency"] == 1.0


class TestExtractAllConventions:
    def test_produces_conventions_dict(self):
        files_by_archetype = {
            "component": [
                _make_parsed_file(
                    f"src/c{i}.tsx", [("react", "namespace"), ("@/hooks/useCustomQuery", "named")]
                )
                for i in range(15)
            ],
        }
        declarations_by_archetype = {
            "component": {"interface": [f"I{chr(65 + i)}Props" for i in range(10)]},
        }
        result = extract_all_conventions(
            files_by_archetype=files_by_archetype,
            declarations_by_archetype=declarations_by_archetype,
            generation=42,
        )
        assert result["schema_version"] == CONVENTIONS_SCHEMA_VERSION
        assert result["generation"] == 42
        assert "component" in result["conventions"]["imports"]
        assert "component" in result["conventions"]["naming"]

    def test_empty_when_no_archetypes(self):
        result = extract_all_conventions(
            files_by_archetype={},
            declarations_by_archetype={},
            generation=1,
        )
        assert result["conventions"]["imports"] == {}
        assert result["conventions"]["naming"] == {}
        assert result["generation"] == 1

    def test_import_only_when_no_declarations(self):
        files_by_archetype = {
            "hook": [
                _make_parsed_file(f"src/hooks/use{i}.ts", [("@/lib/api", "named")])
                for i in range(15)
            ],
        }
        result = extract_all_conventions(
            files_by_archetype=files_by_archetype,
            declarations_by_archetype={},
            generation=7,
        )
        assert "hook" in result["conventions"]["imports"]
        assert result["conventions"]["naming"] == {}

    def test_skips_archetype_below_sample_size(self):
        files_by_archetype = {
            "tiny": [_make_parsed_file(f"src/t{i}.ts", [("lodash", "named")]) for i in range(3)],
        }
        result = extract_all_conventions(
            files_by_archetype=files_by_archetype,
            declarations_by_archetype={},
            generation=1,
        )
        assert "tiny" not in result["conventions"]["imports"]


class TestFormatConventionsForSession:
    def test_formats_import_competing(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["component"] = {
            "preferred": [],
            "competing": [
                {
                    "preferred": "useCustomQuery",
                    "over": "useQuery",
                    "preferred_count": 47,
                    "over_count": 0,
                }
            ],
        }
        text = format_conventions_for_session(conventions)
        assert "useCustomQuery" in text
        assert "not useQuery" in text
        # authoritative header: the block must claim CLAUDE.md-grade authority and
        # preempt the majority-inference rationalization (migration-A/B finding)
        assert "authoritative" in text
        assert "mid-migration" in text

    def test_formats_naming_enforced(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = format_conventions_for_session(conventions)
        assert "I" in text
        assert "interface" in text.lower()

    def test_formats_ruby_casing_conventions(self):
        # method_casing/class_casing/constant_casing (Ruby's in-source casing
        # signal) share the prefix keys' {pattern, consistency} shape but were
        # never rendered into any convention-delivery channel until this test.
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["service"] = {
            "method_casing": {"pattern": "snake_case", "consistency": 1.0, "sample_size": 500},
            "class_casing": {"pattern": "PascalCase", "consistency": 1.0, "sample_size": 50},
            "constant_casing": {
                "pattern": "SCREAMING_SNAKE_CASE",
                "consistency": 1.0,
                "sample_size": 10,
            },
        }
        text = format_conventions_for_session(conventions)
        assert "Name methods in snake_case" in text
        assert "Name classes in PascalCase" in text
        assert "Name constants in SCREAMING_SNAKE_CASE" in text

    def test_formats_ruby_casing_conventions_strong_but_not_enforced(self):
        # Regression: a consistency in [_STRONG_THRESHOLD, _ENFORCE_THRESHOLD)
        # takes the NOT-enforced branch, which raised UnboundLocalError on
        # `type_name` (a leftover reference from before the plural-tuple
        # rewrite) -- crashing format_conventions_for_session entirely and
        # silently dropping the whole SessionStart conventions block, not just
        # the casing line. The prior test above only ever exercised the
        # enforced (>=95%) branch.
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["service"] = {
            "method_casing": {"pattern": "snake_case", "consistency": 0.75, "sample_size": 100},
        }
        text = format_conventions_for_session(conventions)
        assert "Name methods in snake_case (75%)" in text
        assert "enforced" not in text.split("NAMING:")[1].split("\n\n")[0]

    def test_empty_conventions_with_principles(self):
        conventions = empty_conventions(generation=1)
        text = format_conventions_for_session(
            conventions, principles_text="1. Search the codebase for existing utilities."
        )
        assert "PRINCIPLES:" in text
        assert "Search the codebase" in text

    def test_empty_conventions_without_principles(self):
        conventions = empty_conventions(generation=1)
        text = format_conventions_for_session(conventions)
        assert text == ""

    def test_skips_below_60_percent_but_keeps_principles(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["component"] = {
            "enum_prefix": {"pattern": "E", "consistency": 0.55, "sample_size": 8},
        }
        text = format_conventions_for_session(
            conventions, principles_text="1. Match testing granularity of sibling files."
        )
        assert "NAMING" not in text
        assert "PRINCIPLES:" in text


class TestFormatConventionsEcho:
    def test_compact_echo(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["hook"] = {
            "preferred": [],
            "competing": [
                {
                    "preferred": "useCustomQuery",
                    "over": "useQuery",
                    "preferred_count": 47,
                    "over_count": 0,
                }
            ],
        }
        conventions["conventions"]["naming"]["hook"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 100},
        }
        text = format_conventions_echo(conventions, archetype="hook")
        assert "useCustomQuery" in text
        assert "I-prefix" in text
        assert len(text) < 200

    def test_empty_conventions_returns_only_protocol_reminder(self):
        # With no conventions, the echo carries just the always-on
        # anti-hallucination reminder (no convention/principle parts).
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        text = format_conventions_echo(conventions, archetype="hook")
        assert text == "Verify symbols/imports/paths exist before using them; don't invent"

    def test_compact_echo_falls_back_to_ruby_casing(self):
        # No TS prefix convention for this archetype -- the echo must fall
        # back to the Ruby casing signal rather than showing no Naming line.
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["service"] = {
            "method_casing": {"pattern": "snake_case", "consistency": 1.0, "sample_size": 500},
        }
        text = format_conventions_echo(conventions, archetype="service")
        assert "Naming: methods in snake_case" in text

    def test_archetype_absent_does_not_leak_other_archetype_import(self):
        # A dimension keyed only under a DIFFERENT archetype must NOT bleed into
        # the echo for the edited archetype: the Tier-1 pointer is archetype-scoped
        # (parity with the Tier-2 _archetype_facts_section, which never falls back).
        # An arbitrary `next(iter(...values()))` fallback injected another
        # archetype's competing-import preference as if it were this file's.
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["other"] = {
            "preferred": [],
            "competing": [{"preferred": "X", "over": "Y", "preferred_count": 10, "over_count": 0}],
        }
        text = format_conventions_echo(conventions, archetype="hook")
        assert "Imports: X" not in text
        # Still non-empty: the fixed anti-hallucination reminder always trails.
        assert "Verify symbols/imports/paths exist" in text


def _make_ts_file(tmp_path, name: str, content: str) -> ParsedFile:
    """Create a real temp file and return a ParsedFile pointing to it."""
    fp = tmp_path / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return ParsedFile(
        path=fp,
        content_first_200_bytes=content[:200],
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


class TestKeyExportsExtractor:
    def test_extracts_ts_exports(self, tmp_path):
        files = []
        for name, content in [
            ("useDebounce.ts", "export const useDebounce = () => {};\n"),
            ("useToggle.ts", "export const useToggle = () => {};\n"),
            ("formatCurrency.ts", "export function formatCurrency(n: number) { return ''; }\n"),
            ("slugify.ts", "export function slugify(s: string) { return ''; }\n"),
            ("api.ts", "export const api = axios.create();\nexport const request = () => {};\n"),
        ]:
            files.append(_make_ts_file(tmp_path, name, content))
        for i in range(8):
            files.append(
                _make_ts_file(tmp_path, f"filler{i}.ts", f"export const filler{i} = {i};\n")
            )

        result = extract_key_exports(files, language="typescript")
        assert "useDebounce" in result
        assert "useToggle" in result
        assert "formatCurrency" in result
        assert "slugify" in result

    def test_extracts_ruby_exports(self, tmp_path):
        files = []
        for name, content in [
            ("user.rb", "class User < ApplicationRecord\nend\n"),
            ("listing.rb", "class Listing < ApplicationRecord\nend\n"),
            ("charts_data.rb", "module Admin\n  class ChartsData\n  end\nend\n"),
        ]:
            files.append(_make_ruby_file(tmp_path, name, content))
        for i in range(10):
            files.append(
                _make_ruby_file(tmp_path, f"m{i}.rb", f"class Model{i} < ApplicationRecord\nend\n")
            )

        result = extract_key_exports(files, language="ruby")
        assert "User" in result
        assert "Listing" in result

    def test_extracts_compact_namespaced_ruby_exports(self, tmp_path):
        # Regression: a bare \w+ recorded the outer namespace ("Api") for every
        # compact-namespaced class and lost the real name. The meaningful export
        # name is the last "::" segment.
        files = []
        for i in range(12):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class Api::V1::Widget{i}Controller < Api::V1::BaseController\nend\n",
                )
            )
        result = extract_key_exports(files, language="ruby")
        assert "Api" not in result  # outer namespace must not be the recorded name
        assert any(n.startswith("Widget") for n in result)

    def test_ruby_exports_ignore_heredoc_and_nonconstant_names(self, tmp_path):
        # A `class`/`module` keyword inside a heredoc/string is fixture text, not a
        # definition: a Go go.mod heredoc (`module javascript:alert()`,
        # `module example.com/...`) was captured as an "export". The stripper blanks
        # heredoc bodies, and the constant-shape filter drops the single-colon
        # `javascript:alert` and any empty split remnant.
        files = []
        files.append(
            _make_ruby_file(
                tmp_path,
                "go_mod_spec.rb",
                "RSpec.describe GoModViewer do\n"
                "  let(:data) { <<~GOMOD }\n"
                "    module javascript:alert()\n"
                "    module example.com/foo/bar\n"
                "  GOMOD\n"
                "end\n",
            )
        )
        for i in range(12):
            files.append(
                _make_ruby_file(
                    tmp_path, f"m{i}.rb", f"class RealModel{i} < ApplicationRecord\nend\n"
                )
            )
        result = extract_key_exports(files, language="ruby")
        assert not any("javascript" in n for n in result)
        assert "example" not in result
        assert "" not in result
        assert all(n[:1].isupper() for n in result)  # every export is a real constant
        assert any(n.startswith("RealModel") for n in result)

    def test_skips_below_sample_size(self, tmp_path):
        files = [_make_ts_file(tmp_path, "one.ts", "export const foo = 1;\n")]
        result = extract_key_exports(files, language="typescript")
        assert result == []

    def test_deduplicates_across_files(self, tmp_path):
        files = []
        for i in range(15):
            exports = "\n".join(f"export const item{j} = {j};" for j in range(30))
            files.append(_make_ts_file(tmp_path, f"f{i}.ts", exports))
        result = extract_key_exports(files, language="typescript")
        # Deduplicated to the 30 distinct names (not 15*30); the old hard cap of
        # 20 is gone (default is now effectively unbounded, env-overridable).
        assert len(result) == 30
        assert len(result) == len(set(result))


class TestFormatSessionReuse:
    def test_reuse_section_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["key_exports"]["hook"] = [
            "useDebounce",
            "useToggle",
            "formatCurrency",
        ]
        text = format_conventions_for_session(conventions)
        assert "REUSE:" in text
        assert "Check before creating:" in text
        assert "useDebounce" in text

    def test_no_reuse_when_empty(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["key_exports"] = {}
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_for_session(conventions)
        assert "REUSE:" not in text

    def test_reuse_alone_produces_output(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["key_exports"]["component"] = [
            "useDebounce",
            "useToggle",
        ]
        text = format_conventions_for_session(conventions)
        assert text != ""
        assert "<chameleon-conventions>" in text
        assert "REUSE:" in text


def _make_ruby_file(tmp_path, name: str, content: str) -> ParsedFile:
    """Create a real temp file and return a ParsedFile pointing to it."""
    fp = tmp_path / name
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return ParsedFile(
        path=fp,
        content_first_200_bytes=content[:200],
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
    )


class TestInheritanceExtractor:
    def test_detects_dominant_base_class(self, tmp_path):
        files = []
        for i in range(15):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"m{i}.rb",
                    f"class Model{i} < ApplicationRecord\n  validates :name\nend\n",
                )
            )
        result = extract_inheritance_conventions(files)
        assert result["dominant_base"] == "ApplicationRecord"
        assert result["frequency"] >= 0.9

    def test_counts_namespaced_class_base_and_records_known_bases(self, tmp_path):
        # Regression: compact-namespaced classes (`class Api::V1::X < Base`)
        # were skipped entirely by the builder regex, so an established
        # intermediate base was never learned and the linter later flagged it.
        files = []
        for i in range(10):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"c{i}.rb",
                    f"class Api::V1::C{i}Controller < ApplicationController\nend\n",
                )
            )
        for i in range(4):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"a{i}.rb",
                    f"class Api::V2::A{i}Controller < Api::V2::BaseController\nend\n",
                )
            )
        result = extract_inheritance_conventions(files)
        assert result["dominant_base"] == "ApplicationController"
        assert "Api::V2::BaseController" in result["known_bases"]
        assert "ApplicationController" in result["known_bases"]

    def test_detects_include_mixin(self, tmp_path):
        files = []
        for i in range(12):
            files.append(
                _make_ruby_file(
                    tmp_path, f"w{i}.rb", f"class Worker{i}\n  include Sidekiq::Worker\nend\n"
                )
            )
        result = extract_inheritance_conventions(files)
        assert result["dominant_include"] == "Sidekiq::Worker"

    def test_skips_below_threshold(self, tmp_path):
        files = []
        bases = ["ApplicationRecord", "BaseService", "AbstractJob"]
        for i in range(15):
            base = bases[i % 3]
            files.append(_make_ruby_file(tmp_path, f"s{i}.rb", f"class S{i} < {base}\nend\n"))
        result = extract_inheritance_conventions(files)
        assert "dominant_base" not in result

    def test_skips_below_sample_size(self, tmp_path):
        files = [_make_ruby_file(tmp_path, "m.rb", "class M < ApplicationRecord\nend\n")]
        result = extract_inheritance_conventions(files)
        assert result == {}

    def test_groups_base_family_across_namespaces(self, tmp_path):
        # ef-api shape: no single fully-qualified base clears 0.60, but the
        # bases share the unqualified name `BaseController` across namespaces, so
        # the convention is recorded as that family rather than dropped.
        files = []
        for i in range(51):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"v1_{i}.rb",
                    f"class Api::V1::C{i}Controller < Api::V1::BaseController\nend\n",
                )
            )
        for i in range(31):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"admin_{i}.rb",
                    f"class Api::V1::Admin::C{i}Controller < Api::V1::Admin::BaseController\nend\n",
                )
            )
        for i in range(15):
            files.append(
                _make_ruby_file(tmp_path, f"misc_{i}.rb", f"class Other{i} < Unrelated{i}\nend\n")
            )
        result = extract_inheritance_conventions(files)
        # 82/97 = 0.845 of controllers inherit a *BaseController family member;
        # the top single base alone was only 51/97 = 0.526, below threshold.
        assert result["base_family"] == "BaseController"
        assert result["frequency"] >= 0.60
        assert result["dominant_base"] == "Api::V1::BaseController"
        assert set(result["known_bases"]) == {
            "Api::V1::BaseController",
            "Api::V1::Admin::BaseController",
        }

    def test_no_family_when_bases_are_distinct(self, tmp_path):
        # Three distinct unqualified names, none over threshold: no family forms,
        # so the convention is still dropped (no false grouping).
        files = []
        bases = ["AlphaBase", "BetaService", "GammaJob"]
        for i in range(15):
            files.append(
                _make_ruby_file(tmp_path, f"n{i}.rb", f"class N{i} < {bases[i % 3]}\nend\n")
            )
        result = extract_inheritance_conventions(files)
        assert "dominant_base" not in result
        assert "base_family" not in result


class TestMethodCallExtractor:
    def test_detects_common_dsl_calls(self, tmp_path):
        files = []
        for i in range(15):
            files.append(
                _make_ruby_file(
                    tmp_path,
                    f"m{i}.rb",
                    f"class M{i} < ApplicationRecord\n  validates :name\n  belongs_to :user\n  scope :active, -> {{}}\nend\n",
                )
            )
        result = extract_method_call_conventions(files)
        assert "validates" in result["common_top5"]
        assert "belongs_to" in result["common_top5"]

    def test_skips_below_sample_size(self, tmp_path):
        files = [_make_ruby_file(tmp_path, "m.rb", "class M\n  validates :name\nend\n")]
        result = extract_method_call_conventions(files)
        assert result == {}


class TestFormatSessionInheritance:
    def test_inheritance_enforced_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_for_session(conventions)
        assert "INHERITANCE:" in text
        assert "ApplicationRecord" in text
        assert "enforced" in text

    def test_inheritance_strong_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.75,
            "sample_size": 50,
        }
        text = format_conventions_for_session(conventions)
        assert "INHERITANCE:" in text
        assert "ApplicationRecord" in text
        assert "enforced" not in text

    def test_include_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["worker"] = {
            "dominant_base": "ApplicationJob",
            "frequency": 0.80,
            "sample_size": 20,
            "dominant_include": "Sidekiq::Worker",
            "include_frequency": 0.90,
        }
        text = format_conventions_for_session(conventions)
        assert "Sidekiq::Worker" in text

    def test_method_calls_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        conventions["conventions"]["method_calls"]["model"] = {
            "common_top5": ["validates", "belongs_to", "scope", "before_validation", "has_many"],
            "sample_size": 117,
        }
        text = format_conventions_for_session(conventions)
        assert "PATTERNS:" in text
        assert "Common DSL:" in text
        assert "validates" in text

    def test_inheritance_only_returns_nonempty(self):
        """Inheritance alone (no imports/naming) should produce output."""
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_for_session(conventions)
        assert text != ""
        assert "<chameleon-conventions>" in text


class TestFormatEchoInheritance:
    def test_echo_includes_base(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_echo(conventions, archetype="model")
        assert "Base: ApplicationRecord" in text

    def test_echo_no_base_below_threshold(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.50,
            "sample_size": 117,
        }
        text = format_conventions_echo(conventions, archetype="model")
        assert "Base:" not in text

    def test_echo_wrong_archetype_does_not_leak_base(self):
        # `Base:` is derived STRICTLY from the edited archetype's own inheritance
        # entry. A `next(iter(...values()))` fallback used to print the model's
        # base on a controller edit — a self-contradictory injected falsehood (the
        # same block would also carry the controller's own base once present). No
        # inheritance entry for the edited archetype => no `Base:` line.
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_echo(conventions, archetype="controller")
        assert "ApplicationRecord" not in text
        assert "Base:" not in text
        # The edited archetype IS honored when it has its own entry.
        conventions["conventions"]["inheritance"]["controller"] = {
            "dominant_base": "ApplicationController",
            "frequency": 0.9,
            "sample_size": 40,
        }
        text2 = format_conventions_echo(conventions, archetype="controller")
        assert "Base: ApplicationController" in text2
        assert "ApplicationRecord" not in text2


class TestDirectoryListing:
    def test_lists_sibling_files(self, tmp_path):
        from chameleon_mcp.conventions import format_directory_listing

        (tmp_path / "useDebounce.ts").write_text("export const useDebounce = () => {};")
        (tmp_path / "useToggle.ts").write_text("export const useToggle = () => {};")
        (tmp_path / "useConfig.ts").write_text("export const useConfig = () => {};")
        target = str(tmp_path / "useNew.ts")
        result = format_directory_listing(target)
        assert "useDebounce.ts" in result
        assert "useToggle.ts" in result
        assert "useConfig.ts" in result
        assert "check before creating" in result.lower() or "nearby" in result.lower()

    def test_excludes_self(self, tmp_path):
        from chameleon_mcp.conventions import format_directory_listing

        (tmp_path / "useDebounce.ts").write_text("x")
        (tmp_path / "target.ts").write_text("x")
        result = format_directory_listing(str(tmp_path / "target.ts"))
        assert "target.ts" not in result
        assert "useDebounce.ts" in result

    def test_scrubs_control_chars_from_sibling_names(self, tmp_path):
        # A source filename never legitimately holds a control byte. A hostile
        # sibling whose name carries a newline / CR / tab must not split the
        # single-line "Nearby:" listing; the bytes are scrubbed for display while
        # the file itself is still listed (name minus the control bytes).
        from chameleon_mcp.conventions import format_directory_listing

        (tmp_path / "normal.ts").write_text("x")
        (tmp_path / "with\nnewline.ts").write_text("x")
        (tmp_path / "with\ttab.ts").write_text("x")
        result = format_directory_listing(str(tmp_path / "target.ts"))
        assert "\n" not in result and "\t" not in result  # listing stays one line
        assert "normal.ts" in result
        assert "withnewline.ts" in result and "withtab.ts" in result

    def test_empty_for_nonexistent_dir(self):
        from chameleon_mcp.conventions import format_directory_listing

        result = format_directory_listing("/nonexistent/path/file.ts")
        assert result == ""

    def test_empty_for_no_siblings(self, tmp_path):
        from chameleon_mcp.conventions import format_directory_listing

        (tmp_path / "only.ts").write_text("x")
        result = format_directory_listing(str(tmp_path / "only.ts"))
        assert result == ""

    def test_caps_at_max(self, tmp_path):
        from chameleon_mcp.conventions import format_directory_listing

        for i in range(25):
            (tmp_path / f"file{i:02d}.ts").write_text("x")
        result = format_directory_listing(str(tmp_path / "target.ts"), max_files=10)
        assert result.count(".ts") <= 10

    def test_filters_non_source_files(self, tmp_path):
        from chameleon_mcp.conventions import format_directory_listing

        (tmp_path / "component.tsx").write_text("x")
        (tmp_path / "readme.md").write_text("x")
        (tmp_path / "package.json").write_text("x")
        result = format_directory_listing(str(tmp_path / "new.tsx"))
        assert "component.tsx" in result
        assert "readme.md" not in result
        assert "package.json" not in result

    def test_none_file_path(self):
        from chameleon_mcp.conventions import format_directory_listing

        assert format_directory_listing(None) == ""


from chameleon_mcp.principles import generate_principles  # noqa: E402


class TestGeneratePrinciplesProtocol:
    def test_universal_lines_always_present(self):
        out = generate_principles(conventions={}, archetypes={})
        assert "## anti-hallucination protocol" in out
        assert "Don't invent symbols" in out
        assert "canonical witness" in out

    def test_key_exports_line_gated_on_data(self):
        without = generate_principles(conventions={"conventions": {}}, archetypes={})
        assert "Check before creating" not in without
        with_exports = generate_principles(
            conventions={"conventions": {"key_exports": {"service": ["formatDate"]}}},
            archetypes={},
        )
        assert "Check before creating" in with_exports

    def test_known_bases_line_gated_on_ruby_inheritance(self):
        without = generate_principles(conventions={"conventions": {}}, archetypes={})
        assert "Inherit only from bases" not in without
        with_bases = generate_principles(
            conventions={
                "conventions": {
                    "inheritance": {
                        "model": {
                            "dominant_base": "ApplicationRecord",
                            "known_bases": ["ApplicationRecord"],
                        }
                    }
                }
            },
            archetypes={},
        )
        assert "Inherit only from bases" in with_bases

    def test_protocol_lines_are_bullets_not_numbered(self):
        out = generate_principles(conventions={}, archetypes={})
        protocol_idx = out.index("## anti-hallucination protocol")
        tail = out[protocol_idx:]
        for line in tail.splitlines():
            if line.strip() and line[0].isdigit():
                raise AssertionError(f"protocol line is numbered: {line!r}")


class TestSessionProtocolBlock:
    def test_protocol_block_rendered(self):
        from chameleon_mcp.conventions import empty_conventions, format_conventions_for_session

        principles = (
            "# principles\n\n1. Match directory granularity.\n\n"
            "## anti-hallucination protocol\n\n"
            "- Don't invent symbols, imports, file paths, config keys, or APIs.\n"
            "- Match the canonical witness's real shape.\n"
        )
        out = format_conventions_for_session(
            empty_conventions(generation=1), principles_text=principles
        )
        assert "ANTI-HALLUCINATION PROTOCOL:" in out
        assert "Don't invent symbols" in out
        assert "PRINCIPLES:" in out
        assert "Match directory granularity" in out

    def test_no_protocol_section_no_block(self):
        from chameleon_mcp.conventions import empty_conventions, format_conventions_for_session

        principles = "# principles\n\n1. Only a numbered principle.\n"
        out = format_conventions_for_session(
            empty_conventions(generation=1), principles_text=principles
        )
        assert "ANTI-HALLUCINATION PROTOCOL:" not in out
        assert "PRINCIPLES:" in out


class TestEchoProtocolReminder:
    def test_echo_carries_reminder(self):
        from chameleon_mcp.conventions import empty_conventions, format_conventions_echo

        out = format_conventions_echo(empty_conventions(generation=1), archetype="service")
        assert "Verify symbols/imports/paths exist" in out

    def test_reminder_present_even_without_principles(self):
        from chameleon_mcp.conventions import empty_conventions, format_conventions_echo

        out = format_conventions_echo(
            empty_conventions(generation=1), archetype="service", principles_text=""
        )
        assert "Verify symbols/imports/paths exist" in out


def test_merge_taught_competing_preserves_across_rederive():
    from chameleon_mcp.conventions import merge_taught_competing

    prior = {
        "conventions": {
            "imports": {
                "component": {
                    "preferred": [{"module": "react"}],
                    "competing": [{"over": "moment", "preferred": "date-fns"}],
                }
            }
        }
    }
    # A freshly derived profile only has the derived `preferred`, no competing.
    new = {
        "conventions": {
            "imports": {"component": {"preferred": [{"module": "react"}], "competing": []}}
        }
    }
    merge_taught_competing(prior, new)
    comp = new["conventions"]["imports"]["component"]["competing"]
    assert {"over": "moment", "preferred": "date-fns"} in comp


def test_merge_taught_competing_dedupes_and_handles_missing_archetype():
    from chameleon_mcp.conventions import merge_taught_competing

    prior = {
        "conventions": {
            "imports": {
                "gone-archetype": {"competing": [{"over": "x", "preferred": "y"}]},
                "kept": {"competing": [{"over": "a", "preferred": "b"}]},
            }
        }
    }
    new = {
        "conventions": {
            "imports": {"kept": {"preferred": [], "competing": [{"over": "a", "preferred": "b"}]}}
        }
    }
    merge_taught_competing(prior, new)
    # dedupe: 'a'->'b' not duplicated
    assert new["conventions"]["imports"]["kept"]["competing"] == [{"over": "a", "preferred": "b"}]
    # orphaned archetype's taught rule is still carried (created)
    assert new["conventions"]["imports"]["gone-archetype"]["competing"] == [
        {"over": "x", "preferred": "y"}
    ]


def test_merge_taught_competing_noop_when_no_prior():
    from chameleon_mcp.conventions import merge_taught_competing

    new = {"conventions": {"imports": {"c": {"preferred": [], "competing": []}}}}
    merge_taught_competing({}, new)
    assert new["conventions"]["imports"]["c"]["competing"] == []
