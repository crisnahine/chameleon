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

    def test_py_unsupported(self):
        assert detect_language("main.py") is None

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
            k for k in snap.top_level_node_kinds
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
        assert not any(
            k.startswith("ClassNode:") for k in snap.top_level_node_kinds
        )

    def test_superclass_resolved_per_class(self):
        code = (
            "class Plain\nend\n"
            "class Account < ApplicationRecord\nend\n"
        )
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
        code = (
            "class User < ApplicationRecord\n"
            "  validates :a\n"
            "  validates :b\n"
            "end\n"
        )
        snap = _extract_ruby(code)
        dsl_validates = [
            k for k in snap.top_level_node_kinds if k == "DslCall:validates"
        ]
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
        assert _fold_string_concat("'ab' + 'cd'") == "'abcd'"

    def test_triple_concat(self):
        assert _fold_string_concat('"a" + "b" + "c"') == '"abc"'

    def test_mixed_quotes_no_fold(self):
        original = "\"a\" + 'b'"
        assert _fold_string_concat(original) == original

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
        assert _top_level_kinds_match(
            ["ClassNode", "DslCall:validates"],
            ["ClassNode", "DslCall:validates"],
        ) is True

    def test_bare_class_fails_two_kind_expected(self):
        """BUG-031: 1 match of 2 coarse kinds must fail (min-2 threshold)."""
        assert _top_level_kinds_match(
            ["ClassNode"],
            ["ClassNode", "DslCall:scope"],
        ) is False

    def test_bare_const_fails_two_kind_ts_expected(self):
        """BUG-031: TS const x=1 (FirstStatement) vs action with imports."""
        assert _top_level_kinds_match(
            ["FirstStatement"],
            ["FirstStatement", "ImportDeclaration"],
        ) is False

    def test_coarse_dedup_prevents_inflated_match(self):
        """BUG-031: FirstStatement + ExportAssignment both -> CodeDeclaration.
        A file with just FirstStatement should NOT count as 2 matches."""
        assert _top_level_kinds_match(
            ["FirstStatement"],
            ["ClassDeclaration", "ExportAssignment", "FirstStatement", "ImportDeclaration"],
        ) is False

    def test_single_expected_kind_still_requires_match(self):
        assert _top_level_kinds_match(["ClassNode"], ["ModuleNode"]) is False

    def test_single_expected_kind_matches(self):
        assert _top_level_kinds_match(["ClassNode"], ["ClassNode"]) is True

    def test_dsl_cross_category_not_conflict(self):
        """BUG-031: Ruby core DSL (attr_reader) + ActiveRecord DSL (validates)
        in the same file should NOT trigger DSL conflict."""
        assert _top_level_kinds_match(
            ["ClassNode:ApplicationRecord", "DslCall:validates", "DslCall:belongs_to"],
            ["ClassNode", "DslCall:attr_reader"],
        ) is True

    def test_dsl_real_conflict_still_fires(self):
        """ActiveRecord vs ActionController DSLs should still conflict."""
        assert _top_level_kinds_match(
            ["ClassNode", "DslCall:before_action"],
            ["ClassNode", "DslCall:validates", "DslCall:belongs_to"],
        ) is False

    def test_extras_in_file_ok(self):
        """File having MORE kinds than expected is fine."""
        assert _top_level_kinds_match(
            ["ClassNode:ApplicationRecord", "DslCall:validates", "DslCall:scope", "IncludeCall:AASM"],
            ["ClassNode", "DslCall:validates"],
        ) is True

    def test_half_threshold_for_large_expected(self):
        """For 4+ coarse kinds, 50% threshold applies (at least 2)."""
        assert _top_level_kinds_match(
            ["ImportDeclaration", "ClassDeclaration"],
            ["ImportDeclaration", "ClassDeclaration", "InterfaceDeclaration", "TypeAliasDeclaration"],
        ) is True

    def test_below_half_threshold_fails(self):
        assert _top_level_kinds_match(
            ["ImportDeclaration"],
            ["ImportDeclaration", "ClassDeclaration", "InterfaceDeclaration", "TypeAliasDeclaration"],
        ) is False


class TestConventionLint:
    def test_import_preference_violation(self):
        content = 'import { useQuery } from "@tanstack/react-query";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "import-preference-violation"
        assert "useCustomQuery" in violations[0].message

    def test_no_violation_when_correct_import(self):
        content = 'import { useCustomQuery } from "@/hooks/useCustomQuery";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_naming_convention_violation(self):
        content = 'interface UserProps {\n  name: string;\n}\n'
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 1
        assert violations[0].rule == "naming-convention-violation"

    def test_no_naming_violation_with_correct_prefix(self):
        content = 'interface IUserProps {\n  name: string;\n}\n'
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 2158},
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_chameleon_ignore_suppresses_rule(self):
        content = '// chameleon-ignore import-preference\nimport { useQuery } from "@tanstack/react-query";\n'
        conventions = {
            "imports": {
                "competing": [{"preferred": "useCustomQuery", "over": "useQuery", "preferred_count": 47, "over_count": 0}],
            },
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0

    def test_ruby_no_ts_naming_violations(self):
        content = "class User < ApplicationRecord\nend\n"
        conventions = {
            "naming": {
                "interface_prefix": {"pattern": "I", "consistency": 0.999, "sample_size": 100},
            },
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0

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
            "inheritance": {"dominant_base": "ActiveInteraction::Base", "frequency": 0.82, "sample_size": 1414},
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 1
        assert violations[0].rule == "inheritance-convention-violation"

    def test_no_inheritance_violation_with_correct_base(self):
        content = "class MyService < ActiveInteraction::Base\n  def execute\n  end\nend\n"
        conventions = {
            "inheritance": {"dominant_base": "ActiveInteraction::Base", "frequency": 0.82, "sample_size": 1414},
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0

    def test_inheritance_chameleon_ignore(self):
        content = "# chameleon-ignore inheritance-convention\nclass MyService\nend\n"
        conventions = {
            "inheritance": {"dominant_base": "ActiveInteraction::Base", "frequency": 0.82, "sample_size": 1414},
        }
        violations = lint_conventions(content, conventions, language="ruby")
        assert len(violations) == 0

    def test_inheritance_not_checked_for_typescript(self):
        content = "class MyService {\n}\n"
        conventions = {
            "inheritance": {"dominant_base": "ApplicationRecord", "frequency": 0.96, "sample_size": 117},
        }
        violations = lint_conventions(content, conventions, language="typescript")
        assert len(violations) == 0
