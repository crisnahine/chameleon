"""Unit tests for the per-archetype callable-signature consensus.

Covers the conventions.py aggregation that distills the declaration headers the
AST dumps emit into a per-archetype contract. The dump-side extraction is
exercised by the extractor golden tests; these tests stay parser-free by
building ``callable_signatures`` directly in ``ParsedFile.extras``.
"""

from __future__ import annotations

from pathlib import Path

from chameleon_mcp.conventions import (
    extract_all_conventions,
    extract_callable_signatures,
)
from chameleon_mcp.extractors._base import ParsedFile


def _param(name: str, optional: bool = False, kind: str = "positional") -> dict:
    return {"name": name, "optional": optional, "kind": kind}


def _file_with_signatures(name: str, signatures: list[dict]) -> ParsedFile:
    return ParsedFile(
        path=Path(name),
        content_first_200_bytes="",
        top_level_node_kinds=(),
        default_export_kind=None,
        named_export_count=0,
        import_specifiers=(),
        has_jsx=False,
        extras={"callable_signatures": signatures} if signatures else {},
    )


def _sig(name: str, params: list[dict], **extra) -> dict:
    base = {"name": name, "kind": "function", "params": params, "is_default_export": False}
    base.update(extra)
    return base


class TestExtractCallableSignatures:
    def test_empty_when_no_extras(self):
        files = [_file_with_signatures(f"f{i}.ts", []) for i in range(5)]
        assert extract_callable_signatures(files) == {}

    def test_name_below_min_files_dropped(self):
        # A name appearing in only one file is an instance, not a convention.
        files = [
            _file_with_signatures("a.ts", [_sig("solo", [_param("x")])]),
            _file_with_signatures("b.ts", []),
        ]
        assert extract_callable_signatures(files) == {}

    def test_shared_name_recorded_with_consensus_shape(self):
        files = [
            _file_with_signatures(
                "a.ts", [_sig("render", [_param("serializer"), _param("status", optional=True)])]
            ),
            _file_with_signatures(
                "b.ts", [_sig("render", [_param("serializer"), _param("status", optional=True)])]
            ),
            _file_with_signatures(
                "c.ts", [_sig("render", [_param("serializer"), _param("status", optional=True)])]
            ),
        ]
        out = extract_callable_signatures(files)
        sig = out["signatures"]["render"]
        assert sig["file_count"] == 3
        assert [p["name"] for p in sig["params"]] == ["serializer", "status"]
        assert sig["params"][1]["optional"] is True
        assert sig["agreement"] == 3

    def test_dominant_arity_wins_over_outlier(self):
        # Two files share a 2-arg shape; one file has a divergent 3-arg shape.
        # The consensus is the 2-arg shape, with agreement reflecting the split.
        two = [_param("a"), _param("b")]
        three = [_param("a"), _param("b"), _param("c")]
        files = [
            _file_with_signatures("a.ts", [_sig("fn", two)]),
            _file_with_signatures("b.ts", [_sig("fn", list(two))]),
            _file_with_signatures("c.ts", [_sig("fn", three)]),
        ]
        out = extract_callable_signatures(files)
        sig = out["signatures"]["fn"]
        assert len(sig["params"]) == 2
        assert sig["agreement"] == 2

    def test_overrides_base_only_when_base_in_repo(self):
        # The base class is itself defined in the corpus -> recorded.
        files = [
            _file_with_signatures(
                "a.rb",
                [
                    _sig(
                        "perform",
                        [_param("id")],
                        kind="method",
                        enclosing_class="FooJob",
                        base_class="ApplicationJob",
                    )
                ],
            ),
            _file_with_signatures(
                "b.rb",
                [
                    _sig(
                        "perform",
                        [_param("id")],
                        kind="method",
                        enclosing_class="BarJob",
                        base_class="ApplicationJob",
                    )
                ],
            ),
            # ApplicationJob is defined in-repo -> the base is captured.
            _file_with_signatures(
                "base.rb",
                [
                    _sig(
                        "setup",
                        [],
                        kind="method",
                        enclosing_class="ApplicationJob",
                        base_class="ActiveJob::Base",
                    )
                ],
            ),
        ]
        out = extract_callable_signatures(files)
        assert out["signatures"]["perform"]["overrides_base"] == "ApplicationJob"

    def test_overrides_base_omitted_for_framework_base(self):
        # The base class is NOT defined anywhere in the corpus (framework base);
        # the override hint must not be asserted.
        files = [
            _file_with_signatures(
                "a.rb",
                [
                    _sig(
                        "call",
                        [_param("x")],
                        kind="method",
                        enclosing_class="FooService",
                        base_class="SomeGem::Base",
                    )
                ],
            ),
            _file_with_signatures(
                "b.rb",
                [
                    _sig(
                        "call",
                        [_param("x")],
                        kind="method",
                        enclosing_class="BarService",
                        base_class="SomeGem::Base",
                    )
                ],
            ),
        ]
        out = extract_callable_signatures(files)
        assert "overrides_base" not in out["signatures"]["call"]

    def test_name_cap_keeps_most_frequent(self):
        import os

        os.environ["CHAMELEON_CALLABLE_SIGNATURE_MAX_NAMES"] = "1"
        try:
            files = [
                _file_with_signatures("a.ts", [_sig("common", [_param("x")]), _sig("rare", [])]),
                _file_with_signatures("b.ts", [_sig("common", [_param("x")]), _sig("rare2", [])]),
                _file_with_signatures("c.ts", [_sig("common", [_param("x")])]),
            ]
            out = extract_callable_signatures(files)
            assert list(out["signatures"].keys()) == ["common"]
        finally:
            del os.environ["CHAMELEON_CALLABLE_SIGNATURE_MAX_NAMES"]


class TestCallableSignaturesInAllConventions:
    def test_section_populated_per_archetype(self):
        files = [
            _file_with_signatures("a.ts", [_sig("handle", [_param("req"), _param("res")])]),
            _file_with_signatures("b.ts", [_sig("handle", [_param("req"), _param("res")])]),
        ]
        conv = extract_all_conventions(
            files_by_archetype={"controller": files},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        section = conv["conventions"]["callable_signatures"]
        assert "controller" in section
        assert "handle" in section["controller"]["signatures"]

    def test_section_empty_when_nothing_shared(self):
        files = [_file_with_signatures("a.ts", [_sig("x", [])])]
        conv = extract_all_conventions(
            files_by_archetype={"thing": files},
            declarations_by_archetype={},
            generation=1,
            language="typescript",
        )
        assert conv["conventions"]["callable_signatures"] == {}
