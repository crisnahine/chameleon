"""Unit tests for per-archetype import grouping/ordering conventions."""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.conventions import (
    _import_group,
    _import_group_signature,
    extract_all_conventions,
    extract_import_ordering_conventions,
    format_conventions_for_session,
)
from chameleon_mcp.extractors._base import ParsedFile


def _pf(path: str, imports: list[tuple[str, str]]) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=tuple(imports),
        has_jsx=False,
    )


class TestImportGroup:
    def test_bare_package_is_external(self):
        assert _import_group("react") == "external"

    def test_scoped_package_is_external(self):
        assert _import_group("@scope/pkg") == "external"

    def test_dot_relative_is_relative(self):
        assert _import_group("./foo") == "relative"
        assert _import_group("../bar") == "relative"

    def test_alias_roots_are_relative(self):
        assert _import_group("@/components/Button") == "relative"
        assert _import_group("~/lib/util") == "relative"


class TestImportGroupSignature:
    def test_no_imports_returns_none(self):
        assert _import_group_signature(()) is None

    def test_external_then_relative(self):
        sig = _import_group_signature((("react", "named"), ("./x", "default")))
        assert sig == "external-then-relative"

    def test_relative_then_external(self):
        sig = _import_group_signature((("./x", "default"), ("lodash", "named")))
        assert sig == "relative-then-external"

    def test_single_group_collapses(self):
        assert _import_group_signature((("react", "named"), ("lodash", "named"))) == "external"

    def test_interleaved_keeps_runs(self):
        sig = _import_group_signature((("react", "named"), ("./x", "default"), ("lodash", "named")))
        assert sig == "external-then-relative-then-external"


class TestExtractImportOrdering:
    def test_below_sample_floor_returns_empty(self):
        files = [_pf(f"f{i}.ts", [("react", "named"), ("./x", "default")]) for i in range(5)]
        assert extract_import_ordering_conventions(files) == {}

    def test_dominant_pattern_recorded(self):
        files = [_pf(f"f{i}.ts", [("react", "named"), ("./x", "default")]) for i in range(12)]
        result = extract_import_ordering_conventions(files)
        assert result["pattern"] == "external-then-relative"
        assert result["frequency"] == 1.0
        assert result["matching"] == 12
        assert result["total"] == 12

    def test_files_without_imports_excluded_from_vote(self):
        # 11 ordered files + 5 import-less files: import-less must not dilute.
        files = [_pf(f"f{i}.ts", [("react", "named"), ("./x", "default")]) for i in range(11)]
        files += [_pf(f"empty{i}.ts", []) for i in range(5)]
        result = extract_import_ordering_conventions(files)
        assert result["total"] == 11
        assert result["frequency"] == 1.0

    def test_no_dominant_pattern_returns_empty(self):
        # Even split between two patterns: neither clears the 60% floor.
        files = [_pf(f"a{i}.ts", [("react", "named"), ("./x", "default")]) for i in range(6)]
        files += [_pf(f"b{i}.ts", [("./x", "default"), ("react", "named")]) for i in range(6)]
        assert extract_import_ordering_conventions(files) == {}

    def test_wired_into_extract_all(self):
        files = [_pf(f"f{i}.ts", [("react", "named"), ("./x", "default")]) for i in range(12)]
        conv = extract_all_conventions(
            files_by_archetype={"component": files},
            declarations_by_archetype={},
            generation=1,
        )
        assert "component" in conv["conventions"]["import_ordering"]


class TestImportOrderingRendering:
    def test_advisory_line_cites_sibling_count(self):
        conv = {
            "conventions": {
                "import_ordering": {
                    "component": {
                        "pattern": "external-then-relative",
                        "frequency": 0.9,
                        "matching": 18,
                        "total": 20,
                    }
                }
            }
        }
        out = format_conventions_for_session(conv)
        assert "IMPORT ORDERING (advisory):" in out
        assert "group external imports before relative" in out
        assert "18/20 siblings" in out

    def test_malformed_entry_skipped(self):
        conv = {"conventions": {"import_ordering": {"x": "not-a-dict"}}}
        # No other conventions: the block should be empty, not crash.
        assert format_conventions_for_session(conv) == ""
