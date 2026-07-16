"""Unit tests for per-archetype body-shape norms.

Covers the conventions.py aggregation. The dump-side
function-scope extraction is exercised by the extractor golden tests (which run
only when node/ruby are present); these tests stay parser-free by building
``function_scopes`` directly in ``ParsedFile.extras``.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp._thresholds import threshold
from chameleon_mcp.conventions import (
    extract_all_conventions,
    extract_body_shape_conventions,
    format_conventions_for_session,
)
from chameleon_mcp.extractors._base import ParsedFile


def _file_with_scopes(name: str, scopes: list[dict]) -> ParsedFile:
    return ParsedFile(
        path=Path(name),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras={"function_scopes": scopes} if scopes else {},
    )


def _flat_scope(span: int = 10) -> dict:
    return {
        "start_line": 1,
        "end_line": span,
        "line_span": span,
        "max_depth": 1,
        "branch_count": 1,
        "param_count": 2,
    }


def _pool(n: int, scope: dict | None = None) -> list[ParsedFile]:
    scope = scope or _flat_scope()
    return [_file_with_scopes(f"f{i}.ts", [dict(scope)]) for i in range(n)]


class TestExtractBodyShape:
    def test_below_function_pool_gate_returns_empty(self):
        min_fns = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
        assert extract_body_shape_conventions(_pool(min_fns - 1)) == {}

    def test_at_function_pool_gate_produces_norm(self):
        min_fns = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
        norm = extract_body_shape_conventions(_pool(min_fns))
        assert norm["function_count"] == min_fns
        assert set(norm["dimensions"]) == {
            "branch_count",
            "max_depth",
            "line_span",
            "param_count",
        }
        # Every witness is identical, so median == p90 == the constant value.
        assert norm["dimensions"]["line_span"] == {"median": 10, "p90": 10}

    def test_files_without_scopes_do_not_count_toward_pool(self):
        min_fns = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
        files = _pool(min_fns - 1) + [_file_with_scopes("empty.ts", [])]
        assert extract_body_shape_conventions(files) == {}

    def test_percentile_separates_median_from_p90(self):
        # 15 short functions + 5 long ones: nearest-rank p90 (rank 18 of 20)
        # lands in the long tail, above the median.
        files = _pool(15, _flat_scope(span=10))
        files += _pool(5, _flat_scope(span=100))
        norm = extract_body_shape_conventions(files)
        line = norm["dimensions"]["line_span"]
        assert line["median"] == 10
        assert line["p90"] > line["median"]


class TestBodyShapeIntegration:
    def test_extract_all_conventions_includes_body_shape(self):
        min_fns = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
        result = extract_all_conventions(
            files_by_archetype={"service": _pool(min_fns)},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        assert "service" in result["conventions"]["body_shape"]

    def test_session_block_renders_shape_section(self):
        min_fns = int(threshold("BODY_SHAPE_MIN_FUNCTIONS"))
        conv = extract_all_conventions(
            files_by_archetype={"service": _pool(min_fns)},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        text = format_conventions_for_session(conv)
        assert "SHAPE (advisory)" in text
        assert "service:" in text

    def test_body_shape_norm_is_not_block_eligible(self):
        # The advisory routing is enforced by keeping the rule out of the
        # block-eligible set; this pins that contract so a later edit can't
        # quietly promote a noisy line-counter to a hard block.
        from chameleon_mcp.violation_class import BLOCK_ELIGIBLE_RULES

        assert "complexity-outlier" not in BLOCK_ELIGIBLE_RULES
        assert "body-shape" not in BLOCK_ELIGIBLE_RULES
