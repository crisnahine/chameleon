"""Unit tests for chameleon_mcp.conventions — schema, serialization, extraction."""
from __future__ import annotations
import json
from pathlib import Path
from chameleon_mcp.conventions import (
    CONVENTIONS_SCHEMA_VERSION,
    empty_conventions,
    extract_import_conventions,
    serialize_conventions,
)
from chameleon_mcp.extractors._base import ParsedFile


def _make_parsed_file(path: str, imports: list[tuple[str, str]]) -> ParsedFile:
    return ParsedFile(
        path=Path(path),
        content_first_200_bytes="",
        top_level_node_kinds=(),
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
