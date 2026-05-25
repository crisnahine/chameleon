"""Unit tests for chameleon_mcp.signatures — pure functions, no I/O."""
from __future__ import annotations

from chameleon_mcp.signatures import (
    ClusterKey,
    bucket_named_export_count,
    compute_signature,
    content_signal_match_for,
    hash_import_set,
    path_pattern_bucket_for,
)

# ---------------------------------------------------------------------------
# bucket_named_export_count — boundary values
# ---------------------------------------------------------------------------


class TestBucketNamedExportCount:
    def test_zero(self):
        assert bucket_named_export_count(0) == "0"

    def test_negative_maps_to_zero(self):
        assert bucket_named_export_count(-1) == "0"

    def test_one(self):
        assert bucket_named_export_count(1) == "1"

    def test_two(self):
        assert bucket_named_export_count(2) == "2-4"

    def test_four(self):
        assert bucket_named_export_count(4) == "2-4"

    def test_five(self):
        assert bucket_named_export_count(5) == "5-9"

    def test_nine(self):
        assert bucket_named_export_count(9) == "5-9"

    def test_ten(self):
        assert bucket_named_export_count(10) == "10+"

    def test_large(self):
        assert bucket_named_export_count(100) == "10+"


# ---------------------------------------------------------------------------
# hash_import_set — deterministic, order-independent
# ---------------------------------------------------------------------------


class TestHashImportSet:
    def test_deterministic(self):
        imports = [("react", "default"), ("lodash", "named")]
        h1 = hash_import_set(imports)
        h2 = hash_import_set(imports)
        assert h1 == h2

    def test_order_independent(self):
        a = [("react", "default"), ("lodash", "named")]
        b = [("lodash", "named"), ("react", "default")]
        assert hash_import_set(a) == hash_import_set(b)

    def test_different_sets_differ(self):
        a = [("react", "default")]
        b = [("vue", "default")]
        assert hash_import_set(a) != hash_import_set(b)

    def test_empty(self):
        h = hash_import_set([])
        assert isinstance(h, str)
        assert len(h) == 64  # sha256 hex

    def test_kind_matters(self):
        a = [("react", "default")]
        b = [("react", "named")]
        assert hash_import_set(a) != hash_import_set(b)


# ---------------------------------------------------------------------------
# path_pattern_bucket_for — short paths, include_extension
# ---------------------------------------------------------------------------


class TestPathPatternBucketFor:
    def test_single_segment_root(self):
        bucket, sub = path_pattern_bucket_for("Gemfile")
        assert bucket == "(root)"
        assert sub == ""

    def test_two_segments(self):
        bucket, sub = path_pattern_bucket_for("app/models/user.rb")
        assert bucket == "app/models"
        assert sub == ""

    def test_deep_path_default_depth(self):
        bucket, sub = path_pattern_bucket_for("app/controllers/api/v1/users.rb")
        assert bucket == "app/controllers"
        assert sub == "api/v1"

    def test_monorepo_workspace(self):
        bucket, sub = path_pattern_bucket_for("packages/excalidraw/components/Foo.tsx")
        assert bucket == "packages/excalidraw/components"

    def test_monorepo_apps(self):
        bucket, sub = path_pattern_bucket_for("apps/web/routes/page.tsx")
        assert bucket == "apps/web/routes"

    def test_include_extension_tsx(self):
        bucket, _ = path_pattern_bucket_for(
            "src/components/Button.tsx",
            include_extension=True,
        )
        assert bucket.endswith(":tsx")

    def test_include_extension_ts(self):
        bucket, _ = path_pattern_bucket_for(
            "src/components/helper.ts",
            include_extension=True,
        )
        assert bucket.endswith(":ts")

    def test_no_extension_by_default(self):
        bucket, _ = path_pattern_bucket_for("src/components/Button.tsx")
        assert ":" not in bucket

    def test_tsx_and_ts_differ_with_extension(self):
        b1, _ = path_pattern_bucket_for("src/components/Button.tsx", include_extension=True)
        b2, _ = path_pattern_bucket_for("src/components/helper.ts", include_extension=True)
        assert b1 != b2


# ---------------------------------------------------------------------------
# content_signal_match_for — each directive
# ---------------------------------------------------------------------------


class TestContentSignalMatchFor:
    def test_use_client_double_quotes(self):
        assert content_signal_match_for('"use client";') == "use_client"

    def test_use_client_single_quotes(self):
        assert content_signal_match_for("'use client';") == "use_client"

    def test_use_server(self):
        assert content_signal_match_for('"use server";') == "use_server"

    def test_shebang(self):
        assert content_signal_match_for("#!/usr/bin/env node\n") == "shebang"

    def test_ts_pragma(self):
        assert content_signal_match_for("// @ts-nocheck\n") == "ts_pragma"

    def test_ts_pragma_with_leading_whitespace(self):
        assert content_signal_match_for("  // @ts-nocheck\n") == "ts_pragma"

    def test_none_signal(self):
        assert content_signal_match_for("import React from 'react';") == "none"

    def test_empty_string(self):
        assert content_signal_match_for("") == "none"


# ---------------------------------------------------------------------------
# compute_signature — returns frozen hashable ClusterKey
# ---------------------------------------------------------------------------


class TestComputeSignature:
    def test_returns_cluster_key(self):
        key = compute_signature(
            file_path="src/components/Button.tsx",
            content_first_200_bytes='"use client";\nimport React from "react";\n',
            top_level_node_kinds=["ImportDeclaration", "FunctionDeclaration"],
            default_export_kind="FunctionDeclaration",
            named_export_count=0,
            import_specifiers=[("react", "default")],
            has_jsx=True,
        )
        assert isinstance(key, ClusterKey)

    def test_frozen_hashable(self):
        key = compute_signature(
            file_path="src/utils/math.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["FunctionDeclaration"],
            default_export_kind=None,
            named_export_count=3,
            import_specifiers=[],
            has_jsx=False,
        )
        # frozen dataclass is hashable -> usable as dict key
        d = {key: True}
        assert d[key] is True

    def test_same_inputs_same_key(self):
        kwargs = dict(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["ImportDeclaration"],
            default_export_kind="FunctionDeclaration",
            named_export_count=1,
            import_specifiers=[("lodash", "named")],
            has_jsx=False,
        )
        k1 = compute_signature(**kwargs)
        k2 = compute_signature(**kwargs)
        assert k1 == k2

    def test_bucket_wired_correctly(self):
        key = compute_signature(
            file_path="src/components/Button.tsx",
            content_first_200_bytes="",
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.path_pattern_bucket == "src/components"

    def test_content_signal_wired(self):
        key = compute_signature(
            file_path="src/app.ts",
            content_first_200_bytes='"use client";',
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.content_signal_match == "use_client"

    def test_named_export_bucket_wired(self):
        key = compute_signature(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=[],
            default_export_kind=None,
            named_export_count=7,
            import_specifiers=[],
            has_jsx=False,
        )
        assert key.named_export_count_bucket == "5-9"

    def test_to_dict_roundtrip(self):
        key = compute_signature(
            file_path="src/index.ts",
            content_first_200_bytes="",
            top_level_node_kinds=["ImportDeclaration"],
            default_export_kind=None,
            named_export_count=0,
            import_specifiers=[],
            has_jsx=True,
        )
        d = key.to_dict()
        assert d["jsx_present"] is True
        assert d["top_level_node_kinds"] == ["ImportDeclaration"]
        assert d["default_export_kind"] is None
