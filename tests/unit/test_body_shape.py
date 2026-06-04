"""Unit tests for per-archetype body-shape norms.

Covers the conventions.py aggregation + outlier helper. The dump-side
function-scope extraction is exercised by the extractor golden tests (which run
only when node/ruby are present); these tests stay parser-free by building
``function_scopes`` directly in ``ParsedFile.extras``.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp._thresholds import threshold
from chameleon_mcp.conventions import (
    body_shape_outliers,
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


class TestBodyShapeOutliers:
    def _norm(self) -> dict:
        return {
            "dimensions": {
                "branch_count": {"median": 3, "p90": 6},
                "max_depth": {"median": 2, "p90": 3},
                "line_span": {"median": 20, "p90": 40},
                "param_count": {"median": 2, "p90": 4},
            }
        }

    def test_long_but_flat_function_is_not_flagged(self):
        # A 300-line literal table / JSX tree: zero branches, zero nesting.
        flat = [
            {
                "start_line": 1,
                "end_line": 300,
                "line_span": 300,
                "max_depth": 0,
                "branch_count": 0,
                "param_count": 1,
            }
        ]
        assert body_shape_outliers(flat, self._norm()) == []

    def test_long_line_span_alone_is_not_an_outlier(self):
        # Over the line-span p90 but structurally within norms -> no finding,
        # because line span is secondary and never fires on its own.
        long_only = [
            {
                "start_line": 1,
                "end_line": 70,
                "line_span": 70,
                "max_depth": 2,
                "branch_count": 4,
                "param_count": 2,
            }
        ]
        assert body_shape_outliers(long_only, self._norm()) == []

    def test_branchy_nested_function_is_flagged(self):
        complex_fn = [
            {
                "start_line": 1,
                "end_line": 80,
                "line_span": 80,
                "max_depth": 6,
                "branch_count": 14,
                "param_count": 3,
            }
        ]
        findings = body_shape_outliers(complex_fn, self._norm())
        assert len(findings) == 1
        exceeded = {e["dimension"] for e in findings[0]["exceeded"]}
        assert exceeded == {"branch_count", "max_depth"}
        # Line span over p90 rides along as supporting context, not a trigger.
        ctx = {c["dimension"] for c in findings[0]["context"]}
        assert "line_span" in ctx

    def test_flat_archetype_floor_avoids_infinite_outlier(self):
        # A branch-free archetype has p90 == 0; a single decision point must not
        # read as an outlier, but a clearly branchy function still does.
        flat_norm = {
            "dimensions": {
                "branch_count": {"median": 0, "p90": 0},
                "max_depth": {"median": 0, "p90": 0},
                "line_span": {"median": 5, "p90": 8},
                "param_count": {"median": 1, "p90": 2},
            }
        }
        one_branch = [
            {
                "start_line": 1,
                "end_line": 6,
                "line_span": 6,
                "max_depth": 1,
                "branch_count": 1,
                "param_count": 1,
            }
        ]
        assert body_shape_outliers(one_branch, flat_norm) == []
        many_branch = [
            {
                "start_line": 1,
                "end_line": 40,
                "line_span": 40,
                "max_depth": 4,
                "branch_count": 10,
                "param_count": 1,
            }
        ]
        assert len(body_shape_outliers(many_branch, flat_norm)) == 1

    def test_empty_or_missing_norm_returns_empty(self):
        scope = [_flat_scope()]
        assert body_shape_outliers(scope, None) == []
        assert body_shape_outliers(scope, {}) == []
        assert body_shape_outliers([], self._norm()) == []

    def test_malformed_scope_entries_are_skipped(self):
        assert body_shape_outliers([None, "x", 5], self._norm()) == []


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
