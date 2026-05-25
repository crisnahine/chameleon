"""Unit tests for chameleon_mcp.lint_engine — pure functions, no I/O."""
from __future__ import annotations

from chameleon_mcp.lint_engine import (
    DimensionSnapshot,
    _extract_ruby,
    _extract_typescript,
    _fold_string_concat,
    canonical_confidence,
    detect_language,
    lint,
)


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _extract_typescript — default export kind
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _extract_typescript — named export counting
# ---------------------------------------------------------------------------


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
        # count is 1 (the alias target)
        assert snap.named_export_count == 1

    def test_deduplicates(self):
        # same name exported via const and re-export list
        code = "export const foo = 1;\nexport { foo };\n"
        snap = _extract_typescript(code)
        assert snap.named_export_count == 1


# ---------------------------------------------------------------------------
# _extract_typescript — JSX detection (after string/comment stripping)
# ---------------------------------------------------------------------------


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
        # JSX-like text inside a string literal should not trigger jsx_present
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


# ---------------------------------------------------------------------------
# _extract_typescript — content_signal
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _extract_ruby — top-level class/module at column 0
# ---------------------------------------------------------------------------


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
        # indented class should not be detected as top-level in the column-0 pass
        code = "  class Nested\n  end\n"
        snap = _extract_ruby(code)
        # no top-level class or module from the column-0 scan
        column0_kinds = [
            k for k in snap.top_level_node_kinds
            if not k.startswith("IncludeCall:") and not k.startswith("DslCall:")
        ]
        assert column0_kinds == []

    def test_def_at_column_zero(self):
        code = "def helper\nend\n"
        snap = _extract_ruby(code)
        assert "DefNode" in snap.top_level_node_kinds


# ---------------------------------------------------------------------------
# _extract_ruby — superclass detection
# ---------------------------------------------------------------------------


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
        # bare ClassNode, no colon-suffixed variant
        assert "ClassNode" in snap.top_level_node_kinds
        assert not any(
            k.startswith("ClassNode:") for k in snap.top_level_node_kinds
        )


# ---------------------------------------------------------------------------
# _extract_ruby — DSL call detection
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# lint() — violations on dimension mismatch
# ---------------------------------------------------------------------------


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
        # null fields in ast_query mean "no expectation" - never flag
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


# ---------------------------------------------------------------------------
# canonical_confidence()
# ---------------------------------------------------------------------------


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
        # 1 of 2 checks pass
        assert canonical_confidence(snap, query) == 0.5


# ---------------------------------------------------------------------------
# _fold_string_concat()
# ---------------------------------------------------------------------------


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
        # with max_folds=1 only one pair gets folded
        result = _fold_string_concat('"a" + "b" + "c"', max_folds=1)
        assert result == '"ab" + "c"'

    def test_whitespace_around_plus(self):
        assert _fold_string_concat('"a"  +  "b"') == '"ab"'
