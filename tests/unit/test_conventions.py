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


def _make_parsed_file(path: str, imports: list[tuple[str, str]], *, top_level_kinds: tuple[str, ...] = ()) -> ParsedFile:
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
            "preferred": [{"module": "useCustomQuery", "source": "@/hooks", "frequency": 47, "total": 52}],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        c["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = serialize_conventions(c)
        parsed = json.loads(text)
        assert parsed["conventions"]["imports"]["model"]["preferred"][0]["module"] == "useCustomQuery"
        assert parsed["conventions"]["naming"]["component"]["interface_prefix"]["consistency"] == 0.999


class TestImportFrequencyExtractor:
    def test_detects_preferred_import(self):
        files = [_make_parsed_file(f"src/hooks/use{i}.ts", [("@/lib/api", "named")]) for i in range(15)]
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
            "IUserProps", "IChartData", "IListingData", "IApiResponse",
            "ITableRow", "IFormValues", "IModalProps", "ISearchParams",
            "IFilterState", "IConfig",
        ]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result["interface_prefix"]["pattern"] == "I"
        assert result["interface_prefix"]["consistency"] >= 0.95

    def test_no_prefix_when_inconsistent(self):
        declarations = ["IFoo", "Bar", "IBaz", "Qux", "Hello"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert "interface_prefix" not in result or result.get("interface_prefix", {}).get("consistency", 0) < 0.6

    def test_detects_type_t_prefix(self):
        declarations = ["TTheme", "TRoute", "TConfig", "TState", "TProps", "TData"]
        result = extract_naming_conventions(declarations={"type": declarations})
        assert result["type_prefix"]["pattern"] == "T"

    def test_skips_below_min_sample(self):
        declarations = ["IFoo", "IBar"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert result == {}

    def test_no_prefix_convention_for_bulletproof_style(self):
        declarations = ["UserProps", "ChartData", "ListingData", "ApiResponse", "TableRow", "FormValues"]
        result = extract_naming_conventions(declarations={"interface": declarations})
        assert "interface_prefix" not in result


class TestExtractAllConventions:
    def test_produces_conventions_dict(self):
        files_by_archetype = {
            "component": [
                _make_parsed_file(f"src/c{i}.tsx", [("react", "namespace"), ("@/hooks/useCustomQuery", "named")])
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
            "tiny": [
                _make_parsed_file(f"src/t{i}.ts", [("lodash", "named")])
                for i in range(3)
            ],
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
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        text = format_conventions_for_session(conventions)
        assert "useCustomQuery" in text
        assert "not useQuery" in text
        assert "Follow these on every edit" in text

    def test_formats_naming_enforced(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["naming"]["component"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
        }
        text = format_conventions_for_session(conventions)
        assert "I" in text
        assert "interface" in text.lower()

    def test_empty_conventions_with_principles(self):
        conventions = empty_conventions(generation=1)
        text = format_conventions_for_session(conventions, principles_text="1. Search the codebase for existing utilities.")
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
        text = format_conventions_for_session(conventions, principles_text="1. Match testing granularity of sibling files.")
        assert "NAMING" not in text
        assert "PRINCIPLES:" in text


class TestFormatConventionsEcho:
    def test_compact_echo(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["hook"] = {
            "preferred": [],
            "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
        }
        conventions["conventions"]["naming"]["hook"] = {
            "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 100},
        }
        text = format_conventions_echo(conventions, archetype="hook")
        assert "useCustomQuery" in text
        assert "I-prefix" in text
        assert len(text) < 200

    def test_empty_returns_empty(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        text = format_conventions_echo(conventions, archetype="hook")
        assert text == ""

    def test_archetype_not_in_conventions_falls_back(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["imports"]["other"] = {
            "preferred": [],
            "competing": [{"preferred": "X", "over": "Y", "preferred_count": 10, "over_count": 0}],
        }
        text = format_conventions_echo(conventions, archetype="hook")
        assert "X" in text


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
            files.append(_make_ts_file(tmp_path, f"filler{i}.ts", f"export const filler{i} = {i};\n"))

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
            files.append(_make_ruby_file(tmp_path, f"m{i}.rb", f"class Model{i} < ApplicationRecord\nend\n"))

        result = extract_key_exports(files, language="ruby")
        assert "User" in result
        assert "Listing" in result

    def test_skips_below_sample_size(self, tmp_path):
        files = [_make_ts_file(tmp_path, "one.ts", "export const foo = 1;\n")]
        result = extract_key_exports(files, language="typescript")
        assert result == []

    def test_deduplicates_and_limits(self, tmp_path):
        files = []
        for i in range(15):
            exports = "\n".join(f"export const item{j} = {j};" for j in range(30))
            files.append(_make_ts_file(tmp_path, f"f{i}.ts", exports))
        result = extract_key_exports(files, language="typescript")
        assert len(result) <= 20


class TestFormatSessionReuse:
    def test_reuse_section_in_session(self):
        conventions = empty_conventions(generation=1)
        conventions["conventions"]["key_exports"]["hook"] = [
            "useDebounce", "useToggle", "formatCurrency",
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
            "useDebounce", "useToggle",
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
            files.append(_make_ruby_file(tmp_path, f"m{i}.rb", f"class Model{i} < ApplicationRecord\n  validates :name\nend\n"))
        result = extract_inheritance_conventions(files)
        assert result["dominant_base"] == "ApplicationRecord"
        assert result["frequency"] >= 0.9

    def test_detects_include_mixin(self, tmp_path):
        files = []
        for i in range(12):
            files.append(_make_ruby_file(tmp_path, f"w{i}.rb", f"class Worker{i}\n  include Sidekiq::Worker\nend\n"))
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


class TestMethodCallExtractor:
    def test_detects_common_dsl_calls(self, tmp_path):
        files = []
        for i in range(15):
            files.append(_make_ruby_file(tmp_path, f"m{i}.rb",
                f"class M{i} < ApplicationRecord\n  validates :name\n  belongs_to :user\n  scope :active, -> {{}}\nend\n"))
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

    def test_echo_wrong_archetype_falls_back_to_base(self):
        from chameleon_mcp.conventions import format_conventions_echo

        conventions = empty_conventions(generation=1)
        conventions["conventions"]["inheritance"]["model"] = {
            "dominant_base": "ApplicationRecord",
            "frequency": 0.96,
            "sample_size": 117,
        }
        text = format_conventions_echo(conventions, archetype="controller")
        assert "Base: ApplicationRecord" in text


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
