"""Unit tests for chameleon_mcp.lint_engine — pure functions, no I/O."""

from __future__ import annotations

from chameleon_mcp.lint_engine import (
    DimensionSnapshot,
    _coarse_normalize,
    _extract_ruby,
    _extract_typescript,
    _fold_string_concat,
    _top_level_kinds_match,
    canonical_confidence,
    detect_language,
    lint,
    lint_conventions,
)


class TestDetectLanguage:
    def test_ts(self):
        assert detect_language("src/app.ts") == "typescript"

    def test_tsx(self):
        assert detect_language("components/Button.tsx") == "typescript"

    def test_js(self):
        assert detect_language("index.js") == "typescript"

    def test_jsx(self):
        assert detect_language("App.jsx") == "typescript"

    def test_mjs(self):
        assert detect_language("config.mjs") == "typescript"

    def test_cjs(self):
        assert detect_language("config.cjs") == "typescript"

    def test_rb(self):
        assert detect_language("app/models/user.rb") == "ruby"

    def test_md_unsupported(self):
        assert detect_language("README.md") is None

    def test_py_supported(self):
        assert detect_language("main.py") == "python"

    def test_pyi_supported(self):
        assert detect_language("stubs.pyi") == "python"

    def test_none_path(self):
        assert detect_language(None) is None

    def test_empty_string(self):
        assert detect_language("") is None

    def test_case_insensitive(self):
        assert detect_language("FOO.TS") == "typescript"
        assert detect_language("bar.RB") == "ruby"


class TestExtractTsDefaultExport:
    def test_class_default(self):
        code = "export default class Foo {}\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "ClassDeclaration"

    def test_function_default(self):
        code = "export default function handler() {}\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "FunctionDeclaration"

    def test_async_function_default(self):
        code = "export default async function handler() {}\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "FunctionDeclaration"

    def test_arrow_default(self):
        code = "export default (props) => { return null; }\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "ArrowFunction"

    def test_object_default(self):
        code = "export default { key: 'value' };\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "ObjectLiteralExpression"

    def test_array_default(self):
        code = "export default [1, 2, 3];\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "ArrayLiteralExpression"

    def test_identifier_default(self):
        code = "const x = 1;\nexport default x;\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind == "Identifier"

    def test_no_default(self):
        code = "export const foo = 1;\n"
        snap = _extract_typescript(code)
        assert snap.default_export_kind is None


class TestExtractTsNamedExports:
    def test_zero(self):
        code = "const x = 1;\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 0

    def test_single_const(self):
        code = "export const foo = 1;\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 1

    def test_multiple_kinds(self):
        code = (
            "export const a = 1;\n"
            "export function b() {}\n"
            "export class C {}\n"
            "export interface D {}\n"
            "export type E = string;\n"
            "export enum F { X }\n"
        )
        snap = _extract_typescript(code)
        assert snap.named_export_count == 6

    def test_export_list(self):
        code = "const a = 1;\nconst b = 2;\nexport { a, b };\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 2

    def test_export_list_with_alias(self):
        code = "const x = 1;\nexport { x as y };\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 1

    def test_deduplicates(self):
        code = "export const foo = 1;\nexport { foo };\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 1


class TestExtractTsJsx:
    def test_closing_tag(self):
        code = "function App() { return <div></div>; }\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is True

    def test_self_closing_tag(self):
        code = "function App() { return <Input />; }\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is True

    def test_fragment(self):
        code = "function App() { return <>hello</>; }\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is True

    def test_no_jsx(self):
        code = "export function add(a: number, b: number) { return a + b; }\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is False

    def test_jsx_in_string_stripped(self):
        code = 'const html = "</div>";\n'
        snap = _extract_typescript(code)
        assert snap.jsx_present is False

    def test_jsx_in_comment_stripped(self):
        code = "// return <div></div>;\nconst x = 1;\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is False

    def test_jsx_in_block_comment_stripped(self):
        code = "/* <Component /> */\nconst x = 1;\n"
        snap = _extract_typescript(code)
        assert snap.jsx_present is False


class TestExtractTsContentSignal:
    def test_use_client(self):
        code = '"use client";\nimport React from "react";\n'
        snap = _extract_typescript(code)
        assert snap.content_signal == "use_client"

    def test_use_server(self):
        code = "'use server';\nexport async function action() {}\n"
        snap = _extract_typescript(code)
        assert snap.content_signal == "use_server"

    def test_none_signal(self):
        code = "export const x = 1;\n"
        snap = _extract_typescript(code)
        assert snap.content_signal is None


class TestExtractRubyTopLevel:
    def test_class_at_column_zero(self):
        code = "class User\nend\n"
        snap = _extract_ruby(code)
        assert any(k.startswith("ClassNode") for k in snap.top_level_node_kinds)

    def test_module_at_column_zero(self):
        code = "module Helpers\nend\n"
        snap = _extract_ruby(code)
        assert "ModuleNode" in snap.top_level_node_kinds

    def test_indented_class_not_top_level(self):
        code = "  class Nested\n  end\n"
        snap = _extract_ruby(code)
        column0_kinds = [
            k
            for k in snap.top_level_node_kinds
            if not k.startswith("IncludeCall:") and not k.startswith("DslCall:")
        ]
        assert column0_kinds == []

    def test_def_at_column_zero(self):
        code = "def helper\nend\n"
        snap = _extract_ruby(code)
        assert "DefNode" in snap.top_level_node_kinds


class TestExtractRubySuperclass:
    def test_application_record(self):
        code = "class User < ApplicationRecord\nend\n"
        snap = _extract_ruby(code)
        assert "ClassNode:ApplicationRecord" in snap.top_level_node_kinds

    def test_application_controller(self):
        code = "class UsersController < ApplicationController\nend\n"
        snap = _extract_ruby(code)
        assert "ClassNode:ApplicationController" in snap.top_level_node_kinds

    def test_no_superclass(self):
        code = "class PlainRuby\nend\n"
        snap = _extract_ruby(code)
        assert "ClassNode" in snap.top_level_node_kinds
        assert not any(k.startswith("ClassNode:") for k in snap.top_level_node_kinds)

    def test_superclass_resolved_per_class(self):
        code = "class Plain\nend\nclass Account < ApplicationRecord\nend\n"
        snap = _extract_ruby(code)
        assert "ClassNode" in snap.top_level_node_kinds
        assert "ClassNode:ApplicationRecord" in snap.top_level_node_kinds
        assert "ClassNode:Plain" not in snap.top_level_node_kinds


class TestExtractRubyDsl:
    def test_validates(self):
        code = "class User < ApplicationRecord\n  validates :name, presence: true\nend\n"
        snap = _extract_ruby(code)
        assert "DslCall:validates" in snap.top_level_node_kinds

    def test_has_many(self):
        code = "class User < ApplicationRecord\n  has_many :posts\nend\n"
        snap = _extract_ruby(code)
        assert "DslCall:has_many" in snap.top_level_node_kinds

    def test_belongs_to(self):
        code = "class Post < ApplicationRecord\n  belongs_to :user\nend\n"
        snap = _extract_ruby(code)
        assert "DslCall:belongs_to" in snap.top_level_node_kinds

    def test_before_action(self):
        code = "class FooController < ApplicationController\n  before_action :auth\nend\n"
        snap = _extract_ruby(code)
        assert "DslCall:before_action" in snap.top_level_node_kinds

    def test_scope(self):
        code = "class User < ApplicationRecord\n  scope :active, -> { where(active: true) }\nend\n"
        snap = _extract_ruby(code)
        assert "DslCall:scope" in snap.top_level_node_kinds

    def test_deduplicates_dsl(self):
        code = "class User < ApplicationRecord\n  validates :a\n  validates :b\nend\n"
        snap = _extract_ruby(code)
        dsl_validates = [k for k in snap.top_level_node_kinds if k == "DslCall:validates"]
        assert len(dsl_validates) == 1


class TestLint:
    def test_empty_ast_query_no_violations(self):
        snap = DimensionSnapshot()
        assert lint(snap, {}) == []

    def test_none_ast_query_no_violations(self):
        snap = DimensionSnapshot()
        assert lint(snap, None) == []

    def test_default_export_mismatch(self):
        snap = DimensionSnapshot(default_export_kind="ClassDeclaration")
        query = {"default_export_kind": "FunctionDeclaration"}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "default-export-kind-mismatch"
        assert violations[0].severity == "warning"

    def test_default_export_match_no_violation(self):
        snap = DimensionSnapshot(default_export_kind="FunctionDeclaration")
        query = {"default_export_kind": "FunctionDeclaration"}
        assert lint(snap, query) == []

    def test_jsx_file_has_jsx_archetype_expects_none(self):
        snap = DimensionSnapshot(jsx_present=True)
        query = {"jsx_present": False}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "jsx-presence-mismatch"
        assert violations[0].severity == "error"

    def test_jsx_archetype_expects_jsx_file_missing(self):
        snap = DimensionSnapshot(jsx_present=False)
        query = {"jsx_present": True}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "jsx-presence-mismatch"
        assert violations[0].severity == "warning"

    def test_named_export_bucket_mismatch(self):
        snap = DimensionSnapshot(named_export_count=12)
        query = {"named_export_count_bucket": "0"}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "named-export-count-bucket-mismatch"
        assert violations[0].severity == "info"

    def test_content_signal_mismatch(self):
        snap = DimensionSnapshot(content_signal=None)
        query = {"content_signal": "use_client"}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "content-signal-mismatch"

    def test_top_level_kinds_mismatch(self):
        snap = DimensionSnapshot(top_level_node_kinds=["ImportDeclaration"])
        query = {
            "top_level_node_kinds": [
                "ImportDeclaration",
                "ClassDeclaration",
                "FunctionDeclaration",
                "EnumDeclaration",
            ],
        }
        violations = lint(snap, query)
        assert len(violations) == 1
        assert violations[0].rule == "top-level-node-kinds-mismatch"

    def test_top_level_kinds_mismatch_actual_capped_for_huge_files(self):
        # A file with thousands of top-level statements must not embed an
        # unbounded literal repr of every kind in the violation's `actual`.
        kinds = [f"FunctionDeclaration:{i}" for i in range(5000)]
        snap = DimensionSnapshot(top_level_node_kinds=kinds)
        query = {"top_level_node_kinds": ["ClassDeclaration"]}
        violations = lint(snap, query)
        assert len(violations) == 1
        actual = violations[0].actual
        assert len(actual) < 5_000
        assert "+4950 more (capped at 50)" in actual

    def test_top_level_kinds_cap_honors_thresholds_override(self, monkeypatch):
        # The cap must actually be registered in _thresholds.py's DEFAULTS and
        # read via threshold_int (tunable-thresholds-not-inline-constants) --
        # not a hardcoded fallback that merely LOOKS wired. An unregistered
        # name raises KeyError inside threshold(), which the broad except in
        # _top_level_kinds_repr_cap would silently swallow every call, so this
        # override must actually change the cap or the wiring is dead code.
        monkeypatch.setenv("CHAMELEON_TOP_LEVEL_NODE_KINDS_REPR_CAP", "5")
        kinds = [f"FunctionDeclaration:{i}" for i in range(20)]
        snap = DimensionSnapshot(top_level_node_kinds=kinds)
        query = {"top_level_node_kinds": ["ClassDeclaration"]}
        violations = lint(snap, query)
        assert len(violations) == 1
        assert "+15 more (capped at 5)" in violations[0].actual

    def test_ruby_messages_humanized_no_parser_jargon_or_js_isms(self):
        # Parser node-kind names (ClassNode/ModuleNode) and TS-only "default
        # export" must never leak into a Ruby user's message.
        node_kind = lint(
            DimensionSnapshot(top_level_node_kinds=["ModuleNode"]),
            {"top_level_node_kinds": ["ClassNode", "DslCall:validates"]},
            language="ruby",
        )
        assert len(node_kind) == 1
        assert node_kind[0].rule == "top-level-node-kinds-mismatch"
        msg = node_kind[0].message
        assert "classes" in msg
        assert "ClassNode" not in msg
        assert "default export" not in msg

        # An uncategorized custom DSL macro normalizes to a bare "DslCall" and a
        # mixin to "IncludeCall"; neither may leak raw into a Ruby message.
        dsl = lint(
            DimensionSnapshot(top_level_node_kinds=["ClassNode"]),
            {
                "top_level_node_kinds": [
                    "ClassNode",
                    "DslCall:acts_as_list",
                    "IncludeCall:Comparable",
                ]
            },
            language="ruby",
        )
        dsl_msg = dsl[0].message
        assert "DSL calls" in dsl_msg and "includes" in dsl_msg
        assert "DslCall" not in dsl_msg and "IncludeCall" not in dsl_msg

        default_export = lint(
            DimensionSnapshot(default_export_kind="ModuleNode"),
            {"default_export_kind": "ClassNode"},
            language="ruby",
        )
        assert len(default_export) == 1
        dmsg = default_export[0].message
        assert dmsg == "this archetype's primary construct is a class; this file defines a module"
        assert "default export" not in dmsg
        assert "ClassNode" not in dmsg and "ModuleNode" not in dmsg

    def test_typescript_default_export_keeps_export_wording(self):
        # TS files genuinely have default exports; keep the export framing,
        # but still humanize the kind names.
        violations = lint(
            DimensionSnapshot(default_export_kind="FunctionDeclaration"),
            {"default_export_kind": "ClassDeclaration"},
            language="typescript",
        )
        assert len(violations) == 1
        msg = violations[0].message
        assert "default export" in msg
        assert "ClassDeclaration" not in msg and "FunctionDeclaration" not in msg

    def test_null_ast_query_fields_not_flagged(self):
        snap = DimensionSnapshot(
            default_export_kind="ClassDeclaration",
            jsx_present=True,
            content_signal="use_client",
        )
        query = {
            "default_export_kind": None,
            "jsx_present": None,
            "content_signal": None,
        }
        assert lint(snap, query) == []

    def test_multiple_violations(self):
        snap = DimensionSnapshot(
            default_export_kind="ClassDeclaration",
            jsx_present=True,
            content_signal=None,
        )
        query = {
            "default_export_kind": "FunctionDeclaration",
            "jsx_present": False,
            "content_signal": "use_client",
        }
        violations = lint(snap, query)
        rules = {v.rule for v in violations}
        assert "default-export-kind-mismatch" in rules
        assert "jsx-presence-mismatch" in rules
        assert "content-signal-mismatch" in rules


class TestCanonicalConfidence:
    def test_full_match(self):
        snap = DimensionSnapshot(
            default_export_kind="FunctionDeclaration",
            named_export_count=0,
            jsx_present=True,
            content_signal="use_client",
            top_level_node_kinds=["ImportDeclaration", "FunctionDeclaration"],
        )
        query = {
            "default_export_kind": "FunctionDeclaration",
            "named_export_count_bucket": "0",
            "jsx_present": True,
            "content_signal": "use_client",
            "top_level_node_kinds": ["ImportDeclaration", "FunctionDeclaration"],
        }
        assert canonical_confidence(snap, query) == 1.0

    def test_no_match(self):
        snap = DimensionSnapshot(
            default_export_kind="ClassDeclaration",
            named_export_count=10,
            jsx_present=False,
            content_signal=None,
        )
        query = {
            "default_export_kind": "FunctionDeclaration",
            "named_export_count_bucket": "0",
            "jsx_present": True,
            "content_signal": "use_client",
        }
        assert canonical_confidence(snap, query) == 0.0

    def test_empty_ast_query(self):
        snap = DimensionSnapshot()
        assert canonical_confidence(snap, {}) == 1.0

    def test_none_ast_query(self):
        snap = DimensionSnapshot()
        assert canonical_confidence(snap, None) == 1.0

    def test_partial_match(self):
        snap = DimensionSnapshot(
            default_export_kind="FunctionDeclaration",
            jsx_present=False,
        )
        query = {
            "default_export_kind": "FunctionDeclaration",
            "jsx_present": True,
        }
        assert canonical_confidence(snap, query) == 0.5


class TestFoldStringConcat:
    def test_double_quote_fold(self):
        assert _fold_string_concat('"ab" + "cd"') == '"abcd"'

    def test_single_quote_fold(self):
        # The unified fold re-emits every collapsed literal double-quoted (the
        # downstream secret scan is quote-agnostic); the point is contiguity.
        assert _fold_string_concat("'ab' + 'cd'") == '"abcd"'

    def test_triple_concat(self):
        assert _fold_string_concat('"a" + "b" + "c"') == '"abc"'

    def test_mixed_quotes_fold(self):
        # Cross-quote concat collapses into one well-formed double-quoted
        # literal so a token split across quote styles becomes contiguous.
        assert _fold_string_concat("\"a\" + 'b'") == '"ab"'
        assert _fold_string_concat("'a' + \"b\"") == '"ab"'

    def test_array_join_fold(self):
        # An array of string literals joined with an empty separator collapses;
        # a non-empty separator or a non-literal element does not fold.
        assert _fold_string_concat("['gh', 'p_', 'rest'].join('')") == '"ghp_rest"'
        assert _fold_string_concat("['a', 'b'].join('-')") == "['a', 'b'].join('-')"
        assert _fold_string_concat("['a', key, 'b'].join('')") == "['a', key, 'b'].join('')"

    def test_no_plus_passthrough(self):
        original = "const x = 1;"
        assert _fold_string_concat(original) == original

    def test_max_folds_cap(self):
        result = _fold_string_concat('"a" + "b" + "c"', max_folds=1)
        assert result == '"ab" + "c"'

    def test_whitespace_around_plus(self):
        assert _fold_string_concat('"a"  +  "b"') == '"ab"'


class TestCoarseNormalize:
    def test_dsl_active_record_collapses(self):
        assert _coarse_normalize("DslCall:validates") == "DslCall"

    def test_dsl_action_controller_collapses(self):
        assert _coarse_normalize("DslCall:before_action") == "DslCall"

    def test_dsl_ruby_core_collapses(self):
        assert _coarse_normalize("DslCall:attr_reader") == "DslCall"

    def test_dsl_generic_stays(self):
        assert _coarse_normalize("DslCall:unknown_dsl") == "DslCall"

    def test_class_node_strips_superclass(self):
        assert _coarse_normalize("ClassNode:ApplicationRecord") == "ClassNode"

    def test_include_call_strips_name(self):
        assert _coarse_normalize("IncludeCall:Concern") == "IncludeCall"

    def test_ts_first_statement_to_code_declaration(self):
        assert _coarse_normalize("FirstStatement") == "CodeDeclaration"

    def test_ts_function_declaration_to_code_declaration(self):
        assert _coarse_normalize("FunctionDeclaration") == "CodeDeclaration"

    def test_ts_export_assignment_to_code_declaration(self):
        assert _coarse_normalize("ExportAssignment") == "CodeDeclaration"

    def test_import_declaration_passthrough(self):
        assert _coarse_normalize("ImportDeclaration") == "ImportDeclaration"

    def test_class_declaration_passthrough(self):
        assert _coarse_normalize("ClassDeclaration") == "ClassDeclaration"


class TestTopLevelKindsMatch:
    def test_empty_expected_always_matches(self):
        assert _top_level_kinds_match(["ClassNode"], []) is True

    def test_exact_match(self):
        assert (
            _top_level_kinds_match(
                ["ClassNode", "DslCall:validates"],
                ["ClassNode", "DslCall:validates"],
            )
            is True
        )

    def test_bare_class_fails_two_kind_expected(self):
        """BUG-031: 1 match of 2 coarse kinds must fail (min-2 threshold)."""
        assert (
            _top_level_kinds_match(
                ["ClassNode"],
                ["ClassNode", "DslCall:scope"],
            )
            is False
        )

    def test_bare_const_fails_two_kind_ts_expected(self):
        """BUG-031: TS const x=1 (FirstStatement) vs action with imports."""
        assert (
            _top_level_kinds_match(
                ["FirstStatement"],
                ["FirstStatement", "ImportDeclaration"],
            )
            is False
        )

    def test_coarse_dedup_prevents_inflated_match(self):
        """BUG-031: FirstStatement + ExportAssignment both -> CodeDeclaration.
        A file with just FirstStatement should NOT count as 2 matches."""
        assert (
            _top_level_kinds_match(
                ["FirstStatement"],
                [
                    "ClassDeclaration",
                    "ExportAssignment",
                    "FirstStatement",
                    "ImportDeclaration",
                ],
            )
            is False
        )

    def test_single_expected_kind_still_requires_match(self):
        assert _top_level_kinds_match(["ClassNode"], ["ModuleNode"]) is False

    def test_single_expected_kind_matches(self):
        assert _top_level_kinds_match(["ClassNode"], ["ClassNode"]) is True

    def test_dsl_cross_category_not_conflict(self):
        """BUG-031: Ruby core DSL (attr_reader) + ActiveRecord DSL (validates)
        in the same file should NOT trigger DSL conflict."""
        assert (
            _top_level_kinds_match(
                [
                    "ClassNode:ApplicationRecord",
                    "DslCall:validates",
                    "DslCall:belongs_to",
                ],
                ["ClassNode", "DslCall:attr_reader"],
            )
            is True
        )

    def test_dsl_real_conflict_still_fires(self):
        """ActiveRecord vs ActionController DSLs should still conflict."""
        assert (
            _top_level_kinds_match(
                ["ClassNode", "DslCall:before_action"],
                ["ClassNode", "DslCall:validates", "DslCall:belongs_to"],
            )
            is False
        )

    def test_extras_in_file_ok(self):
        """File having MORE kinds than expected is fine."""
        assert (
            _top_level_kinds_match(
                [
                    "ClassNode:ApplicationRecord",
                    "DslCall:validates",
                    "DslCall:scope",
                    "IncludeCall:AASM",
                ],
                ["ClassNode", "DslCall:validates"],
            )
            is True
        )

    def test_half_threshold_for_large_expected(self):
        """For 4+ coarse kinds, 50% threshold applies (at least 2)."""
        assert (
            _top_level_kinds_match(
                ["ImportDeclaration", "ClassDeclaration"],
                [
                    "ImportDeclaration",
                    "ClassDeclaration",
                    "InterfaceDeclaration",
                    "TypeAliasDeclaration",
                ],
            )
            is True
        )

    def test_below_half_threshold_fails(self):
        assert (
            _top_level_kinds_match(
                ["ImportDeclaration"],
                [
                    "ImportDeclaration",
                    "ClassDeclaration",
                    "InterfaceDeclaration",
                    "TypeAliasDeclaration",
                ],
            )
            is False
        )


class TestConventionLint:
    def test_import_preference_violation(self):
        # over/preferred are MODULE specifiers; importing the banned module flags.
        content = 'import http from "axios";\n'
        conventions = {
            "imports": {
                "competing": [
                    {
                        "preferred": "@/lib/http",
                        "over": "axios",
                        "preferred_count": 47,
                        "over_count": 0,
                    }
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "import-preference-violation"
        assert "@/lib/http" in violations[0].message

    def test_no_violation_when_correct_import(self):
        content = 'import http from "@/lib/http";\n'
        conventions = {
            "imports": {
                "competing": [
                    {
                        "preferred": "@/lib/http",
                        "over": "axios",
                        "preferred_count": 47,
                        "over_count": 0,
                    }
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_preferred_scoped_package_not_false_flagged_by_substring_over(self):
        # The preferred scoped package ends with the banned name as a path
        # segment (`react-query` inside `@tanstack/react-query`). Importing the
        # preferred package must NOT flag, and the preferred-present skip guard
        # must still recognize it as present.
        content = 'import { useQuery } from "@tanstack/react-query";\n'
        conventions = {
            "imports": {
                "competing": [
                    {"preferred": "@tanstack/react-query", "over": "react-query"},
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert violations == []

    def test_bare_banned_package_still_flagged(self):
        content = 'import { useQuery } from "react-query";\n'
        conventions = {
            "imports": {
                "competing": [
                    {"preferred": "@tanstack/react-query", "over": "react-query"},
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "import-preference-violation"

    def test_banned_package_subpath_flagged(self):
        content = 'import x from "react-query/devtools";\n'
        conventions = {
            "imports": {
                "competing": [
                    {"preferred": "@tanstack/react-query", "over": "react-query"},
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1

    def test_naming_convention_violation(self):
        content = "interface UserProps {\n  name: string;\n}\n"
        conventions = {
            "naming": {
                "interface_prefix": {
                    "pattern": "I",
                    "consistency": 0.999,
                    "sample_size": 2158,
                },
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "naming-convention-violation"

    def test_no_naming_violation_with_correct_prefix(self):
        content = "interface IUserProps {\n  name: string;\n}\n"
        conventions = {
            "naming": {
                "interface_prefix": {
                    "pattern": "I",
                    "consistency": 0.999,
                    "sample_size": 2158,
                },
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_lowercase_interface_name_is_flagged(self):
        # A lowercase `interface params` is the most blatant I-prefix violation;
        # an uppercase-only declaration regex would skip it entirely.
        content = "interface params {\n  id: number;\n}\n"
        conventions = {
            "naming": {
                "interface_prefix": {
                    "pattern": "I",
                    "consistency": 1.0,
                    "sample_size": 2158,
                },
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert len(naming) == 1
        assert naming[0].actual == "params"

    def test_chameleon_ignore_suppresses_rule(self):
        content = '// chameleon-ignore import-preference\nimport http from "axios";\n'
        conventions = {
            "imports": {
                "competing": [
                    {
                        "preferred": "@/lib/http",
                        "over": "axios",
                        "preferred_count": 47,
                        "over_count": 0,
                    }
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_import_preference_ignores_string_embedded_import(self):
        # A competing import that lives entirely inside a string literal is not a
        # real import; the PreToolUse path already blanks these, so the shared
        # convention scan must agree (PreToolUse and PostToolUse must converge).
        content = "const code = \"import http from 'axios';\";\n"
        conventions = {
            "imports": {
                "competing": [
                    {
                        "preferred": "@/lib/http",
                        "over": "axios",
                        "preferred_count": 47,
                        "over_count": 0,
                    }
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert violations == []

    def test_import_preference_real_import_still_flagged_alongside_string(self):
        # A real banned import must still be flagged even when an unrelated
        # string literal also contains an import-looking snippet.
        content = 'const code = "import { x } from \'lodash\';";\nimport http from "axios";\n'
        conventions = {
            "imports": {
                "competing": [
                    {
                        "preferred": "@/lib/http",
                        "over": "axios",
                        "preferred_count": 47,
                        "over_count": 0,
                    }
                ],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "import-preference-violation"

    def test_ruby_no_ts_naming_violations(self):
        content = "class User < ApplicationRecord\nend\n"
        conventions = {
            "naming": {
                "interface_prefix": {
                    "pattern": "I",
                    "consistency": 0.999,
                    "sample_size": 100,
                },
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0


class TestRubyImportPreference:
    CONSTANT_PAIR = {
        "imports": {"competing": [{"preferred": "Gitlab::HTTP", "over": "Net::HTTP"}]},
    }
    REQUIRE_PAIR = {
        "imports": {"competing": [{"preferred": "gitlab/http", "over": "net/http"}]},
    }

    def test_banned_constant_reference_flagged(self):
        content = "response = Net::HTTP.get(uri)\n"
        violations = lint_conventions(content, self.CONSTANT_PAIR, language="ruby")
        assert [v.rule for v in violations] == ["import-preference-violation"]
        assert "Gitlab::HTTP" in violations[0].message

    def test_preferred_constant_present_skips_pair(self):
        content = "response = Gitlab::HTTP.get(url)\n"
        assert lint_conventions(content, self.CONSTANT_PAIR, language="ruby") == []

    def test_banned_require_flagged(self):
        content = "require 'net/http'\n\nuri = URI(url)\n"
        violations = lint_conventions(content, self.REQUIRE_PAIR, language="ruby")
        assert [v.rule for v in violations] == ["import-preference-violation"]

    def test_require_relative_banned_path_flagged(self):
        content = "require_relative 'net/http'\n"
        violations = lint_conventions(content, self.REQUIRE_PAIR, language="ruby")
        assert len(violations) == 1

    def test_preferred_require_present_skips_pair(self):
        content = "require 'gitlab/http'\n"
        assert lint_conventions(content, self.REQUIRE_PAIR, language="ruby") == []

    def test_constant_in_string_or_comment_not_flagged(self):
        content = "# Net::HTTP is banned here\nmsg = 'use Net::HTTP never'\n"
        assert lint_conventions(content, self.CONSTANT_PAIR, language="ruby") == []

    def test_require_inside_heredoc_not_flagged(self):
        # A require quoted inside a heredoc is example text, not a real require;
        # the string-embedded-import guard must blank it before extraction.
        content = "DOC = <<~RUBY\n  require 'net/http'\nRUBY\n"
        assert lint_conventions(content, self.REQUIRE_PAIR, language="ruby") == []

    def test_real_require_still_flagged_with_heredoc_present(self):
        content = "require 'net/http'\nDOC = <<~RUBY\n  example\nRUBY\n"
        violations = lint_conventions(content, self.REQUIRE_PAIR, language="ruby")
        assert [v.rule for v in violations] == ["import-preference-violation"]

    def test_es_import_text_in_ruby_not_flagged(self):
        # Regression: the TS import regex used to run on Ruby content, so an
        # ES import line pasted into a .rb file fired while real requires never
        # did. Specifier extraction is language-gated now.
        content = "code = \"import x from 'net/http'\"\n"
        assert lint_conventions(content, self.REQUIRE_PAIR, language="ruby") == []

    def test_longer_constant_path_not_false_flagged(self):
        # Foo::Net::HTTP is a different constant from Net::HTTP.
        content = "x = Foo::Net::HTTP.new\n"
        assert lint_conventions(content, self.CONSTANT_PAIR, language="ruby") == []

    def test_toplevel_qualified_constant_flagged(self):
        content = "x = ::Net::HTTP.get(uri)\n"
        violations = lint_conventions(content, self.CONSTANT_PAIR, language="ruby")
        assert len(violations) == 1

    def test_ruby_ignore_directive_suppresses(self):
        content = "# chameleon-ignore import-preference\nx = Net::HTTP.get(uri)\n"
        assert lint_conventions(content, self.CONSTANT_PAIR, language="ruby") == []


class TestRubyNamingConventionLint:
    SNAKE_CONV = {
        "naming": {
            "method_casing": {
                "pattern": "snake_case",
                "consistency": 0.99,
                "sample_size": 400,
            },
            "class_casing": {
                "pattern": "PascalCase",
                "consistency": 0.99,
                "sample_size": 120,
            },
            "constant_casing": {
                "pattern": "SCREAMING_SNAKE_CASE",
                "consistency": 0.97,
                "sample_size": 80,
            },
        },
    }

    def test_camel_case_method_flagged(self):
        content = "class UsersFinder\n  def fetchData\n  end\nend\n"
        violations = lint_conventions(content, self.SNAKE_CONV, language="ruby")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert len(naming) == 1
        assert naming[0].actual == "fetchData"
        assert "snake_case" in naming[0].message

    def test_lowercase_class_name_flagged(self):
        content = "class users_finder\n  def execute\n  end\nend\n"
        violations = lint_conventions(content, self.SNAKE_CONV, language="ruby")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert len(naming) == 1
        assert naming[0].actual == "users_finder"

    def test_badly_cased_constant_flagged(self):
        content = "class C\n  Max_retries = 3\nend\n"
        violations = lint_conventions(content, self.SNAKE_CONV, language="ruby")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert len(naming) == 1
        assert naming[0].actual == "Max_retries"

    def test_conforming_file_clean(self):
        content = (
            "class UsersFinder\n"
            "  MAX_RESULTS = 100\n"
            "  def execute\n"
            "  end\n"
            "  def filter_users\n"
            "  end\n"
            "end\n"
        )
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_pascal_constant_alias_not_flagged(self):
        # `Result = Struct.new(...)` is a legitimate class alias, conforming
        # regardless of the SCREAMING_SNAKE convention for value constants.
        content = "class C\n  Result = Struct.new(:ok)\nend\n"
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_operator_and_setter_defs_not_flagged(self):
        content = "class C\n  def ==(other)\n  end\n  def name=(value)\n  end\nend\n"
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_singleton_class_not_flagged(self):
        content = "class C\n  class << self\n    def helper\n    end\n  end\nend\n"
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_heredoc_content_not_flagged(self):
        content = "class C\n  BODY = <<~TEXT\n    def fakeMethod\n  TEXT\nend\n"
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_no_convention_entries_silent(self):
        content = "class C\n  def fetchData\n  end\nend\n"
        assert lint_conventions(content, {"naming": {}}, language="ruby") == []

    def test_ruby_ignore_directive_suppresses_naming(self):
        content = "# chameleon-ignore naming-convention\nclass C\n  def fetchData\n  end\nend\n"
        assert lint_conventions(content, self.SNAKE_CONV, language="ruby") == []

    def test_empty_conventions_no_violations(self):
        content = 'import { useQuery } from "react-query";\n'
        violations = lint_conventions(content, {}, language="typescript")
        assert len(violations) == 0

    def test_none_conventions_no_violations(self):
        violations = lint_conventions("const x = 1;", None, language="typescript")
        assert len(violations) == 0

    def test_inheritance_convention_violation(self):
        content = "class MyService\n  def execute\n  end\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ActiveInteraction::Base",
                "frequency": 0.82,
                "sample_size": 1414,
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 1
        assert violations[0].rule == "inheritance-convention-violation"

    def test_no_inheritance_violation_with_correct_base(self):
        content = "class MyService < ActiveInteraction::Base\n  def execute\n  end\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ActiveInteraction::Base",
                "frequency": 0.82,
                "sample_size": 1414,
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0

    def test_inheritance_lint_skips_indented_inner_class(self):
        # Outer class has the correct base; the indented inner class is base-less
        # but must NOT be flagged. The MULTILINE ^\\s*class regex used to match
        # the inner declaration and report "class Result should inherit ...".
        content = (
            "class FooController < ApplicationController\n"
            "  class Result\n"
            "    def ok; end\n"
            "  end\n"
            "end\n"
        )
        conventions = {
            "inheritance": {
                "dominant_base": "ApplicationController",
                "frequency": 0.9,
                "sample_size": 100,
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert violations == [], [v.message for v in violations]

    def test_inheritance_lint_flags_sibling_top_level_class(self):
        # Two top-level classes, the second lacks the base -> still flagged
        # (the inner-class skip must not suppress same-indent siblings).
        content = "class A < ApplicationController\nend\nclass B\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ApplicationController",
                "frequency": 0.9,
                "sample_size": 100,
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 1
        assert "class B" in violations[0].message

    def test_inheritance_chameleon_ignore(self):
        content = "# chameleon-ignore inheritance-convention\nclass MyService\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ActiveInteraction::Base",
                "frequency": 0.82,
                "sample_size": 1414,
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0

    def test_inheritance_not_checked_for_typescript(self):
        content = "class MyService {\n}\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ApplicationRecord",
                "frequency": 0.96,
                "sample_size": 117,
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_namespaced_class_with_known_base_not_flagged(self):
        # Regression (real-app test, maybe): a compact-namespaced controller
        # `class Api::V1::FooController < Api::V1::BaseController` was misparsed
        # (name truncated to `Api`, base read as `none`) and flagged, then the
        # PostToolUse escalation drove an unsatisfiable L0->L1->L2 STOP loop.
        # An established intermediate base must NOT be a violation.
        content = "class Api::V1::ChameleonProbeController < Api::V1::BaseController\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ApplicationController",
                "frequency": 0.64,
                "sample_size": 200,
                "known_bases": ["Api::V1::BaseController", "ApplicationController"],
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert violations == []

    def test_namespaced_class_name_parsed_fully(self):
        # A genuinely novel base on a namespaced class still flags, but the
        # message must carry the FULL class name, not the truncated namespace.
        content = "class Api::V1::FooController < SomethingNovel\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "ApplicationController",
                "frequency": 0.64,
                "sample_size": 200,
                "known_bases": ["ApplicationController"],
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 1
        assert "Api::V1::FooController" in violations[0].message
        assert violations[0].actual == "SomethingNovel"

    def test_ruby_framework_root_base_not_flagged(self):
        # Regression (real-app, mastodon): a large `app/controllers` archetype
        # folds api/admin/settings namespaces into one cluster whose dominant base
        # is `Api::BaseController`. A top-level web controller correctly extending
        # `ApplicationController` (the Rails framework root, shared ancestor of
        # every namespace base) must NOT be steered onto the API base.
        content = "class AccountsController < ApplicationController\nend\n"
        conventions = {
            "inheritance": {
                "dominant_base": "Api::BaseController",
                "frequency": 0.74,
                "sample_size": 334,
                "known_bases": ["Api::BaseController", "Admin::BaseController"],
            },
        }
        assert lint_conventions(content, conventions, language="ruby") == []

    def test_python_peer_composition_not_flagged(self):
        # Regression (real-app, py-django-readthedocs): a serializer extending a
        # same-file peer that itself roots at the dominant base is textbook DRF
        # composition, not a deviation. The whole chain is compliant; the check
        # must not flag each descendant link.
        content = (
            "class VersionSerializer(serializers.ModelSerializer):\n    pass\n\n"
            "class VersionAdminSerializer(VersionSerializer):\n    pass\n"
        )
        conventions = {
            "inheritance": {
                "dominant_base": "serializers.ModelSerializer",
                "frequency": 0.60,
                "sample_size": 40,
            },
        }
        assert lint_conventions(content, conventions, language="python") == []

    def test_python_same_module_base_family_not_flagged(self):
        # A different base from the dominant's own framework module
        # (`serializers.RelatedField` alongside `serializers.ModelSerializer`) is a
        # legitimate DRF variant, the Python analog of the Ruby `*BaseController`
        # namespace family -- not a wrong base.
        content = "class NotificationField(serializers.RelatedField):\n    pass\n"
        conventions = {
            "inheritance": {
                "dominant_base": "serializers.ModelSerializer",
                "frequency": 0.60,
                "sample_size": 40,
            },
        }
        assert lint_conventions(content, conventions, language="python") == []

    def test_python_genuinely_unrelated_base_still_flags(self):
        # Detection intact: a serializer extending an unrelated, non-peer,
        # non-family base is still flagged.
        content = "class BadSerializer(SomethingUnrelated):\n    pass\n"
        conventions = {
            "inheritance": {
                "dominant_base": "serializers.ModelSerializer",
                "frequency": 0.60,
                "sample_size": 40,
            },
        }
        v = lint_conventions(content, conventions, language="python")
        assert len(v) == 1
        assert v[0].rule == "inheritance-convention-violation"


class TestPythonNamingConventionLintUnicode:
    """PEP 3131 unicode identifiers must be classified by their own script's
    casing, not misclassified as a violation by an ASCII-only check."""

    SNAKE_CONV = {
        "naming": {
            "method_casing": {
                "pattern": "snake_case",
                "consistency": 0.95,
                "sample_size": 40,
            },
        },
    }

    def test_unicode_snake_case_function_not_flagged(self):
        content = "def calc_café(x):\n    return x + 1\n"
        violations = lint_conventions(content, self.SNAKE_CONV, language="python")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert naming == []

    def test_ascii_camel_case_function_still_flagged(self):
        content = "def fetchData(x):\n    return x + 1\n"
        violations = lint_conventions(content, self.SNAKE_CONV, language="python")
        naming = [v for v in violations if v.rule == "naming-convention-violation"]
        assert len(naming) == 1
        assert naming[0].actual == "fetchData"


class TestRubyHeredocStrip:
    """The heredoc blanker must be O(n) and precise.

    The first implementation was a lazy cross-line regex; on a file with many
    unterminated openers each match attempt rescanned to EOF — quadratic, 4+
    seconds at the 100KB lint cap. A hook-path scan over attacker-controllable
    repo content cannot afford that, so the blanker is a single forward pass.
    """

    def _strip(self, src: str) -> str:
        from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments

        return _strip_ruby_strings_and_comments(src)

    def test_pathological_unterminated_openers_complete_fast(self):
        import time

        evil = ("x = <<~AAA\n" * 10_000)[:100_000]
        t0 = time.monotonic()
        out = self._strip(evil)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"heredoc strip took {elapsed:.2f}s on pathological input"
        assert len(out) == len(evil)

    def test_heredoc_body_blanked_terminator_consumed(self):
        src = "x = <<~TEXT\n  def fakeMethod\n  class fake_class\nTEXT\ny = 1\n"
        out = self._strip(src)
        assert "fakeMethod" not in out
        assert "fake_class" not in out
        assert "y = 1" in out
        assert len(out) == len(src)
        assert out.count("\n") == src.count("\n")

    def test_code_before_opener_kept(self):
        src = "query = <<~SQL.strip\n  SELECT 1\nSQL\n"
        out = self._strip(src)
        assert "query = " in out
        assert "SELECT" not in out

    def test_left_shift_on_value_not_treated_as_heredoc(self):
        # `arr<<FOO` / `(x)<<FOO` are shifts, not heredocs: code after must
        # NOT be blanked to EOF.
        src = "arr<<FOO\n(x)<<BAR\ndef real_method\nend\n"
        out = self._strip(src)
        assert "def real_method" in out

    def test_class_self_singleton_not_treated_as_heredoc(self):
        src = "class C\n  class << self\n    def helper\n    end\n  end\nend\n"
        out = self._strip(src)
        assert "def helper" in out

    def test_dash_and_quoted_delimiters(self):
        src = "a = <<-EOS\n  def inner_a\n  EOS\nb = <<'RAW'\n  def inner_b\nRAW\nc = 1\n"
        out = self._strip(src)
        assert "inner_a" not in out
        assert "inner_b" not in out
        assert "c = 1" in out

    def test_stacked_heredocs_both_bodies_blanked(self):
        src = "foo(<<~AAA, <<~BBB)\n  def body_a\nAAA\n  def body_b\nBBB\nz = 1\n"
        out = self._strip(src)
        assert "body_a" not in out
        assert "body_b" not in out
        assert "z = 1" in out

    def test_unterminated_heredoc_blanks_to_eof_not_code_before(self):
        # An unterminated heredoc is a syntax error; its body is string
        # content, so blanking it to EOF can only reduce false positives.
        src = "x = 1\ny = <<~SQL\n  def looks_like_code\n"
        out = self._strip(src)
        assert "x = 1" in out
        assert "looks_like_code" not in out


class TestStringEmbeddedSlashesDoNotBlindImports:
    """qa25 P3 — a string holding `//` (a URL) on the line above an import was
    mis-tokenized as a comment opener, unbalancing the quote pairing across the
    newline and blanking the real import below; the deny rule went blind."""

    CONVENTIONS = {
        "imports": {
            "competing": [
                {
                    "preferred": "@/lib/http",
                    "over": "axios",
                    "preferred_count": 47,
                    "over_count": 0,
                }
            ],
        },
    }

    def test_url_string_above_import_does_not_blind_the_rule(self):
        content = 'const E = "https://api.x/v1"\nimport http from "axios";\n'
        violations = lint_conventions(content, self.CONVENTIONS, language="typescript")
        assert [v.rule for v in violations] == ["import-preference-violation"]

    def test_protocol_relative_url_single_quotes(self):
        content = "const cdn = '//cdn.example.com/lib'\nimport http from \"axios\";\n"
        violations = lint_conventions(content, self.CONVENTIONS, language="typescript")
        assert [v.rule for v in violations] == ["import-preference-violation"]

    def test_real_line_comment_still_stripped(self):
        # A genuine comment naming the banned module must not flag.
        content = '// we used to import axios from "axios" here\nimport http from "@/lib/http";\n'
        violations = lint_conventions(content, self.CONVENTIONS, language="typescript")
        assert violations == []

    def test_banned_import_inside_string_still_immune(self):
        # The string-blanking that protects help-text mentioning imports keeps
        # working under the combined pass.
        content = "const help = 'import http from \"axios\"'\n"
        violations = lint_conventions(content, self.CONVENTIONS, language="typescript")
        assert violations == []


class TestRubyPercentLiteralBlanking:
    def test_eval_inside_percent_literal_blanked(self):
        from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments as strip

        stripped = strip("PAT = %q{eval(}\nreal = eval(x)\n")
        # The literal's eval( is blanked; a real eval( on the next line survives.
        assert "eval(" not in stripped.splitlines()[0]
        assert "eval(" in stripped.splitlines()[1]

    def test_modulo_not_treated_as_literal(self):
        from chameleon_mcp.lint_engine import _strip_ruby_strings_and_comments as strip

        out = strip("x = a % b\ny = total%count\n")
        assert "% b" in out
        assert "total%count" in out

    def test_word_array_content_blanked(self):
        from chameleon_mcp.lint_engine import _blank_ruby_percent_literals as blank

        assert "eval" not in blank("x = %w[eval( foo]")


class TestRubyInheritanceShortFormBase:
    CONV = {
        "inheritance": {
            "dominant_base": "Api::V1::BaseController",
            "frequency": 0.9,
            "known_bases": ["Api::V1::BaseController"],
        }
    }

    def test_short_form_base_accepted(self):
        content = "module Api::V1\n  class QboController < BaseController\n  end\nend\n"
        assert lint_conventions(content, self.CONV, language="ruby") == []

    def test_fully_qualified_base_accepted(self):
        content = "class QboController < Api::V1::BaseController\nend\n"
        assert lint_conventions(content, self.CONV, language="ruby") == []

    def test_wrong_base_still_flagged(self):
        content = "class QboController < SomethingElse\nend\n"
        rules = {v.rule for v in lint_conventions(content, self.CONV, language="ruby")}
        assert "inheritance-convention-violation" in rules


class TestTsAmbientInterfaceNaming:
    CONV = {"naming": {"interface_prefix": {"pattern": "I", "consistency": 1.0}}}

    def test_declare_global_interface_exempt(self):
        content = "declare global {\n  interface Window { x: string }\n}\n"
        assert lint_conventions(content, self.CONV, language="typescript") == []

    def test_declare_module_interface_exempt(self):
        content = 'declare module "ext" {\n  interface Plugin { y: number }\n}\n'
        assert lint_conventions(content, self.CONV, language="typescript") == []

    def test_plain_interface_still_flagged(self):
        content = "export interface UserProfile { x: number }\n"
        rules = {v.rule for v in lint_conventions(content, self.CONV, language="typescript")}
        assert "naming-convention-violation" in rules

    def test_nested_braces_in_ambient_block_do_not_leak_exemption(self):
        content = (
            "declare global {\n  interface Window { fn: () => { a: number } }\n}\n"
            "export interface BadName {}\n"
        )
        actuals = {v.actual for v in lint_conventions(content, self.CONV, language="typescript")}
        assert "BadName" in actuals
